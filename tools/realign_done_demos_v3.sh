#!/usr/bin/env bash
# Rebuild aligned/ outputs for done demos using tools/align_demo_timestamps_v3.py.
#
# The script generates each new alignment under temp_alignment_test first. Only
# after a demo succeeds does it replace runtime_sessions/demos/<demo>/aligned.
# Previous aligned directories and manifest.json files are backed up under the
# run directory by default.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEMOS_DIR="${REPO_ROOT}/runtime_sessions/demos"
ALIGN_SCRIPT="${REPO_ROOT}/tools/align_demo_timestamps_v3.py"
PYTHON_BIN="${PYTHON:-/usr/bin/python3}"
BASE="realsense:bundle"
MODE="causal"
HZ="30.0"
START_TRIM_S="2.0"
END_TRIM_S="0.0"
RUN_ROOT=""
DRY_RUN=0
UPDATE_MANIFEST=1
KEEP_BACKUPS=1
LIMIT=0
ONLY_DEMOS=()

usage() {
  cat <<'EOF'
Usage:
  tools/realign_done_demos_v3.sh [options]

Rebuild aligned/ for every runtime_sessions/demos/demo_*/manifest.json whose
manifest status is "done", using tools/align_demo_timestamps_v3.py.

Options:
  --repo-root PATH        Repository root. Default: parent of tools/.
  --demos-dir PATH       Demo directory. Default: <repo-root>/runtime_sessions/demos.
  --run-root PATH        Temp run directory. Default: <repo-root>/temp_alignment_test/realign_done_demos_v3_<timestamp>.
  --python PATH          Python executable. Default: ${PYTHON:-/usr/bin/python3}.
  --base VALUE           v3 base. Default: realsense:bundle.
  --mode VALUE           causal or nearest. Default: causal.
  --hz FLOAT             Grid Hz if --base grid. Default: 30.0.
  --start-trim-s FLOAT   Global start trim seconds. Default: 2.0.
  --end-trim-s FLOAT     Global end trim seconds. Default: 0.0.
  --only DEMO_NAME       Restrict to one demo name, repeatable.
  --limit N              Process at most N done demos after filtering.
  --dry-run              List selected demos without rebuilding or replacing.
  --no-update-manifest   Replace aligned/ only; leave manifest.json untouched.
  --discard-backups      Remove per-demo aligned/ and manifest backups after success.
  -h, --help             Show this help.

Notes:
  - Existing demo raw inputs are read only, but aligned/ and optionally
    manifest.json are intentionally replaced for successful demos.
  - If a demo alignment fails, its existing aligned/ is left unchanged.
  - Detailed logs, generated temp outputs, backups, and summaries are written
    under the run root.
EOF
}

die() {
  echo "[ERROR] $*" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      [[ $# -ge 2 ]] || die "--repo-root requires a value"
      REPO_ROOT="$(cd "$2" && pwd)"
      DEMOS_DIR="${REPO_ROOT}/runtime_sessions/demos"
      ALIGN_SCRIPT="${REPO_ROOT}/tools/align_demo_timestamps_v3.py"
      shift 2
      ;;
    --demos-dir)
      [[ $# -ge 2 ]] || die "--demos-dir requires a value"
      DEMOS_DIR="$(cd "$2" && pwd)"
      shift 2
      ;;
    --run-root)
      [[ $# -ge 2 ]] || die "--run-root requires a value"
      RUN_ROOT="$2"
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || die "--python requires a value"
      PYTHON_BIN="$2"
      shift 2
      ;;
    --base)
      [[ $# -ge 2 ]] || die "--base requires a value"
      BASE="$2"
      shift 2
      ;;
    --mode)
      [[ $# -ge 2 ]] || die "--mode requires a value"
      MODE="$2"
      shift 2
      ;;
    --hz)
      [[ $# -ge 2 ]] || die "--hz requires a value"
      HZ="$2"
      shift 2
      ;;
    --start-trim-s)
      [[ $# -ge 2 ]] || die "--start-trim-s requires a value"
      START_TRIM_S="$2"
      shift 2
      ;;
    --end-trim-s)
      [[ $# -ge 2 ]] || die "--end-trim-s requires a value"
      END_TRIM_S="$2"
      shift 2
      ;;
    --only)
      [[ $# -ge 2 ]] || die "--only requires a value"
      ONLY_DEMOS+=("$2")
      shift 2
      ;;
    --limit)
      [[ $# -ge 2 ]] || die "--limit requires a value"
      LIMIT="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --no-update-manifest)
      UPDATE_MANIFEST=0
      shift
      ;;
    --discard-backups)
      KEEP_BACKUPS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -x "$PYTHON_BIN" ]] || die "python executable not found or not executable: $PYTHON_BIN"
[[ -f "$ALIGN_SCRIPT" ]] || die "alignment script not found: $ALIGN_SCRIPT"
[[ -d "$DEMOS_DIR" ]] || die "demos dir not found: $DEMOS_DIR"
[[ "$MODE" == "causal" || "$MODE" == "nearest" ]] || die "--mode must be causal or nearest"
[[ "$LIMIT" =~ ^[0-9]+$ ]] || die "--limit must be a non-negative integer"

if [[ -z "$RUN_ROOT" ]]; then
  RUN_ROOT="${REPO_ROOT}/temp_alignment_test/realign_done_demos_v3_$(date +%Y%m%d_%H%M%S)"
elif [[ "$RUN_ROOT" != /* ]]; then
  RUN_ROOT="${REPO_ROOT}/${RUN_ROOT}"
fi

GENERATED_DIR="${RUN_ROOT}/generated"
BACKUP_DIR="${RUN_ROOT}/backups"
LOG_DIR="${RUN_ROOT}/logs"
SUMMARY_DIR="${RUN_ROOT}/summary"
mkdir -p "$GENERATED_DIR" "$BACKUP_DIR" "$LOG_DIR" "$SUMMARY_DIR"

DONE_LIST="${SUMMARY_DIR}/done_demos.txt"
SUMMARY_CSV="${SUMMARY_DIR}/summary.csv"
SUMMARY_JSON="${SUMMARY_DIR}/summary.json"

ONLY_JSON="$("$PYTHON_BIN" - "${ONLY_DEMOS[@]}" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1:]))
PY
)"

"$PYTHON_BIN" - "$DEMOS_DIR" "$ONLY_JSON" "$LIMIT" > "$DONE_LIST" <<'PY'
import json
import sys
from pathlib import Path

demos_dir = Path(sys.argv[1])
only = set(json.loads(sys.argv[2]))
limit = int(sys.argv[3])
selected = []
for manifest_path in sorted(demos_dir.glob("demo_*/manifest.json")):
    demo_dir = manifest_path.parent
    if only and demo_dir.name not in only:
        continue
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        continue
    if manifest.get("status") == "done":
        selected.append(demo_dir)
    if limit and len(selected) >= limit:
        break
for demo_dir in selected:
    print(demo_dir)
PY

TOTAL="$(wc -l < "$DONE_LIST" | tr -d ' ')"
if [[ "$TOTAL" == "0" ]]; then
  die "no done demos selected under $DEMOS_DIR"
fi

cat > "$SUMMARY_CSV" <<'EOF'
demo,status,reason,sample_count,valid_count,base,base_kind,aligned_dir,backup_dir,log_path
EOF

echo "[INFO] repo_root: $REPO_ROOT"
echo "[INFO] demos_dir: $DEMOS_DIR"
echo "[INFO] run_root: $RUN_ROOT"
echo "[INFO] selected done demos: $TOTAL"
echo "[INFO] base=$BASE mode=$MODE hz=$HZ start_trim_s=$START_TRIM_S end_trim_s=$END_TRIM_S"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "[INFO] dry run; selected demos:"
  sed 's#^#  #' "$DONE_LIST"
  exit 0
fi

processed=0
succeeded=0
failed=0
heartbeat_step=$(( (TOTAL + 19) / 20 ))
[[ "$heartbeat_step" -lt 1 ]] && heartbeat_step=1

restore_demo() {
  local aligned_dir="$1"
  local aligned_backup="$2"
  local manifest_path="$3"
  local manifest_backup="$4"
  rm -rf -- "$aligned_dir"
  if [[ -n "$aligned_backup" && -d "$aligned_backup" ]]; then
    mv -- "$aligned_backup" "$aligned_dir"
  fi
  if [[ -f "$manifest_backup" ]]; then
    cp -- "$manifest_backup" "$manifest_path"
  fi
}

update_manifest_alignment() {
  local manifest_path="$1"
  local aligned_manifest_path="$2"
  local started_ns="$3"
  "$PYTHON_BIN" - "$manifest_path" "$aligned_manifest_path" "$started_ns" <<'PY'
import json
import time
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
aligned_manifest_path = Path(sys.argv[2])
started_ns = int(sys.argv[3])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
aligned = json.loads(aligned_manifest_path.read_text(encoding="utf-8"))
entry = {
    "status": aligned.get("status", "done"),
    "config_path": "aligned/alignment_config.json",
    "index_path": "aligned/aligned_index.npz",
    "manifest_path": "aligned/aligned_manifest.json",
    "report_path": "aligned/alignment_report.md",
    "started_ns": started_ns,
    "finished_ns": time.time_ns(),
    "sample_count": aligned.get("sample_count"),
    "valid_count": aligned.get("valid_count"),
    "base": aligned.get("base"),
    "base_kind": aligned.get("base_kind"),
    "zmq_clock_offsets": aligned.get("zmq_clock_offsets", {}),
    "warnings": aligned.get("warnings", []),
}
if aligned.get("schema_version") is not None:
    entry["schema_version"] = aligned.get("schema_version")
manifest["alignment"] = entry
manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True), encoding="utf-8")
PY
}

while IFS= read -r demo_dir; do
  processed=$((processed + 1))
  demo_name="$(basename "$demo_dir")"
  aligned_dir="${demo_dir}/aligned"
  manifest_path="${demo_dir}/manifest.json"
  tmp_parent="${GENERATED_DIR}/${demo_name}"
  tmp_aligned="${tmp_parent}/aligned"
  aligned_backup=""
  manifest_backup="${BACKUP_DIR}/${demo_name}.manifest.json"
  log_path="${LOG_DIR}/${demo_name}.log"
  started_ns="$("$PYTHON_BIN" -c 'import time; print(time.time_ns())')"

  rm -rf -- "$tmp_parent"
  mkdir -p "$tmp_aligned"

  if "$PYTHON_BIN" "$ALIGN_SCRIPT" \
      --demo-dir "$demo_dir" \
      --repo-root "$REPO_ROOT" \
      --output-dir "$tmp_aligned" \
      --base "$BASE" \
      --mode "$MODE" \
      --hz "$HZ" \
      --start-trim-s "$START_TRIM_S" \
      --end-trim-s "$END_TRIM_S" \
      > "$log_path" 2>&1; then
    cp -- "$manifest_path" "$manifest_backup"
    if [[ -d "$aligned_dir" ]]; then
      aligned_backup="${BACKUP_DIR}/${demo_name}.aligned"
      rm -rf -- "$aligned_backup"
      mv -- "$aligned_dir" "$aligned_backup"
    fi

    if mv -- "$tmp_aligned" "$aligned_dir"; then
      if [[ "$UPDATE_MANIFEST" == "1" ]]; then
        if ! update_manifest_alignment "$manifest_path" "${aligned_dir}/aligned_manifest.json" "$started_ns" >> "$log_path" 2>&1; then
          restore_demo "$aligned_dir" "$aligned_backup" "$manifest_path" "$manifest_backup"
          failed=$((failed + 1))
          echo "${demo_name},failed,manifest_update_failed,,,,,${aligned_dir},${aligned_backup},${log_path}" >> "$SUMMARY_CSV"
          continue
        fi
      fi

      read -r sample_count valid_count base_value base_kind < <("$PYTHON_BIN" - "${aligned_dir}/aligned_manifest.json" <<'PY'
import json
import sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload.get("sample_count", ""), payload.get("valid_count", ""), payload.get("base", ""), payload.get("base_kind", ""))
PY
)
      succeeded=$((succeeded + 1))
      echo "${demo_name},done,ok,${sample_count},${valid_count},${base_value},${base_kind},${aligned_dir},${aligned_backup},${log_path}" >> "$SUMMARY_CSV"
      if [[ "$KEEP_BACKUPS" == "0" ]]; then
        rm -rf -- "$aligned_backup" "$manifest_backup"
      fi
    else
      restore_demo "$aligned_dir" "$aligned_backup" "$manifest_path" "$manifest_backup"
      failed=$((failed + 1))
      echo "${demo_name},failed,replace_failed,,,,,${aligned_dir},${aligned_backup},${log_path}" >> "$SUMMARY_CSV"
    fi
  else
    failed=$((failed + 1))
    echo "${demo_name},failed,alignment_failed,,,,,${aligned_dir},,${log_path}" >> "$SUMMARY_CSV"
  fi

  if (( processed == TOTAL || processed % heartbeat_step == 0 )); then
    echo "[INFO] progress ${processed}/${TOTAL}; succeeded=${succeeded}; failed=${failed}"
  fi
done < "$DONE_LIST"

"$PYTHON_BIN" - "$SUMMARY_CSV" "$SUMMARY_JSON" "$RUN_ROOT" "$TOTAL" "$succeeded" "$failed" <<'PY'
import csv
import json
import sys
from pathlib import Path

summary_csv = Path(sys.argv[1])
summary_json = Path(sys.argv[2])
rows = list(csv.DictReader(summary_csv.open(newline="", encoding="utf-8")))
payload = {
    "run_root": sys.argv[3],
    "selected_count": int(sys.argv[4]),
    "succeeded_count": int(sys.argv[5]),
    "failed_count": int(sys.argv[6]),
    "summary_csv": str(summary_csv),
    "failures": [row for row in rows if row["status"] != "done"],
}
summary_json.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
PY

echo "[INFO] finished; succeeded=${succeeded}; failed=${failed}"
echo "[INFO] summary: $SUMMARY_JSON"
if [[ "$failed" != "0" ]]; then
  exit 1
fi
