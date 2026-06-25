from __future__ import annotations

from pathlib import Path

import hdf5plugin
import h5py
import numpy as np
import pytest

import tools.remap_gripper_gpo as remap_tool
from tools.remap_gripper_gpo import (
    ANCHOR,
    GPO_DATASET_PATH,
    SOURCE_P95_THRESHOLD,
    apply_remaps,
    dataset_layout,
    nearest_p95,
    preflight,
    remap_curve,
    stable_rng,
)


def high_curve() -> np.ndarray:
    return np.asarray([ANCHOR] * 5 + [200] * 95, dtype=np.uint8)


def low_curve() -> np.ndarray:
    return np.asarray([ANCHOR] * 5 + [160] * 95, dtype=np.uint8)


def write_h5(path: Path, values: np.ndarray) -> None:
    with h5py.File(path, "w") as h5:
        h5.create_dataset(
            GPO_DATASET_PATH,
            data=values,
            chunks=(min(32, len(values)),),
            **dict(hdf5plugin.Zstd(clevel=12)),
        )


def read_h5(path: Path) -> tuple[np.ndarray, object]:
    with h5py.File(path, "r") as h5:
        dataset = h5[GPO_DATASET_PATH]
        return dataset[...], dataset_layout(dataset)


def test_remap_preserves_anchor_and_is_deterministic(tmp_path):
    path = tmp_path / "demo_1.h5"
    values = high_curve()

    first = remap_curve(values, stable_rng(path))
    second = remap_curve(values, stable_rng(path))

    assert np.array_equal(first, second)
    assert first.dtype == np.dtype("uint8")
    assert first.shape == values.shape
    assert int(first.min()) == ANCHOR
    assert nearest_p95(first) <= SOURCE_P95_THRESHOLD


def test_preflight_failure_prevents_all_writes(tmp_path):
    valid_path = tmp_path / "demo_1.h5"
    invalid_path = tmp_path / "demo_2.h5"
    write_h5(valid_path, high_curve())
    write_h5(
        invalid_path,
        np.asarray([4] * 5 + [200] * 95, dtype=np.uint8),
    )
    valid_before, valid_layout = read_h5(valid_path)

    with pytest.raises(RuntimeError, match="no files were modified"):
        preflight(tmp_path)

    valid_after, layout_after = read_h5(valid_path)
    assert np.array_equal(valid_after, valid_before)
    assert layout_after == valid_layout


def test_preflight_skips_p95_at_or_below_threshold(tmp_path):
    path = tmp_path / "demo_1.h5"
    values = low_curve()
    write_h5(path, values)

    result = preflight(tmp_path)

    assert result.files_scanned == 1
    assert result.unchanged_files == 1
    assert result.pending == ()
    actual, _ = read_h5(path)
    assert np.array_equal(actual, values)


def test_preflight_is_stable_for_interrupted_rerun(tmp_path):
    first_path = tmp_path / "demo_1.h5"
    second_path = tmp_path / "demo_2.h5"
    write_h5(first_path, high_curve())
    write_h5(second_path, high_curve())

    first_plan = preflight(tmp_path)
    second_plan = preflight(tmp_path)

    assert [item.path for item in first_plan.pending] == [
        item.path for item in second_plan.pending
    ]
    for first, second in zip(first_plan.pending, second_plan.pending):
        assert np.array_equal(first.remapped, second.remapped)


def test_apply_writes_values_and_preserves_layout(tmp_path):
    remapped_path = tmp_path / "demo_1.h5"
    unchanged_path = tmp_path / "demo_2.h5"
    write_h5(remapped_path, high_curve())
    write_h5(unchanged_path, low_curve())
    original_unchanged, unchanged_layout = read_h5(unchanged_path)
    _, original_remapped_layout = read_h5(remapped_path)

    result = preflight(tmp_path)
    completed = apply_remaps(result)

    assert completed == (remapped_path,)
    remapped, remapped_layout = read_h5(remapped_path)
    unchanged, layout_after = read_h5(unchanged_path)
    assert np.array_equal(remapped, result.pending[0].remapped)
    assert int(remapped.min()) == ANCHOR
    assert nearest_p95(remapped) <= SOURCE_P95_THRESHOLD
    assert remapped_layout == original_remapped_layout
    assert remapped.dtype == np.dtype("uint8")
    assert remapped.shape == high_curve().shape
    assert np.array_equal(unchanged, original_unchanged)
    assert layout_after == unchanged_layout


def test_apply_does_not_overwrite_external_change_after_preflight(tmp_path):
    path = tmp_path / "demo_1.h5"
    write_h5(path, high_curve())
    result = preflight(tmp_path)
    externally_changed = high_curve().copy()
    externally_changed[-1] = 199
    with h5py.File(path, "r+") as h5:
        h5[GPO_DATASET_PATH][...] = externally_changed

    with pytest.raises(RuntimeError, match="was not modified by this tool"):
        apply_remaps(result)

    actual, _ = read_h5(path)
    assert np.array_equal(actual, externally_changed)


def test_apply_restores_current_file_when_post_write_verification_fails(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "demo_1.h5"
    original = high_curve()
    write_h5(path, original)
    result = preflight(tmp_path)
    real_verify = remap_tool.verify_values_and_layout
    calls = 0

    def fail_first_verification(item, expected_values):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected verification failure")
        real_verify(item, expected_values)

    monkeypatch.setattr(
        remap_tool,
        "verify_values_and_layout",
        fail_first_verification,
    )

    with pytest.raises(RuntimeError, match="restored and verified"):
        apply_remaps(result)

    actual, _ = read_h5(path)
    assert np.array_equal(actual, original)
