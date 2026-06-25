from __future__ import annotations

from pathlib import Path

import h5py
import hdf5plugin
import numpy as np
import pytest

from tools.migrate_hdf5_tactile_bgr import (
    NEW_DATASET_PATH,
    NEW_SCHEMA_VERSION,
    OLD_DATASET_PATH,
    OLD_SCHEMA_VERSION,
    apply_migration,
    dataset_snapshot,
    inspect_file,
    preflight,
)


def write_fixture(
    path: Path,
    *,
    dataset_path: str = OLD_DATASET_PATH,
    schema_version: str = OLD_SCHEMA_VERSION,
) -> None:
    values = np.arange(2 * 2 * 700 * 400 * 3, dtype=np.uint8).reshape(
        2, 2, 700, 400, 3
    )
    with h5py.File(path, "w") as h5:
        h5.attrs["schema_version"] = schema_version
        h5.attrs["preserved"] = "root-value"
        dataset = h5.create_dataset(
            dataset_path,
            data=values,
            chunks=(1, 2, 700, 400, 3),
            **dict(hdf5plugin.Zstd(clevel=12)),
        )
        dataset.attrs["sensor_names"] = np.asarray(
            ["left", "right"],
            dtype=h5py.string_dtype("utf-8"),
        )
        dataset.attrs["preserved"] = 17


def read_state(path: Path) -> tuple[str, bool, bool, object, str]:
    with h5py.File(path, "r") as h5:
        dataset_path = (
            NEW_DATASET_PATH if NEW_DATASET_PATH in h5 else OLD_DATASET_PATH
        )
        return (
            str(h5.attrs["schema_version"]),
            OLD_DATASET_PATH in h5,
            NEW_DATASET_PATH in h5,
            dataset_snapshot(h5[dataset_path]),
            str(h5.attrs["preserved"]),
        )


def test_dry_run_preflight_does_not_modify_file(tmp_path):
    path = tmp_path / "demo.h5"
    write_fixture(path)
    before = read_state(path)

    result = preflight(tmp_path)

    assert result.files_scanned == 1
    assert [item.action for item in result.pending] == ["rename"]
    assert read_state(path) == before


def test_apply_renames_link_updates_version_and_preserves_dataset(tmp_path):
    path = tmp_path / "nested" / "demo.h5"
    path.parent.mkdir()
    write_fixture(path)
    before = read_state(path)

    result = preflight(tmp_path)
    assert apply_migration(result) == 1

    after = read_state(path)
    assert after[0] == NEW_SCHEMA_VERSION
    assert after[1:3] == (False, True)
    assert after[3] == before[3]
    assert after[4] == "root-value"


def test_repeated_run_is_idempotent(tmp_path):
    path = tmp_path / "demo.h5"
    write_fixture(path)
    apply_migration(preflight(tmp_path))

    second = preflight(tmp_path)

    assert second.pending == ()
    assert second.already_migrated == 1
    assert apply_migration(second) == 0


def test_interrupted_new_path_v02_state_completes_version_only(tmp_path):
    path = tmp_path / "demo.h5"
    write_fixture(
        path,
        dataset_path=NEW_DATASET_PATH,
        schema_version=OLD_SCHEMA_VERSION,
    )
    before = read_state(path)

    result = preflight(tmp_path)

    assert [item.action for item in result.pending] == ["version_only"]
    assert apply_migration(result) == 1
    after = read_state(path)
    assert after[0] == NEW_SCHEMA_VERSION
    assert after[1:3] == (False, True)
    assert after[3] == before[3]


def test_preflight_rejects_both_paths_without_modifying_files(tmp_path):
    conflict = tmp_path / "conflict.h5"
    write_fixture(conflict)
    with h5py.File(conflict, "r+") as h5:
        h5[NEW_DATASET_PATH] = h5[OLD_DATASET_PATH]
    valid = tmp_path / "valid.h5"
    write_fixture(valid)
    before = read_state(valid)

    with pytest.raises(RuntimeError, match="no files were modified"):
        preflight(tmp_path)

    assert read_state(valid) == before
    with h5py.File(conflict, "r") as h5:
        assert OLD_DATASET_PATH in h5
        assert NEW_DATASET_PATH in h5


def test_inspect_rejects_unsupported_schema(tmp_path):
    path = tmp_path / "demo.h5"
    write_fixture(path, schema_version="v0.1")

    with pytest.raises(RuntimeError, match="unsupported path/schema state"):
        inspect_file(path)
