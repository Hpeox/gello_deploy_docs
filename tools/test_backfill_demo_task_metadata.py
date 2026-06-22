from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.backfill_demo_task_metadata import (
    TaskInstructions,
    apply_backfill,
    manifest_with_metadata,
    parse_task_instructions,
    scan_manifests,
)


EARLY_TASK = TaskInstructions("early-task", ("early one", "early two"), (0.75, 0.25))
LATE_TASK = TaskInstructions("late-task", ("late one",), (1.0,))


def write_manifest(demo_dir: Path, payload: dict) -> Path:
    demo_dir.mkdir(parents=True)
    path = demo_dir / "manifest.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def base_manifest() -> dict:
    return {
        "status": "done",
        "run_id": "run_test",
        "xense_sdk_version": "2.0",
        "rosbag_uri": "rosbag",
    }


def test_parse_task_instructions_resolves_automatic_weights(tmp_path):
    task = parse_task_instructions(
        "test-task",
        {
            "instructions": [
                {"text": "primary", "weight": 0.6},
                {"text": "second", "weight": -1},
                {"text": "third", "weight": -1},
            ]
        },
        tmp_path / "test-task.json",
    )

    assert task.texts == ("primary", "second", "third")
    assert task.weights == pytest.approx((0.6, 0.2, 0.2))


def test_scan_uses_inclusive_cutoff_and_skips_existing_and_missing(tmp_path):
    demos_root = tmp_path / "demos"
    before = write_manifest(demos_root / "demo_20260618_160910", base_manifest())
    cutoff = write_manifest(demos_root / "demo_20260618_160911", base_manifest())
    after = write_manifest(demos_root / "demo_20260618_160912", base_manifest())
    existing_payload = {
        **base_manifest(),
        "task_name": "already-tagged",
        "language_instruction": "Keep this value.",
    }
    existing = write_manifest(
        demos_root / "demo_20260618_160913",
        existing_payload,
    )
    missing = demos_root / "demo_20260618_160914"
    missing.mkdir(parents=True)

    scan = scan_manifests(
        demos_root,
        "demo_20260618_160911",
        EARLY_TASK,
        LATE_TASK,
    )

    assert [item.manifest_path for item in scan.pending] == [before, cutoff, after]
    assert [item.task.name for item in scan.pending] == [
        "early-task",
        "early-task",
        "late-task",
    ]
    assert scan.task_counts == {"early-task": 2, "late-task": 1}
    assert scan.skipped_existing == (existing.parent,)
    assert scan.skipped_missing == (missing,)


@pytest.mark.parametrize(
    "metadata",
    [
        {"task_name": "partial"},
        {"language_instruction": "partial"},
        {"task_name": "", "language_instruction": "instruction"},
        {"task_name": "task", "language_instruction": " \n"},
        {"task_name": "task/name", "language_instruction": "instruction"},
    ],
)
def test_scan_rejects_partial_or_invalid_existing_metadata(tmp_path, metadata):
    demos_root = tmp_path / "demos"
    write_manifest(
        demos_root / "demo_20260618_160911",
        {**base_manifest(), **metadata},
    )

    with pytest.raises(RuntimeError, match="manifest preflight failed"):
        scan_manifests(
            demos_root,
            "demo_20260618_160911",
            EARLY_TASK,
            LATE_TASK,
        )


def test_preflight_error_prevents_any_write(tmp_path):
    demos_root = tmp_path / "demos"
    valid_path = write_manifest(
        demos_root / "demo_20260618_160910",
        base_manifest(),
    )
    invalid_path = demos_root / "demo_20260618_160911" / "manifest.json"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_text("{invalid", encoding="utf-8")
    original = valid_path.read_bytes()

    with pytest.raises(RuntimeError, match="manifest preflight failed"):
        scan_manifests(
            demos_root,
            "demo_20260618_160911",
            EARLY_TASK,
            LATE_TASK,
        )

    assert valid_path.read_bytes() == original


def test_apply_samples_with_resolved_weights_and_preserves_field_order(tmp_path):
    demos_root = tmp_path / "demos"
    manifest_path = write_manifest(
        demos_root / "demo_20260618_160911",
        base_manifest(),
    )
    scan = scan_manifests(
        demos_root,
        "demo_20260618_160911",
        EARLY_TASK,
        LATE_TASK,
    )
    calls = []

    def choose(population, *, weights, k):
        calls.append((population, weights, k))
        return [population[1]]

    apply_backfill(scan, chooser=choose)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert calls == [(EARLY_TASK.texts, EARLY_TASK.weights, 1)]
    assert manifest["task_name"] == "early-task"
    assert manifest["language_instruction"] == "early two"
    assert manifest["rosbag_uri"] == "rosbag"
    keys = list(manifest)
    assert keys[keys.index("xense_sdk_version") + 1] == "task_name"
    assert keys[keys.index("task_name") + 1] == "language_instruction"


def test_dry_run_scan_does_not_sample_or_write(tmp_path):
    demos_root = tmp_path / "demos"
    manifest_path = write_manifest(
        demos_root / "demo_20260618_160911",
        base_manifest(),
    )
    original = manifest_path.read_bytes()

    scan = scan_manifests(
        demos_root,
        "demo_20260618_160911",
        EARLY_TASK,
        LATE_TASK,
    )

    assert len(scan.pending) == 1
    assert manifest_path.read_bytes() == original


def test_manifest_with_metadata_requires_insertion_anchor():
    with pytest.raises(RuntimeError, match="insertion anchor"):
        manifest_with_metadata(
            {"status": "done"},
            "task",
            "instruction",
        )
