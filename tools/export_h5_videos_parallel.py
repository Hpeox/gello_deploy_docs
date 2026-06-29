#!/usr/bin/env python3
"""Coordinate file-backed workers for parallel HDF5 video export."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

try:
    from export_h5_videos import (
        ExportJob,
        ExportOptions,
        build_jobs,
        build_parser as build_export_parser,
        export_job,
        options_from_args,
        validate_hevc_nvenc,
    )
except ModuleNotFoundError:
    from tools.export_h5_videos import (
        ExportJob,
        ExportOptions,
        build_jobs,
        build_parser as build_export_parser,
        export_job,
        options_from_args,
        validate_hevc_nvenc,
    )


DEFAULT_CPU_PAIRS = ((8, 9), (10, 11), (12, 13), (14, 15))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-cpus")
    validate.add_argument("--workers", type=int, required=True)
    validate.add_argument("--cpu-pairs", default=None)

    output_dir = subparsers.add_parser("output-dir")
    output_dir.add_argument("export_args", nargs=argparse.REMAINDER)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--run-dir", type=Path, required=True)
    prepare.add_argument("export_args", nargs=argparse.REMAINDER)

    worker = subparsers.add_parser("worker")
    worker.add_argument("--run-dir", type=Path, required=True)
    worker.add_argument("--worker-id", required=True)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--run-dir", type=Path, required=True)
    recover.add_argument("--worker-id", required=True)
    recover.add_argument("--exit-code", type=int, required=True)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--run-dir", type=Path, required=True)
    return parser


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_cpu_pairs(value: str | None) -> tuple[tuple[int, int], ...]:
    if value is None:
        return DEFAULT_CPU_PAIRS
    pairs: list[tuple[int, int]] = []
    for raw_pair in value.split(","):
        fields = raw_pair.strip().replace("-", ":").split(":")
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            raise RuntimeError(
                "--cpu-pairs must look like 8-9,10-11,12-13,14-15"
            )
        pair = (int(fields[0]), int(fields[1]))
        if pair[0] == pair[1]:
            raise RuntimeError(f"CPU pair contains the same CPU twice: {raw_pair}")
        pairs.append(pair)
    if not pairs:
        raise RuntimeError("--cpu-pairs must contain at least one pair")
    flattened = [cpu for pair in pairs for cpu in pair]
    if len(flattened) != len(set(flattened)):
        raise RuntimeError("--cpu-pairs contains duplicate CPUs")
    return tuple(pairs)


def parse_lscpu_rows(text: str) -> dict[int, tuple[int, int, bool]]:
    rows: dict[int, tuple[int, int, bool]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(",")
        if len(fields) != 4:
            raise RuntimeError(f"unexpected lscpu row: {raw_line!r}")
        cpu, core, socket = (int(value) for value in fields[:3])
        rows[cpu] = (core, socket, fields[3].strip().upper() == "Y")
    return rows


def validate_cpu_pairs(
    workers: int,
    pairs: Sequence[tuple[int, int]],
    rows: dict[int, tuple[int, int, bool]],
) -> tuple[str, ...]:
    if workers <= 0:
        raise RuntimeError(f"--workers must be positive, got {workers}")
    if workers > len(pairs):
        raise RuntimeError(
            f"--workers={workers} exceeds available CPU pairs={len(pairs)}"
        )
    selected: list[str] = []
    for first, second in pairs[:workers]:
        missing = [cpu for cpu in (first, second) if cpu not in rows]
        if missing:
            raise RuntimeError(f"CPU pair {first}-{second} is missing CPUs: {missing}")
        first_core, first_socket, first_online = rows[first]
        second_core, second_socket, second_online = rows[second]
        if not first_online or not second_online:
            raise RuntimeError(f"CPU pair {first}-{second} contains an offline CPU")
        if (first_core, first_socket) != (second_core, second_socket):
            raise RuntimeError(
                f"CPU pair {first}-{second} does not share one physical core: "
                f"{first_core}/{first_socket} != {second_core}/{second_socket}"
            )
        selected.append(f"{first},{second}")
    return tuple(selected)


def validate_cpus(workers: int, cpu_pairs: str | None) -> int:
    result = subprocess.run(
        ["lscpu", "-p=CPU,CORE,SOCKET,ONLINE"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    rows = parse_lscpu_rows(result.stdout)
    for pair in validate_cpu_pairs(workers, parse_cpu_pairs(cpu_pairs), rows):
        print(pair)
    return 0


def _export_args(args: Sequence[str]) -> argparse.Namespace:
    values = list(args)
    if values and values[0] == "--":
        values = values[1:]
    return build_export_parser().parse_args(values)


def resolve_export_output_dir(raw_export_args: Sequence[str]) -> Path:
    export_args = _export_args(raw_export_args)
    return options_from_args(export_args).output_dir


def require_empty_run_dir(run_dir: Path) -> None:
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"--run-dir must be absent or empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)


def prepare_run(run_dir: Path, raw_export_args: Sequence[str]) -> int:
    run_dir = run_dir.resolve()
    export_args = _export_args(raw_export_args)
    options = options_from_args(export_args)
    jobs = build_jobs(export_args.input, options.output_dir)
    ffmpeg = validate_hevc_nvenc(options.ffmpeg)
    require_empty_run_dir(run_dir)
    for name in ("pending", "in_progress", "results", "logs/tasks", "logs/workers"):
        (run_dir / name).mkdir(parents=True, exist_ok=True)

    snapshot = []
    for index, job in enumerate(jobs):
        task = {
            "task_id": f"{index:08d}",
            "input_path": str(job.input_path),
            "output_path": str(job.output_path),
        }
        snapshot.append(task)
        atomic_write_json(
            run_dir / "pending" / f"{task['task_id']}.json",
            task,
        )
    config = {
        "ffmpeg": ffmpeg,
        "options": {
            **asdict(options),
            "output_dir": str(options.output_dir),
        },
    }
    atomic_write_json(run_dir / "config.json", config)
    atomic_write_json(run_dir / "snapshot.json", snapshot)
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "snapshot_count": len(snapshot),
                "output_dir": str(options.output_dir),
            }
        )
    )
    return 0


def load_options(config: dict[str, Any]) -> ExportOptions:
    values = dict(config["options"])
    values["output_dir"] = Path(values["output_dir"])
    return ExportOptions(**values)


def claim_next(run_dir: Path, worker_dir: Path) -> Path | None:
    for pending_path in sorted((run_dir / "pending").glob("*.json")):
        claimed_path = worker_dir / pending_path.name
        try:
            os.replace(pending_path, claimed_path)
        except FileNotFoundError:
            continue
        return claimed_path
    return None


def task_log(result: dict[str, Any]) -> str:
    lines = [
        f"status={result['status']}",
        f"input_path={result['input_path']}",
        f"output_path={result['output_path']}",
    ]
    if result.get("frames"):
        lines.append(f"frames={result['frames']}")
    if result.get("fps") is not None:
        lines.append(f"fps={result['fps']}")
    if result.get("elapsed_seconds"):
        lines.append(f"elapsed_seconds={result['elapsed_seconds']:.6f}")
    if result.get("error"):
        lines.append(f"error={result['error']}")
    return "\n".join(lines) + "\n"


def run_worker(run_dir: Path, worker_id: str) -> int:
    def interrupt_worker(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, interrupt_worker)
        signal.signal(signal.SIGINT, interrupt_worker)
    run_dir = run_dir.resolve()
    config = read_json(run_dir / "config.json")
    options = load_options(config)
    worker_dir = run_dir / "in_progress" / worker_id
    worker_dir.mkdir(parents=True, exist_ok=True)
    if any(worker_dir.glob("*.json")):
        raise RuntimeError(f"worker has an unrecovered claim: {worker_dir}")

    while True:
        claimed_path = claim_next(run_dir, worker_dir)
        if claimed_path is None:
            return 0
        task = read_json(claimed_path)
        job = ExportJob(
            input_path=Path(task["input_path"]),
            output_path=Path(task["output_path"]),
        )
        try:
            exported = export_job(
                job,
                options,
                validated_ffmpeg=config["ffmpeg"],
            )
            result = {
                "task_id": task["task_id"],
                "worker_id": worker_id,
                **asdict(exported),
            }
        except Exception as exc:
            result = {
                "task_id": task["task_id"],
                "worker_id": worker_id,
                "input_path": task["input_path"],
                "output_path": task["output_path"],
                "status": "failed",
                "frames": 0,
                "fps": None,
                "elapsed_seconds": 0.0,
                "error": str(exc),
            }
        atomic_write_json(
            run_dir / "results" / f"{task['task_id']}.json",
            result,
        )
        atomic_write_text(
            run_dir / "logs/tasks" / f"{task['task_id']}.log",
            task_log(result),
        )
        claimed_path.unlink()


def recover_worker(
    run_dir: Path,
    worker_id: str,
    exit_code: int,
) -> int:
    run_dir = run_dir.resolve()
    worker_dir = run_dir / "in_progress" / worker_id
    claims = sorted(worker_dir.glob("*.json")) if worker_dir.exists() else []
    if len(claims) > 1:
        raise RuntimeError(f"worker {worker_id} has multiple claims: {claims}")
    for claimed_path in claims:
        task = read_json(claimed_path)
        result = {
            "task_id": task["task_id"],
            "worker_id": worker_id,
            "input_path": task["input_path"],
            "output_path": task["output_path"],
            "status": "failed",
            "frames": 0,
            "fps": None,
            "elapsed_seconds": 0.0,
            "error": f"worker {worker_id} exited with code {exit_code}",
        }
        atomic_write_json(
            run_dir / "results" / f"{task['task_id']}.json",
            result,
        )
        atomic_write_text(
            run_dir / "logs/tasks" / f"{task['task_id']}.log",
            task_log(result),
        )
        claimed_path.unlink()
    return 0


def finalize_run(run_dir: Path) -> int:
    run_dir = run_dir.resolve()
    snapshot = read_json(run_dir / "snapshot.json")
    expected_ids = [task["task_id"] for task in snapshot]
    result_paths = sorted((run_dir / "results").glob("*.json"))
    actual_ids = [path.stem for path in result_paths]
    pending = sorted((run_dir / "pending").glob("*.json"))
    in_progress = sorted((run_dir / "in_progress").glob("*/*.json"))
    errors = []
    if actual_ids != expected_ids:
        errors.append(
            "result coverage mismatch: "
            f"missing={sorted(set(expected_ids) - set(actual_ids))}, "
            f"unexpected={sorted(set(actual_ids) - set(expected_ids))}"
        )
    if pending:
        errors.append(f"pending tasks remain: {[path.name for path in pending]}")
    if in_progress:
        errors.append(
            f"in-progress tasks remain: {[str(path) for path in in_progress]}"
        )
    if errors:
        atomic_write_json(run_dir / "finalize_error.json", {"errors": errors})
        raise RuntimeError("; ".join(errors))

    results = [read_json(path) for path in result_paths]
    summary = {
        "processed": len(results),
        "succeeded": sum(item["status"] == "succeeded" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "results": results,
    }
    atomic_write_json(run_dir / "summary.json", summary)
    print(
        json.dumps(
            {
                key: summary[key]
                for key in ("processed", "succeeded", "failed", "skipped")
            }
        )
    )
    if summary["failed"]:
        failed = [item for item in results if item["status"] == "failed"]
        for item in failed[:10]:
            print(
                f"[ERROR] {item['input_path']}: {item['error']}",
                file=sys.stderr,
            )
        if len(failed) > 10:
            print(
                f"[ERROR] {len(failed) - 10} additional failures; "
                f"see {run_dir / 'summary.json'}",
                file=sys.stderr,
            )
    return 1 if summary["failed"] else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-cpus":
        return validate_cpus(args.workers, args.cpu_pairs)
    if args.command == "output-dir":
        print(resolve_export_output_dir(args.export_args))
        return 0
    if args.command == "prepare":
        return prepare_run(args.run_dir, args.export_args)
    if args.command == "worker":
        return run_worker(args.run_dir, args.worker_id)
    if args.command == "recover":
        return recover_worker(args.run_dir, args.worker_id, args.exit_code)
    if args.command == "finalize":
        return finalize_run(args.run_dir)
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2)
