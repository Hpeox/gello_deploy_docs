from __future__ import annotations

import os
from pathlib import Path

import h5py
import numpy as np
import pytest

from tools.export_h5_videos import (
    ExportError,
    ExportJob,
    ExportOptions,
    build_jobs,
    export_job,
    ffmpeg_command,
    inspect_h5,
    iter_mosaic_batches,
)


def write_valid_h5(path: Path, frames: int = 2, fps: float = 30.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as h5:
        h5.attrs["nominal_hz"] = fps
        rgb = h5.create_group("/observations/rgb")
        rgb.create_dataset(
            "top",
            data=np.zeros((frames, 480, 640, 3), dtype=np.uint8),
            chunks=(1, 480, 640, 3),
        )
        rgb.create_dataset(
            "side",
            data=np.ones((frames, 480, 640, 3), dtype=np.uint8),
            chunks=(1, 480, 640, 3),
        )
        wrist = np.empty((frames, 2, 480, 640, 3), dtype=np.uint8)
        wrist[:, 0] = 2
        wrist[:, 1] = 3
        rgb.create_dataset(
            "wrist",
            data=wrist,
            chunks=(1, 2, 480, 640, 3),
        )


def write_fake_ffmpeg(path: Path, *, fail: bool = False) -> None:
    code = """#!/usr/bin/env python3
import pathlib
import sys

if "-encoders" in sys.argv:
    print(" V....D hevc_nvenc NVIDIA NVENC hevc encoder")
    raise SystemExit(0)

sys.stdin.buffer.read()
if FAIL:
    print("synthetic encoder failure", file=sys.stderr)
    raise SystemExit(7)
pathlib.Path(sys.argv[-1]).write_bytes(b"fake-mp4")
"""
    path.write_text(
        code.replace("FAIL", "True" if fail else "False"),
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_build_jobs_supports_file_and_recursive_directory(tmp_path):
    first = tmp_path / "a" / "demo_a.h5"
    second = tmp_path / "b" / "demo_b.h5"
    first.parent.mkdir()
    second.parent.mkdir()
    first.touch()
    second.touch()
    output_dir = tmp_path / "output"

    assert [job.input_path for job in build_jobs(first, output_dir)] == [
        first.resolve()
    ]
    assert [job.output_path.name for job in build_jobs(tmp_path, output_dir)] == [
        "demo_a.mp4",
        "demo_b.mp4",
    ]


def test_build_jobs_rejects_empty_directory_and_flat_name_collision(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ExportError, match="no .h5 files"):
        build_jobs(empty, tmp_path / "output")

    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "one" / "same.h5").touch()
    (tmp_path / "two" / "same.h5").touch()
    with pytest.raises(ExportError, match="filename collisions"):
        build_jobs(tmp_path, tmp_path / "output")


def test_inspection_and_mosaic_layout(tmp_path):
    path = tmp_path / "demo.h5"
    write_valid_h5(path)
    with h5py.File(path, "r") as h5:
        assert inspect_h5(h5, None) == (2, 30.0)
        batches = list(iter_mosaic_batches(h5, 2, 8))

    assert len(batches) == 1
    frames = batches[0]
    assert frames.shape == (2, 960, 1280, 3)
    assert np.all(frames[:, :480, :640] == 0)
    assert np.all(frames[:, :480, 640:] == 1)
    assert np.all(frames[:, 480:, :640] == 2)
    assert np.all(frames[:, 480:, 640:] == 3)


def test_inspection_rejects_schema_and_accepts_fps_override(tmp_path):
    path = tmp_path / "demo.h5"
    write_valid_h5(path)
    with h5py.File(path, "r+") as h5:
        del h5.attrs["nominal_hz"]
        assert inspect_h5(h5, 25.0) == (2, 25.0)
        del h5["/observations/rgb/side"]
        with pytest.raises(ExportError, match="missing dataset"):
            inspect_h5(h5, 25.0)


def test_ffmpeg_command_contains_nvenc_quality_settings(tmp_path):
    options = ExportOptions(output_dir=tmp_path, gpu=1, preset="p6", cq=19)
    command = ffmpeg_command(
        "/usr/bin/ffmpeg",
        tmp_path / "out.mp4",
        30.0,
        options,
    )

    assert command[command.index("-c:v") + 1] == "hevc_nvenc"
    assert command[command.index("-gpu") + 1] == "1"
    assert command[command.index("-preset") + 1] == "p6"
    assert command[command.index("-cq") + 1] == "19"
    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert command[-3:] == ["-f", "mp4", str(tmp_path / "out.mp4")]


def test_export_uses_atomic_replacement_and_skip_existing(tmp_path):
    h5_path = tmp_path / "demo.h5"
    output_path = tmp_path / "output" / "demo.mp4"
    ffmpeg = tmp_path / "fake_ffmpeg"
    write_valid_h5(h5_path)
    write_fake_ffmpeg(ffmpeg)
    output_path.parent.mkdir()
    output_path.write_bytes(b"old")
    options = ExportOptions(output_dir=output_path.parent, ffmpeg=str(ffmpeg))
    job = ExportJob(h5_path, output_path)

    result = export_job(job, options)

    assert result.status == "succeeded"
    assert result.frames == 2
    assert output_path.read_bytes() == b"fake-mp4"
    assert not list(output_path.parent.glob(".*.tmp.mp4"))

    skipped = export_job(
        job,
        ExportOptions(
            output_dir=output_path.parent,
            overwrite=False,
            ffmpeg=str(ffmpeg),
        ),
    )
    assert skipped.status == "skipped"


def test_export_failure_preserves_existing_output_and_bounds_cleanup(tmp_path):
    h5_path = tmp_path / "demo.h5"
    output_path = tmp_path / "output" / "demo.mp4"
    ffmpeg = tmp_path / "fake_ffmpeg"
    write_valid_h5(h5_path)
    write_fake_ffmpeg(ffmpeg, fail=True)
    output_path.parent.mkdir()
    output_path.write_bytes(b"keep-me")

    with pytest.raises(ExportError, match="synthetic encoder failure"):
        export_job(
            ExportJob(h5_path, output_path),
            ExportOptions(output_dir=output_path.parent, ffmpeg=str(ffmpeg)),
        )

    assert output_path.read_bytes() == b"keep-me"
    assert not list(output_path.parent.glob(".*.tmp.mp4"))


def test_fake_ffmpeg_is_executable(tmp_path):
    ffmpeg = tmp_path / "fake_ffmpeg"
    write_fake_ffmpeg(ffmpeg)
    assert os.access(ffmpeg, os.X_OK)
