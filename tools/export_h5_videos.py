#!/usr/bin/env python3
"""Export tiled RGB videos from DatasetBuilder HDF5 files with FFmpeg NVENC."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Sequence

import hdf5plugin  # noqa: F401 - register HDF5 filters before opening files.
import h5py
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "temp_mp4"
RGB_PATHS = (
    "/observations/rgb/top",
    "/observations/rgb/side",
    "/observations/rgb/wrist",
)
EXPECTED_TOP_SIDE_SHAPE = (480, 640, 3)
EXPECTED_WRIST_SHAPE = (2, 480, 640, 3)
OUTPUT_WIDTH = 1280
OUTPUT_HEIGHT = 960
STDERR_TAIL_BYTES = 16 * 1024


@dataclass(frozen=True)
class ExportOptions:
    output_dir: Path
    overwrite: bool = True
    fps: float | None = None
    ffmpeg: str = "ffmpeg"
    gpu: int = 0
    preset: str = "p5"
    cq: int = 23
    batch_frames: int = 8


@dataclass(frozen=True)
class ExportJob:
    input_path: Path
    output_path: Path


@dataclass(frozen=True)
class ExportResult:
    input_path: str
    output_path: str
    status: str
    frames: int = 0
    fps: float | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None


class ExportError(RuntimeError):
    """A user-facing export failure."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        type=Path,
        help="One .h5 file or a directory recursively containing .h5 files.",
    )
    parser.add_argument(
        "--output-dir",
        "--output_dir",
        dest="output_dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Flat MP4 output directory. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip an input when its target MP4 already exists. Default: overwrite.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Override the HDF5 nominal_hz value.",
    )
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="FFmpeg executable or path. Default: ffmpeg.",
    )
    parser.add_argument("--gpu", type=int, default=0, help="NVENC GPU index.")
    parser.add_argument(
        "--preset",
        choices=tuple(f"p{index}" for index in range(1, 8)),
        default="p5",
        help="NVENC preset. Default: p5.",
    )
    parser.add_argument(
        "--cq",
        type=int,
        default=23,
        help="NVENC constant-quality target, 0 through 51. Default: 23.",
    )
    parser.add_argument(
        "--batch-frames",
        type=int,
        default=8,
        help="HDF5 frames read per batch. Default: 8.",
    )
    return parser


def options_from_args(args: argparse.Namespace) -> ExportOptions:
    if args.fps is not None and (
        not math.isfinite(args.fps) or args.fps <= 0
    ):
        raise ExportError(f"--fps must be finite and positive, got {args.fps!r}")
    if args.gpu < 0:
        raise ExportError(f"--gpu must be non-negative, got {args.gpu}")
    if not 0 <= args.cq <= 51:
        raise ExportError(f"--cq must be between 0 and 51, got {args.cq}")
    if args.batch_frames <= 0:
        raise ExportError(
            f"--batch-frames must be positive, got {args.batch_frames}"
        )
    return ExportOptions(
        output_dir=args.output_dir.expanduser().resolve(),
        overwrite=not args.skip_existing,
        fps=args.fps,
        ffmpeg=args.ffmpeg,
        gpu=args.gpu,
        preset=args.preset,
        cq=args.cq,
        batch_frames=args.batch_frames,
    )


def discover_h5_files(input_path: Path) -> tuple[Path, ...]:
    resolved = input_path.expanduser().resolve()
    if not resolved.exists():
        raise ExportError(f"input path does not exist: {resolved}")
    if resolved.is_file():
        if resolved.suffix != ".h5":
            raise ExportError(f"input file must have a .h5 suffix: {resolved}")
        return (resolved,)
    if not resolved.is_dir():
        raise ExportError(f"input path is not a regular file or directory: {resolved}")
    files = tuple(sorted(path.resolve() for path in resolved.rglob("*.h5")))
    if not files:
        raise ExportError(f"no .h5 files found under: {resolved}")
    return files


def build_jobs(input_path: Path, output_dir: Path) -> tuple[ExportJob, ...]:
    files = discover_h5_files(input_path)
    jobs = tuple(
        ExportJob(path, output_dir / f"{path.stem}.mp4")
        for path in files
    )
    by_output: dict[str, list[Path]] = {}
    for job in jobs:
        by_output.setdefault(job.output_path.name, []).append(job.input_path)
    collisions = {
        name: paths for name, paths in by_output.items() if len(paths) > 1
    }
    if collisions:
        lines = ["flat output filename collisions detected:"]
        for name, paths in sorted(collisions.items()):
            lines.append(f"- {name}")
            lines.extend(f"  - {path}" for path in paths)
        raise ExportError("\n".join(lines))
    return jobs


def resolve_ffmpeg(executable: str) -> str:
    if os.sep in executable:
        path = Path(executable).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ExportError(f"FFmpeg is not executable: {path}")
        return str(path)
    resolved = shutil.which(executable)
    if resolved is None:
        raise ExportError(f"FFmpeg executable not found in PATH: {executable}")
    return resolved


def validate_hevc_nvenc(ffmpeg: str) -> str:
    executable = resolve_ffmpeg(ffmpeg)
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-encoders"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError(f"failed to query FFmpeg encoders: {exc}") from exc
    if result.returncode != 0:
        tail = result.stdout[-STDERR_TAIL_BYTES:].strip()
        raise ExportError(
            f"FFmpeg encoder query failed with code {result.returncode}: {tail}"
        )
    if "hevc_nvenc" not in result.stdout:
        raise ExportError(f"FFmpeg does not provide hevc_nvenc: {executable}")
    return executable


def _validate_dataset(
    h5: h5py.File,
    path: str,
    expected_tail: tuple[int, ...],
    frame_count: int | None,
) -> int:
    if path not in h5:
        raise ExportError(f"missing dataset: {path}")
    dataset = h5[path]
    if dataset.dtype != np.dtype("uint8"):
        raise ExportError(
            f"{path} dtype must be uint8, got {dataset.dtype}"
        )
    if dataset.ndim != len(expected_tail) + 1:
        raise ExportError(
            f"{path} shape must be (T, {', '.join(map(str, expected_tail))}), "
            f"got {dataset.shape}"
        )
    if tuple(dataset.shape[1:]) != expected_tail:
        raise ExportError(
            f"{path} frame shape must be {expected_tail}, got {dataset.shape[1:]}"
        )
    count = int(dataset.shape[0])
    if count <= 0:
        raise ExportError(f"{path} contains no frames")
    if frame_count is not None and count != frame_count:
        raise ExportError(
            f"RGB frame count mismatch: expected {frame_count}, "
            f"{path} has {count}"
        )
    return count


def inspect_h5(h5: h5py.File, fps_override: float | None) -> tuple[int, float]:
    frame_count = _validate_dataset(
        h5,
        RGB_PATHS[0],
        EXPECTED_TOP_SIDE_SHAPE,
        None,
    )
    _validate_dataset(
        h5,
        RGB_PATHS[1],
        EXPECTED_TOP_SIDE_SHAPE,
        frame_count,
    )
    _validate_dataset(
        h5,
        RGB_PATHS[2],
        EXPECTED_WRIST_SHAPE,
        frame_count,
    )
    if fps_override is not None:
        return frame_count, float(fps_override)
    try:
        fps = float(h5.attrs["nominal_hz"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExportError(
            "root attr nominal_hz is required unless --fps is provided"
        ) from exc
    if not math.isfinite(fps) or fps <= 0:
        raise ExportError(f"nominal_hz must be finite and positive, got {fps!r}")
    return frame_count, fps


def iter_mosaic_batches(
    h5: h5py.File,
    frame_count: int,
    batch_frames: int,
) -> Iterator[np.ndarray]:
    top_dataset = h5[RGB_PATHS[0]]
    side_dataset = h5[RGB_PATHS[1]]
    wrist_dataset = h5[RGB_PATHS[2]]
    for start in range(0, frame_count, batch_frames):
        stop = min(start + batch_frames, frame_count)
        top = np.asarray(top_dataset[start:stop])
        side = np.asarray(side_dataset[start:stop])
        wrist = np.asarray(wrist_dataset[start:stop])
        frames = np.empty(
            (stop - start, OUTPUT_HEIGHT, OUTPUT_WIDTH, 3),
            dtype=np.uint8,
        )
        frames[:, :480, :640] = top
        frames[:, :480, 640:] = side
        frames[:, 480:, :640] = wrist[:, 0]
        frames[:, 480:, 640:] = wrist[:, 1]
        yield frames


def ffmpeg_command(
    ffmpeg: str,
    output_path: Path,
    fps: float,
    options: ExportOptions,
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s:v",
        f"{OUTPUT_WIDTH}x{OUTPUT_HEIGHT}",
        "-r",
        format(fps, ".12g"),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "hevc_nvenc",
        "-gpu",
        str(options.gpu),
        "-preset",
        options.preset,
        "-tune",
        "hq",
        "-rc",
        "vbr",
        "-cq",
        str(options.cq),
        "-b:v",
        "0",
        "-spatial_aq",
        "1",
        "-aq-strength",
        "8",
        "-pix_fmt",
        "yuv420p",
        "-tag:v",
        "hvc1",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    ]


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _stderr_tail(stderr_file: object) -> str:
    stderr_file.seek(0, os.SEEK_END)
    size = stderr_file.tell()
    stderr_file.seek(max(0, size - STDERR_TAIL_BYTES))
    return stderr_file.read().decode("utf-8", errors="replace").strip()


def export_job(
    job: ExportJob,
    options: ExportOptions,
    *,
    validated_ffmpeg: str | None = None,
) -> ExportResult:
    if job.output_path.exists() and not options.overwrite:
        return ExportResult(
            input_path=str(job.input_path),
            output_path=str(job.output_path),
            status="skipped",
        )

    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = job.output_path.with_name(
        f".{job.output_path.stem}.{os.getpid()}.tmp.mp4"
    )
    temporary_path.unlink(missing_ok=True)
    ffmpeg = validated_ffmpeg or validate_hevc_nvenc(options.ffmpeg)
    started = time.perf_counter()
    process: subprocess.Popen[bytes] | None = None
    try:
        with h5py.File(job.input_path, "r") as h5:
            frame_count, fps = inspect_h5(h5, options.fps)
            command = ffmpeg_command(ffmpeg, temporary_path, fps, options)
            with tempfile.TemporaryFile() as stderr_file:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    start_new_session=True,
                )
                if process.stdin is None:
                    raise ExportError("failed to open FFmpeg stdin")
                try:
                    for batch in iter_mosaic_batches(
                        h5,
                        frame_count,
                        options.batch_frames,
                    ):
                        process.stdin.write(memoryview(batch).cast("B"))
                    process.stdin.close()
                    return_code = process.wait()
                except BrokenPipeError as exc:
                    try:
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                    return_code = process.wait()
                    tail = _stderr_tail(stderr_file)
                    raise ExportError(
                        f"FFmpeg pipe closed with code {return_code}: {tail}"
                    ) from exc
                except BaseException:
                    try:
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                    _terminate_process_group(process)
                    raise
                if return_code != 0:
                    tail = _stderr_tail(stderr_file)
                    raise ExportError(
                        f"FFmpeg exited with code {return_code}: {tail}"
                    )
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise ExportError("FFmpeg completed without producing a non-empty MP4")
        os.replace(temporary_path, job.output_path)
        return ExportResult(
            input_path=str(job.input_path),
            output_path=str(job.output_path),
            status="succeeded",
            frames=frame_count,
            fps=fps,
            elapsed_seconds=time.perf_counter() - started,
        )
    except BaseException:
        if process is not None:
            _terminate_process_group(process)
        temporary_path.unlink(missing_ok=True)
        raise


def run_jobs(
    jobs: Sequence[ExportJob],
    options: ExportOptions,
) -> tuple[ExportResult, ...]:
    ffmpeg = validate_hevc_nvenc(options.ffmpeg)
    results: list[ExportResult] = []
    for job in jobs:
        try:
            result = export_job(job, options, validated_ffmpeg=ffmpeg)
        except Exception as exc:
            result = ExportResult(
                input_path=str(job.input_path),
                output_path=str(job.output_path),
                status="failed",
                error=str(exc),
            )
        results.append(result)
    return tuple(results)


def summarize(results: Sequence[ExportResult]) -> dict[str, object]:
    return {
        "processed": len(results),
        "succeeded": sum(result.status == "succeeded" for result in results),
        "failed": sum(result.status == "failed" for result in results),
        "skipped": sum(result.status == "skipped" for result in results),
        "results": [asdict(result) for result in results],
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        options = options_from_args(args)
        jobs = build_jobs(args.input, options.output_dir)
        results = run_jobs(jobs, options)
        summary = summarize(results)
        print(json.dumps({key: summary[key] for key in (
            "processed",
            "succeeded",
            "failed",
            "skipped",
        )}))
        for result in results:
            if result.status == "failed":
                print(
                    f"[ERROR] {result.input_path}: {result.error}",
                    file=sys.stderr,
                )
        return 1 if summary["failed"] else 0
    except (ExportError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
