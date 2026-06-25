#!/usr/bin/env python3
"""Rename the tactile image dataset in existing HDF5 files to the BGR path.

Usage:
    # Read-only recursive preflight.
    /usr/bin/python3 tools/migrate_hdf5_tactile_bgr.py \
      --dataset-root /data/external/DATASET

    # Apply the migration after a successful preflight.
    /usr/bin/python3 tools/migrate_hdf5_tactile_bgr.py \
      --dataset-root /data/external/DATASET \
      --apply

The tool recursively scans ``*.h5`` files. By default it performs a complete
read-only preflight. With ``--apply``, it renames
``/observations/tactile_images/rgb`` to
``/observations/tactile_images/bgr`` using an HDF5 link move, so the image
payload is not copied, and updates ``schema_version`` from ``v0.2`` to
``v0.3``. It verifies object identity, shape, dtype, chunking, filters, and
attributes before and after each change.

The migration is idempotent. A file left at the new path with schema ``v0.2``
after an interrupted run is completed by updating its version. Files
containing both paths, neither path, or an unsupported schema version fail the
global preflight and no files are modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import hdf5plugin  # noqa: F401 - register HDF5 compression filters.
import numpy as np


DEFAULT_DATASET_ROOT = Path("/data/external/DATASET")
OLD_DATASET_PATH = "/observations/tactile_images/rgb"
NEW_DATASET_PATH = "/observations/tactile_images/bgr"
OLD_SCHEMA_VERSION = "v0.2"
NEW_SCHEMA_VERSION = "v0.3"
EXPECTED_SUFFIX_SHAPE = (2, 700, 400, 3)
EXPECTED_DTYPE = np.dtype("uint8")
EXPECTED_SENSOR_NAMES = ("left", "right")


@dataclass(frozen=True)
class DatasetSnapshot:
    object_address: int
    shape: tuple[int, ...]
    dtype: str
    chunks: tuple[int, ...] | None
    maxshape: tuple[int | None, ...]
    filters: tuple[tuple[Any, ...], ...]
    attrs: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class PendingMigration:
    path: Path
    action: str
    snapshot: DatasetSnapshot


@dataclass(frozen=True)
class PreflightResult:
    files_scanned: int
    already_migrated: int
    pending: tuple[PendingMigration, ...]


def decode_attr(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, np.generic):
        return value.item()
    return value


def freeze_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return (
            "ndarray",
            value.dtype.str,
            tuple(value.shape),
            freeze_value(value.tolist()),
        )
    if isinstance(value, np.generic):
        return freeze_value(value.item())
    if isinstance(value, list):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, bytes):
        return ("bytes", bytes(value))
    return value


def attr_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(decode_attr(item)) for item in value)


def dataset_snapshot(dataset: h5py.Dataset) -> DatasetSnapshot:
    creation = dataset.id.get_create_plist()
    filters = tuple(
        tuple(creation.get_filter(index))
        for index in range(creation.get_nfilters())
    )
    attrs = tuple(
        (key, freeze_value(dataset.attrs[key]))
        for key in sorted(dataset.attrs)
    )
    return DatasetSnapshot(
        object_address=int(h5py.h5o.get_info(dataset.id).addr),
        shape=tuple(dataset.shape),
        dtype=dataset.dtype.str,
        chunks=dataset.chunks,
        maxshape=dataset.maxshape,
        filters=filters,
        attrs=attrs,
    )


def validate_dataset(path: Path, dataset: h5py.Dataset) -> DatasetSnapshot:
    if dataset.ndim != 5 or tuple(dataset.shape[1:]) != EXPECTED_SUFFIX_SHAPE:
        raise RuntimeError(
            f"{path}: tactile image shape must be "
            f"(T, {', '.join(str(item) for item in EXPECTED_SUFFIX_SHAPE)}), "
            f"got {dataset.shape}"
        )
    if dataset.shape[0] <= 0:
        raise RuntimeError(f"{path}: tactile image dataset must not be empty")
    if dataset.dtype != EXPECTED_DTYPE:
        raise RuntimeError(
            f"{path}: tactile image dtype must be uint8, got {dataset.dtype}"
        )
    sensor_names = attr_strings(dataset.attrs.get("sensor_names"))
    if sensor_names != EXPECTED_SENSOR_NAMES:
        raise RuntimeError(
            f"{path}: sensor_names must be {EXPECTED_SENSOR_NAMES}, "
            f"got {sensor_names}"
        )
    return dataset_snapshot(dataset)


def inspect_file(path: Path) -> tuple[str, DatasetSnapshot]:
    try:
        with h5py.File(path, "r") as h5:
            schema_version = decode_attr(h5.attrs.get("schema_version"))
            has_old = OLD_DATASET_PATH in h5
            has_new = NEW_DATASET_PATH in h5
            if has_old and has_new:
                raise RuntimeError(
                    f"both {OLD_DATASET_PATH} and {NEW_DATASET_PATH} exist"
                )
            if not has_old and not has_new:
                raise RuntimeError(
                    f"neither {OLD_DATASET_PATH} nor {NEW_DATASET_PATH} exists"
                )
            dataset_path = OLD_DATASET_PATH if has_old else NEW_DATASET_PATH
            dataset = h5[dataset_path]
            if not isinstance(dataset, h5py.Dataset):
                raise RuntimeError(f"{dataset_path} is not a dataset")
            snapshot = validate_dataset(path, dataset)
    except OSError as exc:
        raise RuntimeError(f"cannot open HDF5 file: {exc}") from exc

    if has_old and schema_version == OLD_SCHEMA_VERSION:
        return "rename", snapshot
    if has_new and schema_version == OLD_SCHEMA_VERSION:
        return "version_only", snapshot
    if has_new and schema_version == NEW_SCHEMA_VERSION:
        return "complete", snapshot
    raise RuntimeError(
        f"unsupported path/schema state: path={dataset_path}, "
        f"schema_version={schema_version!r}"
    )


def preflight(dataset_root: Path) -> PreflightResult:
    if not dataset_root.is_dir():
        raise RuntimeError(
            f"dataset root does not exist or is not a directory: {dataset_root}"
        )
    paths = tuple(sorted(dataset_root.rglob("*.h5")))
    if not paths:
        raise RuntimeError(f"no HDF5 files found under {dataset_root}")

    pending: list[PendingMigration] = []
    already_migrated = 0
    errors: list[str] = []
    for path in paths:
        try:
            action, snapshot = inspect_file(path)
            if action == "complete":
                already_migrated += 1
            else:
                pending.append(PendingMigration(path, action, snapshot))
        except RuntimeError as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        raise RuntimeError(
            "preflight failed; no files were modified:\n" + "\n".join(errors)
        )
    return PreflightResult(
        files_scanned=len(paths),
        already_migrated=already_migrated,
        pending=tuple(pending),
    )


def require_unchanged(
    path: Path,
    dataset: h5py.Dataset,
    expected: DatasetSnapshot,
) -> None:
    actual = validate_dataset(path, dataset)
    if actual != expected:
        raise RuntimeError(
            f"{path}: tactile image dataset changed after preflight"
        )


def apply_one(item: PendingMigration) -> None:
    with h5py.File(item.path, "r+") as h5:
        schema_version = decode_attr(h5.attrs.get("schema_version"))
        has_old = OLD_DATASET_PATH in h5
        has_new = NEW_DATASET_PATH in h5
        if item.action == "rename":
            if schema_version != OLD_SCHEMA_VERSION or not has_old or has_new:
                raise RuntimeError(
                    f"{item.path}: path/schema state changed after preflight"
                )
            require_unchanged(item.path, h5[OLD_DATASET_PATH], item.snapshot)
            h5.move(OLD_DATASET_PATH, NEW_DATASET_PATH)
            require_unchanged(item.path, h5[NEW_DATASET_PATH], item.snapshot)
        elif item.action == "version_only":
            if schema_version != OLD_SCHEMA_VERSION or has_old or not has_new:
                raise RuntimeError(
                    f"{item.path}: path/schema state changed after preflight"
                )
            require_unchanged(item.path, h5[NEW_DATASET_PATH], item.snapshot)
        else:
            raise RuntimeError(f"{item.path}: unknown migration action {item.action}")
        h5.attrs["schema_version"] = NEW_SCHEMA_VERSION
        h5.flush()

    action, snapshot = inspect_file(item.path)
    if action != "complete" or snapshot != item.snapshot:
        raise RuntimeError(f"{item.path}: post-migration verification failed")


def apply_migration(result: PreflightResult) -> int:
    completed = 0
    for item in result.pending:
        try:
            apply_one(item)
        except Exception as exc:
            raise RuntimeError(
                f"migration failed for {item.path} after "
                f"{completed}/{len(result.pending)} files completed: {exc}"
            ) from exc
        completed += 1
    return completed


def summary_payload(
    dataset_root: Path,
    result: PreflightResult,
    applied: bool,
    modified: int,
) -> dict[str, Any]:
    return {
        "mode": "apply" if applied else "dry-run",
        "dataset_root": dataset_root.as_posix(),
        "files_scanned": result.files_scanned,
        "pending_rename": sum(
            item.action == "rename" for item in result.pending
        ),
        "pending_version_only": sum(
            item.action == "version_only" for item in result.pending
        ),
        "already_migrated": result.already_migrated,
        "modified": modified,
        "target_schema_version": NEW_SCHEMA_VERSION,
        "target_dataset_path": NEW_DATASET_PATH,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=DEFAULT_DATASET_ROOT,
        help="root directory recursively containing HDF5 files",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes; without this flag the tool only performs a dry-run",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset_root = args.dataset_root.expanduser().resolve()
    try:
        result = preflight(dataset_root)
        modified = apply_migration(result) if args.apply else 0
        print(
            json.dumps(
                summary_payload(dataset_root, result, args.apply, modified),
                indent=2,
            )
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
