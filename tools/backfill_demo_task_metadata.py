#!/usr/bin/env python3
"""Backfill task metadata in legacy MainController demo manifests.

Usage:
    # Preview the planned changes without modifying manifests.
    /usr/bin/python3 tools/backfill_demo_task_metadata.py

    # Apply the backfill after reviewing the dry-run summary.
    /usr/bin/python3 tools/backfill_demo_task_metadata.py --apply

    # Override the demo root, cutoff, and task assignments.
    /usr/bin/python3 tools/backfill_demo_task_metadata.py \
      --demos-root runtime_sessions/demos \
      --cutoff demo_20260618_160911 \
      --early-task 16mm-peg-in-hole \
      --late-task gear-insert-big2small

The default mode is read-only. The tool loads task instructions from
``<repo-root>/TaskInstruction/<task-name>.json`` and only writes manifests when
``--apply`` is specified.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEMOS_ROOT = REPO_ROOT / "runtime_sessions" / "demos"
DEFAULT_CUTOFF = "demo_20260618_160911"
DEFAULT_EARLY_TASK = "16mm-peg-in-hole"
DEFAULT_LATE_TASK = "gear-insert-big2small"
DEMO_NAME_PATTERN = re.compile(r"^demo_\d{8}_\d{6}$")
TASK_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WEIGHT_SUM_ABS_TOL = 1e-9


@dataclass(frozen=True)
class TaskInstructions:
    name: str
    texts: tuple[str, ...]
    weights: tuple[float, ...]


@dataclass(frozen=True)
class PendingManifest:
    demo_dir: Path
    manifest_path: Path
    manifest: dict[str, Any]
    task: TaskInstructions


@dataclass(frozen=True)
class ScanResult:
    pending: tuple[PendingManifest, ...]
    skipped_existing: tuple[Path, ...]
    skipped_missing: tuple[Path, ...]
    task_counts: dict[str, int]


def validate_task_name(task_name: str) -> str:
    if not isinstance(task_name, str):
        raise RuntimeError("task name must be a string")
    if not TASK_NAME_PATTERN.fullmatch(task_name) or ".." in task_name:
        raise RuntimeError(
            "task name must start with an ASCII letter or digit, contain only "
            'ASCII letters, digits, ".", "_", or "-", and must not contain ".."'
        )
    return task_name


def _instruction_text(value: Any, index: int, path: Path) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{path}: instructions[{index}].text must be a string")
    text = value.strip()
    if not text:
        raise RuntimeError(f"{path}: instructions[{index}].text must not be empty")
    if "\x00" in text:
        raise RuntimeError(
            f"{path}: instructions[{index}].text must not contain NUL characters"
        )
    return text


def _instruction_weight(value: Any, index: int, path: Path) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{path}: instructions[{index}].weight must be a number")
    weight = float(value)
    if not math.isfinite(weight):
        raise RuntimeError(f"{path}: instructions[{index}].weight must be finite")
    if weight != -1.0 and not 0.0 < weight < 1.0:
        raise RuntimeError(
            f"{path}: instructions[{index}].weight must be -1 or satisfy 0 < weight < 1"
        )
    return weight


def parse_task_instructions(
    task_name: str,
    payload: Any,
    path: Path,
) -> TaskInstructions:
    validate_task_name(task_name)
    if not isinstance(payload, dict) or set(payload) != {"instructions"}:
        raise RuntimeError(
            f'{path}: root must be an object containing only "instructions"'
        )
    items = payload["instructions"]
    if not isinstance(items, list) or not items:
        raise RuntimeError(f'{path}: "instructions" must be a non-empty array')

    texts: list[str] = []
    raw_weights: list[float] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"text", "weight"}:
            raise RuntimeError(
                f'{path}: instructions[{index}] must contain only "text" and "weight"'
            )
        texts.append(_instruction_text(item["text"], index, path))
        raw_weights.append(_instruction_weight(item["weight"], index, path))

    explicit_sum = sum(weight for weight in raw_weights if weight != -1.0)
    automatic_count = sum(weight == -1.0 for weight in raw_weights)
    if automatic_count:
        if explicit_sum >= 1.0:
            raise RuntimeError(
                f"{path}: explicit weights must sum to less than 1 when automatic "
                "entries are present"
            )
        automatic_weight = (1.0 - explicit_sum) / automatic_count
        weights = tuple(
            automatic_weight if weight == -1.0 else weight
            for weight in raw_weights
        )
    else:
        if not math.isclose(
            explicit_sum,
            1.0,
            rel_tol=0.0,
            abs_tol=WEIGHT_SUM_ABS_TOL,
        ):
            raise RuntimeError(
                f"{path}: explicit weights must sum to 1 when no automatic entries "
                "are present"
            )
        weights = tuple(raw_weights)
    return TaskInstructions(task_name, tuple(texts), weights)


def load_task_instructions(repo_root: Path, task_name: str) -> TaskInstructions:
    task_name = validate_task_name(task_name)
    path = repo_root / "TaskInstruction" / f"{task_name}.json"
    if not path.is_file():
        raise RuntimeError(f"task instruction file does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"task instruction file is not valid UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"task instruction file is not valid JSON: {path}: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot read task instruction file {path}: {exc}") from exc
    return parse_task_instructions(task_name, payload, path)


def _valid_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def validate_existing_metadata(manifest: dict[str, Any], manifest_path: Path) -> bool:
    has_task = "task_name" in manifest
    has_instruction = "language_instruction" in manifest
    if not has_task and not has_instruction:
        return False
    if has_task != has_instruction:
        raise RuntimeError(
            f"{manifest_path}: task_name and language_instruction must either both "
            "exist or both be absent"
        )
    try:
        validate_task_name(manifest["task_name"])
    except RuntimeError as exc:
        raise RuntimeError(f"{manifest_path}: invalid task_name: {exc}") from exc
    if not _valid_nonempty_string(manifest["language_instruction"]):
        raise RuntimeError(
            f"{manifest_path}: language_instruction must be a non-empty string "
            "without NUL characters"
        )
    return True


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{path}: manifest is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path}: manifest is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path}: manifest root must be a JSON object")
    return payload


def scan_manifests(
    demos_root: Path,
    cutoff: str,
    early_task: TaskInstructions,
    late_task: TaskInstructions,
) -> ScanResult:
    if not DEMO_NAME_PATTERN.fullmatch(cutoff):
        raise RuntimeError(
            f"cutoff must match demo_YYYYMMDD_HHMMSS, got {cutoff!r}"
        )
    if not demos_root.is_dir():
        raise RuntimeError(f"demos root does not exist or is not a directory: {demos_root}")

    pending: list[PendingManifest] = []
    skipped_existing: list[Path] = []
    skipped_missing: list[Path] = []
    task_counts = {early_task.name: 0, late_task.name: 0}
    errors: list[str] = []

    for demo_dir in sorted(demos_root.glob("demo_*")):
        if not demo_dir.is_dir():
            continue
        if not DEMO_NAME_PATTERN.fullmatch(demo_dir.name):
            errors.append(
                f"{demo_dir}: directory name must match demo_YYYYMMDD_HHMMSS"
            )
            continue
        manifest_path = demo_dir / "manifest.json"
        if not manifest_path.is_file():
            skipped_missing.append(demo_dir)
            continue
        try:
            manifest = read_manifest(manifest_path)
            if validate_existing_metadata(manifest, manifest_path):
                skipped_existing.append(demo_dir)
                continue
            if "xense_sdk_version" not in manifest:
                raise RuntimeError(
                    f"{manifest_path}: xense_sdk_version is required as the insertion anchor"
                )
        except RuntimeError as exc:
            errors.append(str(exc))
            continue

        task = early_task if demo_dir.name <= cutoff else late_task
        pending.append(PendingManifest(demo_dir, manifest_path, manifest, task))
        task_counts[task.name] += 1

    if errors:
        joined = "\n".join(f"- {error}" for error in errors)
        raise RuntimeError(f"manifest preflight failed:\n{joined}")
    return ScanResult(
        pending=tuple(pending),
        skipped_existing=tuple(skipped_existing),
        skipped_missing=tuple(skipped_missing),
        task_counts=task_counts,
    )


def manifest_with_metadata(
    manifest: dict[str, Any],
    task_name: str,
    language_instruction: str,
) -> dict[str, Any]:
    updated: dict[str, Any] = {}
    inserted = False
    for key, value in manifest.items():
        updated[key] = value
        if key == "xense_sdk_version":
            updated["task_name"] = task_name
            updated["language_instruction"] = language_instruction
            inserted = True
    if not inserted:
        raise RuntimeError("manifest has no xense_sdk_version insertion anchor")
    return updated


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def apply_backfill(
    scan: ScanResult,
    chooser: Callable[..., Sequence[str]] | None = None,
) -> None:
    choose = chooser or random.SystemRandom().choices
    for item in scan.pending:
        instruction = choose(item.task.texts, weights=item.task.weights, k=1)[0]
        updated = manifest_with_metadata(
            item.manifest,
            item.task.name,
            instruction,
        )
        atomic_write_json(item.manifest_path, updated)


def summary_payload(scan: ScanResult, applied: bool) -> dict[str, Any]:
    return {
        "mode": "apply" if applied else "dry-run",
        "pending_or_modified": len(scan.pending),
        "task_counts": scan.task_counts,
        "skipped_existing": len(scan.skipped_existing),
        "skipped_missing_manifest": len(scan.skipped_missing),
        "missing_manifest_demos": [path.name for path in scan.skipped_missing],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--demos-root", type=Path, default=DEFAULT_DEMOS_ROOT)
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--early-task", default=DEFAULT_EARLY_TASK)
    parser.add_argument("--late-task", default=DEFAULT_LATE_TASK)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes; without this flag the tool only performs a dry-run",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        repo_root = args.repo_root.expanduser().resolve()
        demos_root = args.demos_root.expanduser().resolve()
        early_task = load_task_instructions(repo_root, args.early_task)
        late_task = load_task_instructions(repo_root, args.late_task)
        scan = scan_manifests(demos_root, args.cutoff, early_task, late_task)
        if args.apply:
            apply_backfill(scan)
        print(json.dumps(summary_payload(scan, args.apply), indent=2))
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
