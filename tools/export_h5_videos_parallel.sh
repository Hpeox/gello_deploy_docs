#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-/usr/bin/python3}"
WORKERS=4
RUN_DIR=""
CPU_PAIRS="8-9,10-11,12-13,14-15"
USE_CPU_AFFINITY=1
FORWARD_ARGS=()
PIDS=()
WORKER_IDS=()
INTERRUPTED=0

usage() {
  cat <<'EOF'
Usage:
  tools/export_h5_videos_parallel.sh [parallel-options] INPUT [export-options]

Parallel options:
  --workers N            Worker count. Default: 4.
  --python PATH          Python executable. Default: ${PYTHON:-/usr/bin/python3}.
  --run-dir PATH         Override the queue, result, and log directory.
                         Default: OUTPUT_DIR/export_video_runs/<run-id>.
  --cpu-pairs PAIRS      Comma-separated SMT pairs. Default: 8-9,10-11,12-13,14-15.
  --no-cpu-affinity      Do not bind workers to CPU pairs.

Export options:
  --output-dir PATH      Flat MP4 output directory.
  --output_dir PATH      Alias for --output-dir.
  --skip-existing
  --fps FPS
  --ffmpeg PATH
  --gpu INDEX
  --preset p1..p7
  --cq 0..51
  --batch-frames N
EOF
}

die() {
  echo "[ERROR] $*" >&2
  exit 2
}

cleanup_workers() {
  local pid
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      pkill -TERM -P "$pid" 2>/dev/null || true
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
}

handle_signal() {
  INTERRUPTED=1
  cleanup_workers
}

trap handle_signal INT TERM

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workers)
      [[ $# -ge 2 ]] || die "--workers requires a value"
      WORKERS="$2"
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || die "--python requires a value"
      PYTHON_BIN="$2"
      shift 2
      ;;
    --run-dir)
      [[ $# -ge 2 ]] || die "--run-dir requires a value"
      RUN_DIR="$2"
      shift 2
      ;;
    --cpu-pairs)
      [[ $# -ge 2 ]] || die "--cpu-pairs requires a value"
      CPU_PAIRS="$2"
      shift 2
      ;;
    --no-cpu-affinity)
      USE_CPU_AFFINITY=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

[[ "$WORKERS" =~ ^[0-9]+$ ]] || die "--workers must be an integer"
((WORKERS >= 1)) || die "--workers must be positive"
[[ -x "$PYTHON_BIN" ]] || die "python executable not found or not executable: $PYTHON_BIN"
command -v setsid >/dev/null 2>&1 || die "setsid is not available"
if ((USE_CPU_AFFINITY)); then
  command -v taskset >/dev/null 2>&1 || die "taskset is not available"
fi
[[ "${#FORWARD_ARGS[@]}" -gt 0 ]] || die "INPUT is required"

if [[ -z "$RUN_DIR" ]]; then
  OUTPUT_DIR="$(
    "$PYTHON_BIN" "${SCRIPT_DIR}/export_h5_videos_parallel.py" \
      output-dir -- "${FORWARD_ARGS[@]}"
  )" || exit $?
  RUN_DIR="${OUTPUT_DIR}/export_video_runs/run_$(date +%Y%m%d_%H%M%S)_$$"
elif [[ "$RUN_DIR" != /* ]]; then
  RUN_DIR="${PWD}/${RUN_DIR}"
fi

CPU_BINDINGS=()
if ((USE_CPU_AFFINITY)); then
  mapfile -t CPU_BINDINGS < <(
    "$PYTHON_BIN" "${SCRIPT_DIR}/export_h5_videos_parallel.py" \
      validate-cpus --workers "$WORKERS" --cpu-pairs "$CPU_PAIRS"
  )
  [[ "${#CPU_BINDINGS[@]}" -eq "$WORKERS" ]] || die "CPU topology validation failed"
fi

"$PYTHON_BIN" "${SCRIPT_DIR}/export_h5_videos_parallel.py" \
  prepare --run-dir "$RUN_DIR" -- "${FORWARD_ARGS[@]}" || exit $?

if ((USE_CPU_AFFINITY)); then
  echo "[INFO] run_dir=$RUN_DIR workers=$WORKERS cpu_pairs=${CPU_BINDINGS[*]}"
else
  echo "[INFO] run_dir=$RUN_DIR workers=$WORKERS cpu_affinity=disabled"
fi

for ((index = 0; index < WORKERS; index++)); do
  worker_id="worker_${index}"
  log_path="${RUN_DIR}/logs/workers/${worker_id}.log"
  if ((USE_CPU_AFFINITY)); then
    setsid taskset -c "${CPU_BINDINGS[$index]}" \
      "$PYTHON_BIN" "${SCRIPT_DIR}/export_h5_videos_parallel.py" \
      worker --run-dir "$RUN_DIR" --worker-id "$worker_id" \
      >"$log_path" 2>&1 &
  else
    setsid "$PYTHON_BIN" "${SCRIPT_DIR}/export_h5_videos_parallel.py" \
      worker --run-dir "$RUN_DIR" --worker-id "$worker_id" \
      >"$log_path" 2>&1 &
  fi
  PIDS+=("$!")
  WORKER_IDS+=("$worker_id")
done

for ((index = 0; index < WORKERS; index++)); do
  pid="${PIDS[$index]}"
  worker_id="${WORKER_IDS[$index]}"
  if wait "$pid"; then
    status=0
  else
    status=$?
  fi
  if [[ "$INTERRUPTED" == "1" ]]; then
    exit 130
  fi
  if [[ "$status" -ne 0 ]]; then
    echo "[WARN] ${worker_id} exited with code ${status}" >&2
    "$PYTHON_BIN" "${SCRIPT_DIR}/export_h5_videos_parallel.py" \
      recover --run-dir "$RUN_DIR" --worker-id "$worker_id" --exit-code "$status"
  fi
done

"$PYTHON_BIN" "${SCRIPT_DIR}/export_h5_videos_parallel.py" \
  finalize --run-dir "$RUN_DIR"
