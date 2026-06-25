#!/usr/bin/env python3
"""One-time in-place remapping tool for gripper gPO HDF5 datasets."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hdf5plugin  # noqa: F401 - register filters before h5py opens a file.
import h5py
import numpy as np


DATASET_ROOT = Path("/data/external/DATASET/16mm-peg-in-hole")
GPO_DATASET_PATH = "/observations/gripper/gPO"
ANCHOR = 3
SOURCE_P95_THRESHOLD = 170
TARGET_PLATFORM_AVG = 158.23140495867767
OFFSET_SIGMA = 2.0
OFFSET_LIMIT = 5.0
RANDOM_SEED = 20260625
CONFIRMATION_TEXT = "REMAP GPO DATASETS"


@dataclass(frozen=True)
class DatasetLayout:
    shape: tuple[int, ...]
    dtype: str
    chunks: tuple[int, ...] | None
    maxshape: tuple[int | None, ...]
    filters: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class PendingRemap:
    path: Path
    original: np.ndarray
    remapped: np.ndarray
    source_p95: int
    target_p95: int
    layout: DatasetLayout


@dataclass(frozen=True)
class PreflightResult:
    files_scanned: int
    unchanged_files: int
    pending: tuple[PendingRemap, ...]


def nearest_p95(values: np.ndarray) -> int:
    return int(np.percentile(values, 95, method="nearest"))


def stable_rng(path: Path) -> np.random.Generator:
    material = f"{RANDOM_SEED}:{path.name}".encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(material).digest()[:16], "big")
    return np.random.default_rng(seed)


def sample_truncated_offset(rng: np.random.Generator) -> float:
    while True:
        offset = float(rng.normal(loc=0.0, scale=OFFSET_SIGMA))
        if -OFFSET_LIMIT <= offset <= OFFSET_LIMIT:
            return offset


def remap_curve(
    data: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    values = np.asarray(data, dtype=np.float64)
    if values.ndim != 1 or values.size == 0:
        raise ValueError("data must be a non-empty one-dimensional array")

    minimum = float(values.min())
    if minimum != ANCHOR:
        raise ValueError(
            f"minimum gPO value must equal anchor {ANCHOR}, got {minimum}"
        )

    source_platform = float(nearest_p95(values))
    if source_platform <= ANCHOR:
        raise ValueError(
            f"p95={source_platform} must be greater than anchor {ANCHOR}"
        )

    target_platform = TARGET_PLATFORM_AVG + sample_truncated_offset(rng)
    scale = (target_platform - ANCHOR) / (source_platform - ANCHOR)
    corrected = ANCHOR + (values - ANCHOR) * scale
    return np.clip(np.rint(corrected), 0, 255).astype(np.uint8)


def dataset_layout(dataset: h5py.Dataset) -> DatasetLayout:
    creation = dataset.id.get_create_plist()
    filters = tuple(
        tuple(creation.get_filter(index))
        for index in range(creation.get_nfilters())
    )
    return DatasetLayout(
        shape=dataset.shape,
        dtype=dataset.dtype.str,
        chunks=dataset.chunks,
        maxshape=dataset.maxshape,
        filters=filters,
    )


def read_and_validate(path: Path) -> tuple[np.ndarray, DatasetLayout]:
    try:
        with h5py.File(path, "r") as h5:
            if GPO_DATASET_PATH not in h5:
                raise RuntimeError(f"missing dataset {GPO_DATASET_PATH}")
            dataset = h5[GPO_DATASET_PATH]
            if not isinstance(dataset, h5py.Dataset):
                raise RuntimeError(f"{GPO_DATASET_PATH} is not a dataset")
            if dataset.ndim != 1 or dataset.size == 0:
                raise RuntimeError(
                    f"{GPO_DATASET_PATH} must be non-empty and one-dimensional, "
                    f"got shape {dataset.shape}"
                )
            if dataset.dtype != np.dtype("uint8"):
                raise RuntimeError(
                    f"{GPO_DATASET_PATH} must have dtype uint8, got {dataset.dtype}"
                )
            values = dataset[...]
            layout = dataset_layout(dataset)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"{path}: {exc}") from exc

    minimum = int(values.min())
    if minimum != ANCHOR:
        raise RuntimeError(
            f"{path}: minimum {GPO_DATASET_PATH} value must equal "
            f"{ANCHOR}, got {minimum}"
        )
    return values, layout


def preflight(dataset_root: Path = DATASET_ROOT) -> PreflightResult:
    if not dataset_root.is_dir():
        raise RuntimeError(
            f"dataset root does not exist or is not a directory: {dataset_root}"
        )
    paths = tuple(sorted(dataset_root.glob("*.h5")))
    if not paths:
        raise RuntimeError(f"no HDF5 files found under {dataset_root}")

    validated: list[tuple[Path, np.ndarray, DatasetLayout]] = []
    errors: list[str] = []
    for path in paths:
        try:
            values, layout = read_and_validate(path)
            validated.append((path, values, layout))
        except RuntimeError as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError(
            "preflight failed; no files were modified:\n" + "\n".join(errors)
        )

    pending: list[PendingRemap] = []
    for path, values, layout in validated:
        source_p95 = nearest_p95(values)
        if source_p95 <= SOURCE_P95_THRESHOLD:
            continue
        remapped = remap_curve(values, stable_rng(path))
        target_p95 = nearest_p95(remapped)
        if remapped.shape != values.shape or remapped.dtype != np.dtype("uint8"):
            raise RuntimeError(f"{path}: remapped data shape or dtype is invalid")
        if int(remapped.min()) != ANCHOR:
            raise RuntimeError(
                f"{path}: remapped minimum does not preserve anchor {ANCHOR}"
            )
        if target_p95 > SOURCE_P95_THRESHOLD:
            raise RuntimeError(
                f"{path}: remapped p95={target_p95} still exceeds "
                f"threshold {SOURCE_P95_THRESHOLD}"
            )
        pending.append(
            PendingRemap(
                path=path,
                original=values,
                remapped=remapped,
                source_p95=source_p95,
                target_p95=target_p95,
                layout=layout,
            )
        )

    return PreflightResult(
        files_scanned=len(paths),
        unchanged_files=len(paths) - len(pending),
        pending=tuple(pending),
    )


def verify_values_and_layout(
    item: PendingRemap,
    expected_values: np.ndarray,
) -> None:
    with h5py.File(item.path, "r") as h5:
        dataset = h5[GPO_DATASET_PATH]
        actual = dataset[...]
        actual_layout = dataset_layout(dataset)
    if not np.array_equal(actual, expected_values):
        raise RuntimeError(f"{item.path}: written gPO values failed verification")
    if actual_layout != item.layout:
        raise RuntimeError(
            f"{item.path}: dataset layout changed from {item.layout!r} "
            f"to {actual_layout!r}"
        )


def restore_item(item: PendingRemap) -> None:
    with h5py.File(item.path, "r+") as h5:
        dataset = h5[GPO_DATASET_PATH]
        if dataset_layout(dataset) != item.layout:
            raise RuntimeError("dataset layout changed before recovery")
        dataset[...] = item.original
        h5.flush()
    verify_values_and_layout(item, item.original)


def apply_remaps(result: PreflightResult) -> tuple[Path, ...]:
    completed: list[Path] = []
    for item in result.pending:
        write_attempted = False
        try:
            with h5py.File(item.path, "r+") as h5:
                dataset = h5[GPO_DATASET_PATH]
                if dataset_layout(dataset) != item.layout:
                    raise RuntimeError(
                        f"{item.path}: dataset layout changed after preflight"
                    )
                current = dataset[...]
                if not np.array_equal(current, item.original):
                    raise RuntimeError(
                        f"{item.path}: gPO data changed after preflight"
                    )
                write_attempted = True
                dataset[...] = item.remapped
                h5.flush()
            verify_values_and_layout(item, item.remapped)
            completed.append(item.path)
        except Exception as exc:
            recovery_error: Exception | None = None
            if write_attempted:
                try:
                    restore_item(item)
                except Exception as recovery_exc:
                    recovery_error = recovery_exc
            message = (
                f"write failed for {item.path} after {len(completed)} completed "
                f"files: {exc}"
            )
            if not write_attempted:
                message += "; current file was not modified by this tool"
            elif recovery_error is None:
                message += "; current file was restored and verified"
            else:
                message += f"; current file recovery also failed: {recovery_error}"
            raise RuntimeError(message) from exc
    return tuple(completed)


def print_summary(result: PreflightResult) -> None:
    target_p95_values = [item.target_p95 for item in result.pending]
    print(f"Dataset root: {DATASET_ROOT}")
    print(f"Files scanned: {result.files_scanned}")
    print(f"Files requiring remap: {len(result.pending)}")
    print(f"Files unchanged: {result.unchanged_files}")
    print(f"Anchor: {ANCHOR}")
    print(f"Source p95 threshold: {SOURCE_P95_THRESHOLD}")
    print(f"Target platform average: {TARGET_PLATFORM_AVG}")
    print(f"Random seed: {RANDOM_SEED}")
    if target_p95_values:
        print(
            "Planned target p95 range: "
            f"{min(target_p95_values)}..{max(target_p95_values)}"
        )


def main() -> None:
    result = preflight()
    print_summary(result)
    if not result.pending:
        print("No HDF5 files require modification.")
        return

    print("No backups will be created.")
    entered = input(
        f'Type "{CONFIRMATION_TEXT}" to modify the HDF5 files: '
    )
    if entered != CONFIRMATION_TEXT:
        print("Confirmation did not match; no files were modified.")
        return

    completed = apply_remaps(result)
    print(f"Successfully remapped and verified {len(completed)} HDF5 files.")


if __name__ == "__main__":
    main()
