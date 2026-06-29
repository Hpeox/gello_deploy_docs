from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tools import export_h5_videos_parallel as parallel
from tools.export_h5_videos import ExportResult


def topology_rows() -> dict[int, tuple[int, int, bool]]:
    return {
        cpu: ((cpu - 8) // 2 + 4, 0, True)
        for cpu in range(8, 16)
    }


def test_default_cpu_pairs_are_validated_as_smt_siblings():
    assert parallel.validate_cpu_pairs(
        4,
        parallel.DEFAULT_CPU_PAIRS,
        topology_rows(),
    ) == ("8,9", "10,11", "12,13", "14,15")


def test_cpu_pair_parser_and_validation_reject_bad_topology():
    assert parallel.parse_cpu_pairs("8-9,10:11") == ((8, 9), (10, 11))
    with pytest.raises(RuntimeError, match="duplicate CPUs"):
        parallel.parse_cpu_pairs("8-9,9-10")
    with pytest.raises(RuntimeError, match="exceeds available"):
        parallel.validate_cpu_pairs(5, parallel.DEFAULT_CPU_PAIRS, topology_rows())

    rows = topology_rows()
    rows[9] = (99, 0, True)
    with pytest.raises(RuntimeError, match="does not share"):
        parallel.validate_cpu_pairs(1, parallel.DEFAULT_CPU_PAIRS, rows)


def test_default_run_location_resolves_under_export_output_dir(tmp_path):
    input_path = tmp_path / "demo.h5"
    output_dir = tmp_path / "videos"

    assert parallel.resolve_export_output_dir(
        [
            str(input_path),
            "--output-dir",
            str(output_dir),
        ]
    ) == output_dir.resolve()
    assert parallel.resolve_export_output_dir([str(input_path)]) == (
        parallel.build_export_parser().get_default("output_dir").resolve()
    )


def prepare_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int = 7,
) -> Path:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for index in range(count):
        (input_dir / f"demo_{index}.h5").touch()
    run_dir = tmp_path / "run"
    monkeypatch.setattr(parallel, "validate_hevc_nvenc", lambda value: value)
    assert parallel.prepare_run(
        run_dir,
        [
            str(input_dir),
            "--output-dir",
            str(tmp_path / "output"),
            "--ffmpeg",
            "/fake/ffmpeg",
        ],
    ) == 0
    return run_dir


def test_prepare_creates_fixed_snapshot(tmp_path, monkeypatch):
    run_dir = prepare_fixture(tmp_path, monkeypatch, count=3)

    snapshot = json.loads(
        (run_dir / "snapshot.json").read_text(encoding="utf-8")
    )
    assert [item["task_id"] for item in snapshot] == [
        "00000000",
        "00000001",
        "00000002",
    ]
    assert len(list((run_dir / "pending").glob("*.json"))) == 3


def test_dynamic_workers_cover_snapshot_exactly_once(tmp_path, monkeypatch):
    run_dir = prepare_fixture(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_export(job, options, *, validated_ffmpeg):
        calls.append(job.input_path.name)
        return ExportResult(
            input_path=str(job.input_path),
            output_path=str(job.output_path),
            status="succeeded",
            frames=10,
            fps=30.0,
            elapsed_seconds=0.1,
        )

    monkeypatch.setattr(parallel, "export_job", fake_export)
    with ThreadPoolExecutor(max_workers=4) as executor:
        statuses = list(
            executor.map(
                lambda worker_id: parallel.run_worker(run_dir, worker_id),
                [f"worker_{index}" for index in range(4)],
            )
        )

    assert statuses == [0, 0, 0, 0]
    assert len(calls) == 7
    assert len(set(calls)) == 7
    assert not list((run_dir / "pending").glob("*.json"))
    assert parallel.finalize_run(run_dir) == 0
    summary = json.loads(
        (run_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["succeeded"] == 7
    assert len(list((run_dir / "logs/tasks").glob("*.log"))) == 7


def test_task_failure_is_recorded_without_stopping_worker(tmp_path, monkeypatch):
    run_dir = prepare_fixture(tmp_path, monkeypatch, count=2)

    def fake_export(job, options, *, validated_ffmpeg):
        if job.input_path.name == "demo_0.h5":
            raise RuntimeError("synthetic failure")
        return ExportResult(
            input_path=str(job.input_path),
            output_path=str(job.output_path),
            status="skipped",
        )

    monkeypatch.setattr(parallel, "export_job", fake_export)
    assert parallel.run_worker(run_dir, "worker_0") == 0
    assert parallel.finalize_run(run_dir) == 1
    summary = json.loads(
        (run_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["failed"] == 1
    assert summary["skipped"] == 1


def test_recover_marks_claimed_task_failed(tmp_path, monkeypatch):
    run_dir = prepare_fixture(tmp_path, monkeypatch, count=1)
    pending = next((run_dir / "pending").glob("*.json"))
    worker_dir = run_dir / "in_progress" / "worker_0"
    worker_dir.mkdir()
    pending.replace(worker_dir / pending.name)

    assert parallel.recover_worker(run_dir, "worker_0", 143) == 0
    result = json.loads(
        (run_dir / "results" / "00000000.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "failed"
    assert "exited with code 143" in result["error"]


def test_finalize_rejects_incomplete_coverage(tmp_path, monkeypatch):
    run_dir = prepare_fixture(tmp_path, monkeypatch, count=1)
    with pytest.raises(RuntimeError, match="coverage mismatch"):
        parallel.finalize_run(run_dir)
    assert (run_dir / "finalize_error.json").exists()
