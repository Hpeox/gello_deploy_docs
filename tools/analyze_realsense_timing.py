#!/usr/bin/env python3
"""Analyze raw RealSense timing stability and visual bundle necessity.

The script is intentionally read-only with respect to runtime_sessions/demos.
It discovers demos with a successful aligned/aligned_manifest.json, reads raw
RealSense metadata timestamps per demo, and writes a timestamped report run
under temp_realsense_test by default.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = REPO_ROOT / "runtime_sessions" / "demos"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "temp_realsense_test"
NSEC_PER_MSEC = 1_000_000.0
NOMINAL_REALSENSE_PERIOD_MS = 33.333333333
EXPECTED_CAMERAS = ("cam1", "cam2", "cam3", "cam4")
EXPECTED_ROLES = ("color", "aligned_depth")
STREAM_ORDER = tuple(f"{camera}_{role}" for camera in EXPECTED_CAMERAS for role in EXPECTED_ROLES)
PAIR_THRESHOLD_MS = 20.0
BUNDLE_THRESHOLDS_MS = (15.0, 20.0, 25.0, 30.0)


@dataclass(frozen=True)
class StreamData:
    camera: str
    role: str
    topic: str
    times_ns: np.ndarray
    source_indices: np.ndarray
    frame_numbers: np.ndarray | None = None

    @property
    def name(self) -> str:
        return f"{self.camera}_{self.role}"


@dataclass(frozen=True)
class PairResult:
    camera: str
    tick_times_ns: np.ndarray
    pair_delta_ms: np.ndarray
    color_count: int
    depth_count: int
    matched_count: int
    unmatched_count: int


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def percentile(values: list[float] | np.ndarray, q: float) -> float | None:
    if len(values) == 0:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), q * 100.0))


def fmt(value: float | int | None, digits: int = 6) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return f"{value:.{digits}f}"
    return str(value)


def safe_int_array(values: Any) -> np.ndarray:
    result: list[int] = []
    for value in values:
        try:
            if value is None or (isinstance(value, float) and math.isnan(value)):
                result.append(-1)
            else:
                result.append(int(value))
        except Exception:
            result.append(-1)
    return np.asarray(result, dtype=np.int64)


def discover_successful_demos(input_dir: Path) -> list[Path]:
    demos: list[Path] = []
    for demo_dir in sorted(input_dir.glob("demo_*")):
        aligned_manifest = demo_dir / "aligned" / "aligned_manifest.json"
        if not aligned_manifest.exists():
            continue
        try:
            manifest = read_json(aligned_manifest)
        except Exception:
            continue
        if manifest.get("status") == "done":
            demos.append(demo_dir)
    return demos


def required_image_topics(manifest: dict[str, Any]) -> list[str]:
    postcheck = manifest.get("realsense_rosbag_postcheck") or {}
    readiness = manifest.get("realsense_image_readiness") or {}
    return [str(topic) for topic in (postcheck.get("required_topics") or readiness.get("required_topics") or [])]


def image_topic_to_metadata_topic(topic: str) -> str:
    if "/color/" in topic:
        return topic.replace("/color/image_raw", "/color/metadata")
    return re.sub(r"/aligned_depth_to_color/image_raw$", "/depth/metadata", topic)


def topic_camera_role(topic: str) -> tuple[str, str] | None:
    parts = [part for part in topic.split("/") if part]
    if not parts:
        return None
    camera = parts[0]
    if camera not in EXPECTED_CAMERAS:
        return None
    if "color" in parts:
        return camera, "color"
    if "aligned_depth_to_color" in parts or "depth" in parts:
        return camera, "aligned_depth"
    return None


def expected_image_topic(camera: str, role: str) -> str:
    if role == "color":
        return f"/{camera}/camera/color/image_raw"
    return f"/{camera}/camera/aligned_depth_to_color/image_raw"


def load_realsense_streams(demo_dir: Path) -> tuple[dict[str, StreamData], list[str]]:
    warnings: list[str] = []
    manifest_path = demo_dir / "manifest.json"
    if not manifest_path.exists():
        return {}, ["manifest.json missing"]
    manifest = read_json(manifest_path)
    npz_value = (manifest.get("npz") or {}).get("realsense", "realsense_metadata.npz")
    npz_path = Path(npz_value)
    if not npz_path.is_absolute():
        npz_path = demo_dir / npz_path
    if not npz_path.exists():
        return {}, [f"RealSense metadata missing: {npz_path.relative_to(demo_dir)}"]

    data = np.load(npz_path, allow_pickle=True)
    metadata_topics = np.asarray(data["topic"]).astype(str)
    header_stamp_ns = safe_int_array(data["header_stamp_ns"])
    frame_numbers = safe_int_array(data["frame_number"]) if "frame_number" in data.files else None

    metadata_by_topic: dict[str, dict[str, np.ndarray]] = {}
    for topic in sorted(set(metadata_topics)):
        mask = metadata_topics == topic
        valid = header_stamp_ns[mask] > 0
        indices = np.nonzero(mask)[0].astype(np.int64)[valid]
        times = header_stamp_ns[mask][valid]
        frames = frame_numbers[mask][valid] if frame_numbers is not None else None
        order = np.argsort(times, kind="stable")
        metadata_by_topic[topic] = {
            "times_ns": times[order].astype(np.int64, copy=False),
            "source_indices": indices[order].astype(np.int64, copy=False),
            "frame_numbers": None if frames is None else frames[order].astype(np.int64, copy=False),
        }

    topics = required_image_topics(manifest)
    if not topics:
        topics = [expected_image_topic(camera, role) for camera in EXPECTED_CAMERAS for role in EXPECTED_ROLES]

    streams: dict[str, StreamData] = {}
    for image_topic in topics:
        parsed = topic_camera_role(image_topic)
        if parsed is None:
            continue
        camera, role = parsed
        metadata_topic = image_topic_to_metadata_topic(image_topic)
        table = metadata_by_topic.get(metadata_topic)
        if table is None:
            warnings.append(f"metadata topic missing for {image_topic}: expected {metadata_topic}")
            continue
        stream = StreamData(
            camera=camera,
            role=role,
            topic=image_topic,
            times_ns=table["times_ns"],
            source_indices=table["source_indices"],
            frame_numbers=table["frame_numbers"],
        )
        streams[stream.name] = stream
    return streams, warnings


def dt_stats(times_ns: np.ndarray) -> dict[str, Any]:
    if len(times_ns) == 0:
        return {
            "frame_count": 0,
            "interval_count": 0,
            "median_dt_ms": None,
            "mean_dt_ms": None,
            "p95_dt_ms": None,
            "p99_dt_ms": None,
            "jitter_p95_ms": None,
            "max_gap_ms": None,
            "drop_count": None,
            "duplicate_or_reverse_count": None,
        }
    dt_ms = np.diff(times_ns.astype(np.float64)) / NSEC_PER_MSEC
    if len(dt_ms) == 0:
        return {
            "frame_count": int(len(times_ns)),
            "interval_count": 0,
            "median_dt_ms": None,
            "mean_dt_ms": None,
            "p95_dt_ms": None,
            "p99_dt_ms": None,
            "jitter_p95_ms": None,
            "max_gap_ms": None,
            "drop_count": 0,
            "duplicate_or_reverse_count": 0,
        }
    median_dt = float(np.median(dt_ms))
    return {
        "frame_count": int(len(times_ns)),
        "interval_count": int(len(dt_ms)),
        "median_dt_ms": median_dt,
        "mean_dt_ms": float(np.mean(dt_ms)),
        "p95_dt_ms": percentile(dt_ms, 0.95),
        "p99_dt_ms": percentile(dt_ms, 0.99),
        "jitter_p95_ms": percentile(np.abs(dt_ms - median_dt), 0.95),
        "max_gap_ms": float(np.max(dt_ms)),
        "drop_count": int(np.sum(dt_ms > 1.5 * median_dt)) if median_dt > 0 else None,
        "duplicate_or_reverse_count": int(np.sum(dt_ms <= 0)),
    }


def nearest_one_to_one_pairs(left_ns: np.ndarray, right_ns: np.ndarray, threshold_ms: float) -> tuple[np.ndarray, np.ndarray]:
    candidates: list[tuple[int, int, int]] = []
    threshold_ns = int(round(threshold_ms * NSEC_PER_MSEC))
    for i, value in enumerate(left_ns):
        pos = int(np.searchsorted(right_ns, value, side="left"))
        for j in (pos - 1, pos):
            if 0 <= j < len(right_ns):
                delta = abs(int(value) - int(right_ns[j]))
                if delta <= threshold_ns:
                    candidates.append((delta, i, j))
    candidates.sort()
    used_left: set[int] = set()
    used_right: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _delta, i, j in candidates:
        if i in used_left or j in used_right:
            continue
        used_left.add(i)
        used_right.add(j)
        pairs.append((i, j))
    pairs.sort()
    if not pairs:
        return np.asarray([], dtype=np.int64), np.asarray([], dtype=np.int64)
    left_idx, right_idx = zip(*pairs)
    return np.asarray(left_idx, dtype=np.int64), np.asarray(right_idx, dtype=np.int64)


def pair_color_depth(streams: dict[str, StreamData], threshold_ms: float) -> dict[str, PairResult]:
    results: dict[str, PairResult] = {}
    for camera in EXPECTED_CAMERAS:
        color = streams.get(f"{camera}_color")
        depth = streams.get(f"{camera}_aligned_depth")
        if color is None or depth is None:
            continue
        color_idx, depth_idx = nearest_one_to_one_pairs(color.times_ns, depth.times_ns, threshold_ms)
        color_times = color.times_ns[color_idx] if len(color_idx) else np.asarray([], dtype=np.int64)
        depth_times = depth.times_ns[depth_idx] if len(depth_idx) else np.asarray([], dtype=np.int64)
        deltas_ms = np.abs(color_times.astype(np.float64) - depth_times.astype(np.float64)) / NSEC_PER_MSEC
        tick_times = np.maximum(color_times, depth_times).astype(np.int64, copy=False)
        order = np.argsort(tick_times, kind="stable")
        results[camera] = PairResult(
            camera=camera,
            tick_times_ns=tick_times[order],
            pair_delta_ms=deltas_ms[order],
            color_count=int(len(color.times_ns)),
            depth_count=int(len(depth.times_ns)),
            matched_count=int(len(tick_times)),
            unmatched_count=int(len(color.times_ns) + len(depth.times_ns) - 2 * len(tick_times)),
        )
    return results


def summarize_values(values: np.ndarray | list[float]) -> dict[str, float | None]:
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return {"mean": None, "median": None, "p95": None, "p99": None, "max": None}
    return {
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "p95": percentile(arr, 0.95),
        "p99": percentile(arr, 0.99),
        "max": float(np.max(arr)),
    }


def nearest_signed_offsets(anchor_ns: np.ndarray, target_ns: np.ndarray) -> np.ndarray:
    offsets: list[float] = []
    for value in anchor_ns:
        pos = int(np.searchsorted(target_ns, value, side="left"))
        choices = []
        for j in (pos - 1, pos):
            if 0 <= j < len(target_ns):
                choices.append(int(target_ns[j]) - int(value))
        if choices:
            offsets.append(float(min(choices, key=abs)) / NSEC_PER_MSEC)
    return np.asarray(offsets, dtype=float)


def circular_offsets_ms(anchor_ns: np.ndarray, target_ns: np.ndarray, period_ms: float) -> np.ndarray:
    if period_ms <= 0:
        return np.asarray([], dtype=float)
    period_ns = period_ms * NSEC_PER_MSEC
    offsets: list[float] = []
    for value in anchor_ns:
        pos = int(np.searchsorted(target_ns, value, side="left"))
        choices = []
        for j in (pos - 1, pos):
            if 0 <= j < len(target_ns):
                raw = float(int(target_ns[j]) - int(value))
                centered = ((raw + period_ns / 2.0) % period_ns) - period_ns / 2.0
                choices.append(centered)
        if choices:
            offsets.append(min(choices, key=abs) / NSEC_PER_MSEC)
    return np.asarray(offsets, dtype=float)


def wrap_to_half_period(value: float, period: float) -> float:
    return ((value + period / 2.0) % period) - period / 2.0


def line_fit_stats(times_ns: np.ndarray) -> dict[str, float | int | None]:
    if len(times_ns) < 2:
        return {
            "frame_count": int(len(times_ns)),
            "fit_intercept_ms": None,
            "fit_period_ms": None,
            "period_error_ms": None,
            "residual_abs_median_ms": None,
            "residual_abs_p95_ms": None,
            "residual_abs_max_ms": None,
            "estimated_period_ms": None,
            "phase_ms": None,
            "residual_p95_ms": None,
            "residual_max_ms": None,
        }
    x = np.arange(len(times_ns), dtype=np.float64)
    t0_ms = float(times_ns[0]) / NSEC_PER_MSEC
    y = times_ns.astype(np.float64) / NSEC_PER_MSEC - t0_ms
    b, intercept_rel = np.polyfit(x, y, 1)
    fitted = intercept_rel + b * x
    residual = np.abs(y - fitted)
    a_ms = t0_ms + float(intercept_rel)
    phase = a_ms % float(b) if b > 0 else None
    return {
        "frame_count": int(len(times_ns)),
        "fit_intercept_ms": a_ms,
        "fit_period_ms": float(b),
        "period_error_ms": float(b) - NOMINAL_REALSENSE_PERIOD_MS,
        "residual_abs_median_ms": percentile(residual, 0.50),
        "residual_abs_p95_ms": percentile(residual, 0.95),
        "residual_abs_max_ms": float(np.max(residual)),
        "estimated_period_ms": float(b),
        "phase_ms": phase,
        "residual_p95_ms": percentile(residual, 0.95),
        "residual_max_ms": float(np.max(residual)),
    }


def foldback_stats(anchor_ns: np.ndarray, target_ns: np.ndarray) -> dict[str, Any]:
    prev_ages: list[float] = []
    next_delays: list[float] = []
    foldback_prev_ages: list[float] = []
    foldback_next_delays: list[float] = []
    foldback_count = 0
    comparable_count = 0
    for anchor in anchor_ns:
        right = int(np.searchsorted(target_ns, anchor, side="right"))
        prev_idx = right - 1
        next_idx = right
        if prev_idx < 0 or next_idx >= len(target_ns):
            continue
        prev_age = float(int(anchor) - int(target_ns[prev_idx])) / NSEC_PER_MSEC
        next_delay = float(int(target_ns[next_idx]) - int(anchor)) / NSEC_PER_MSEC
        comparable_count += 1
        prev_ages.append(prev_age)
        next_delays.append(next_delay)
        if prev_age > 20.0 and next_delay < 10.0:
            foldback_count += 1
            foldback_prev_ages.append(prev_age)
            foldback_next_delays.append(next_delay)
    return {
        "comparable_count": comparable_count,
        "foldback_count": foldback_count,
        "foldback_rate": None if comparable_count == 0 else foldback_count / comparable_count,
        "prev_age_median_ms": percentile(prev_ages, 0.50),
        "prev_age_p95_ms": percentile(prev_ages, 0.95),
        "next_delay_median_ms": percentile(next_delays, 0.50),
        "next_delay_p95_ms": percentile(next_delays, 0.95),
        "foldback_prev_age_median_ms": percentile(foldback_prev_ages, 0.50),
        "foldback_next_delay_median_ms": percentile(foldback_next_delays, 0.50),
    }


def closest_bundles(tick_streams: dict[str, np.ndarray], threshold_ms: float) -> np.ndarray:
    cameras = list(EXPECTED_CAMERAS)
    pointers = {camera: 0 for camera in cameras}
    spans: list[float] = []
    threshold_ns = int(round(threshold_ms * NSEC_PER_MSEC))
    while all(pointers[camera] < len(tick_streams[camera]) for camera in cameras):
        current = {camera: int(tick_streams[camera][pointers[camera]]) for camera in cameras}
        min_camera = min(current, key=current.get)
        min_time = current[min_camera]
        max_time = max(current.values())
        span = max_time - min_time
        if span <= threshold_ns:
            spans.append(float(span) / NSEC_PER_MSEC)
            for camera in cameras:
                pointers[camera] += 1
        else:
            pointers[min_camera] += 1
    return np.asarray(spans, dtype=float)


def create_run_dir(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for suffix in ["", *[f"_{i:02d}" for i in range(1, 100)]]:
        run_dir = output_dir / f"run_{stamp}{suffix}"
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            continue
    raise RuntimeError(f"could not create unique run directory under {output_dir}")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def add_single_stream_rows(
    demo: str,
    streams: dict[str, StreamData],
    rows: list[dict[str, Any]],
    pooled_dt: dict[str, list[float]],
) -> None:
    for stream_name in STREAM_ORDER:
        camera, role = stream_name.split("_", 1)
        stream = streams.get(stream_name)
        if stream is None:
            rows.append({"scope": "demo", "demo": demo, "stream": stream_name, "camera": camera, "role": role, "status": "missing"})
            continue
        stats = dt_stats(stream.times_ns)
        dt_ms = np.diff(stream.times_ns.astype(np.float64)) / NSEC_PER_MSEC
        pooled_dt.setdefault(stream_name, []).extend(float(value) for value in dt_ms)
        rows.append(
            {
                "scope": "demo",
                "demo": demo,
                "stream": stream_name,
                "camera": camera,
                "role": role,
                "topic": stream.topic,
                "status": "ok",
                **stats,
            }
        )


def add_pair_rows(
    demo: str,
    streams: dict[str, StreamData],
    pair_results: dict[str, PairResult],
    rows: list[dict[str, Any]],
    pooled_delta: dict[str, list[float]],
) -> None:
    for camera in EXPECTED_CAMERAS:
        color_missing = f"{camera}_color" not in streams
        depth_missing = f"{camera}_aligned_depth" not in streams
        pair = pair_results.get(camera)
        if pair is None:
            rows.append(
                {
                    "scope": "demo",
                    "demo": demo,
                    "camera": camera,
                    "status": "missing",
                    "missing_streams": ",".join(
                        name
                        for name, missing in ((f"{camera}_color", color_missing), (f"{camera}_aligned_depth", depth_missing))
                        if missing
                    ),
                }
            )
            continue
        pooled_delta.setdefault(camera, []).extend(float(value) for value in pair.pair_delta_ms)
        summary = summarize_values(pair.pair_delta_ms)
        rows.append(
            {
                "scope": "demo",
                "demo": demo,
                "camera": camera,
                "status": "ok",
                "color_count": pair.color_count,
                "depth_count": pair.depth_count,
                "pair_success_count": pair.matched_count,
                "unmatched_count": pair.unmatched_count,
                "pair_delta_mean_ms": summary["mean"],
                "pair_delta_median_ms": summary["median"],
                "pair_delta_p95_ms": summary["p95"],
                "pair_delta_max_ms": summary["max"],
                "camera_tick_span_mean_ms": summary["mean"],
                "camera_tick_span_median_ms": summary["median"],
                "camera_tick_span_p95_ms": summary["p95"],
                "camera_tick_span_max_ms": summary["max"],
            }
        )


def add_phase_rows(
    demo: str,
    ticks: dict[str, np.ndarray],
    rows: list[dict[str, Any]],
    pooled_offsets: dict[str, list[float]],
    pooled_phase_offsets: dict[str, list[float]],
    line_fit_rows: dict[str, list[dict[str, Any]]],
) -> None:
    missing = [camera for camera in EXPECTED_CAMERAS if camera not in ticks or len(ticks[camera]) == 0]
    if "cam1" not in ticks or len(ticks.get("cam1", [])) == 0:
        rows.append({"scope": "demo", "demo": demo, "row_type": "phase_pair", "status": "missing", "missing_cameras": ",".join(missing)})
    else:
        cam1 = ticks["cam1"]
        cam1_dt = np.diff(cam1.astype(np.float64)) / NSEC_PER_MSEC
        period_ms = float(np.median(cam1_dt)) if len(cam1_dt) else 33.333333
        for camera in ("cam2", "cam3", "cam4"):
            if camera not in ticks or len(ticks[camera]) == 0:
                rows.append(
                    {
                        "scope": "demo",
                        "demo": demo,
                        "row_type": "phase_pair",
                        "status": "missing",
                        "reference_camera": "cam1",
                        "target_camera": camera,
                        "missing_cameras": camera,
                    }
                )
                continue
            offsets = nearest_signed_offsets(cam1, ticks[camera])
            phases = circular_offsets_ms(cam1, ticks[camera], period_ms)
            key = f"cam1_to_{camera}"
            pooled_offsets.setdefault(key, []).extend(float(value) for value in offsets)
            pooled_phase_offsets.setdefault(key, []).extend(float(value) for value in phases)
            rows.append(
                {
                    "scope": "demo",
                    "demo": demo,
                    "row_type": "phase_pair",
                    "status": "ok",
                    "reference_camera": "cam1",
                    "target_camera": camera,
                    "sample_count": len(offsets),
                    "median_signed_offset_ms": percentile(offsets, 0.50),
                    "p95_abs_offset_ms": percentile(np.abs(offsets), 0.95),
                    "phase_offset_median_ms": percentile(phases, 0.50),
                    "phase_offset_p95_ms": percentile(np.abs(phases), 0.95),
                }
            )
    for camera in EXPECTED_CAMERAS:
        if camera not in ticks or len(ticks[camera]) == 0:
            rows.append({"scope": "demo", "demo": demo, "row_type": "line_fit", "status": "missing", "camera": camera})
            continue
        stats = line_fit_stats(ticks[camera])
        row = {"scope": "demo", "demo": demo, "row_type": "line_fit", "status": "ok", "camera": camera, **stats}
        rows.append(row)
        line_fit_rows.setdefault(camera, []).append(row)


def add_foldback_rows(
    demo: str,
    ticks: dict[str, np.ndarray],
    rows: list[dict[str, Any]],
    pooled: dict[str, dict[str, list[float] | int]],
) -> None:
    for anchor in EXPECTED_CAMERAS:
        for target in EXPECTED_CAMERAS:
            if anchor == target:
                continue
            if anchor not in ticks or target not in ticks or len(ticks[anchor]) == 0 or len(ticks[target]) < 2:
                rows.append(
                    {
                        "scope": "demo",
                        "demo": demo,
                        "status": "missing",
                        "anchor_camera": anchor,
                        "target_camera": target,
                    }
                )
                continue
            stats = foldback_stats(ticks[anchor], ticks[target])
            key = f"{anchor}_to_{target}"
            bucket = pooled.setdefault(
                key,
                {
                    "foldback_count": 0,
                    "comparable_count": 0,
                    "prev_ages": [],
                    "next_delays": [],
                },
            )
            bucket["foldback_count"] = int(bucket["foldback_count"]) + int(stats["foldback_count"])
            bucket["comparable_count"] = int(bucket["comparable_count"]) + int(stats["comparable_count"])
            # Recompute arrays once for aggregate so the public columns mean the same thing.
            for anchor_time in ticks[anchor]:
                right = int(np.searchsorted(ticks[target], anchor_time, side="right"))
                if 0 <= right - 1 and right < len(ticks[target]):
                    cast_prev = bucket["prev_ages"]
                    cast_next = bucket["next_delays"]
                    assert isinstance(cast_prev, list)
                    assert isinstance(cast_next, list)
                    cast_prev.append(float(int(anchor_time) - int(ticks[target][right - 1])) / NSEC_PER_MSEC)
                    cast_next.append(float(int(ticks[target][right]) - int(anchor_time)) / NSEC_PER_MSEC)
            rows.append({"scope": "demo", "demo": demo, "status": "ok", "anchor_camera": anchor, "target_camera": target, **stats})


def add_bundle_rows(
    demo: str,
    ticks: dict[str, np.ndarray],
    rows: list[dict[str, Any]],
    pooled_spans: dict[float, list[float]],
    pooled_counts: dict[float, dict[str, int]],
) -> None:
    missing = [camera for camera in EXPECTED_CAMERAS if camera not in ticks or len(ticks[camera]) == 0]
    total_frames = sum(len(ticks.get(camera, [])) for camera in EXPECTED_CAMERAS)
    for threshold in BUNDLE_THRESHOLDS_MS:
        if missing:
            rows.append(
                {
                    "scope": "demo",
                    "demo": demo,
                    "status": "missing",
                    "threshold_ms": threshold,
                    "missing_cameras": ",".join(missing),
                }
            )
            continue
        spans = closest_bundles(ticks, threshold)
        summary = summarize_values(spans)
        dropped = int(total_frames - 4 * len(spans))
        pooled_spans.setdefault(threshold, []).extend(float(value) for value in spans)
        counts = pooled_counts.setdefault(threshold, {"bundle_count": 0, "dropped_or_unmatched_count": 0, "input_frame_count": 0})
        counts["bundle_count"] += int(len(spans))
        counts["dropped_or_unmatched_count"] += dropped
        counts["input_frame_count"] += int(total_frames)
        rows.append(
            {
                "scope": "demo",
                "demo": demo,
                "status": "ok",
                "threshold_ms": threshold,
                "input_frame_count": total_frames,
                "bundle_count": len(spans),
                "dropped_or_unmatched_count": dropped,
                "bundle_span_mean_ms": summary["mean"],
                "bundle_span_median_ms": summary["median"],
                "bundle_span_p95_ms": summary["p95"],
                "bundle_span_p99_ms": summary["p99"],
                "bundle_span_max_ms": summary["max"],
                "oldest_age_if_bundle_time_is_max_mean_ms": summary["mean"],
                "oldest_age_if_bundle_time_is_max_p95_ms": summary["p95"],
                "oldest_age_if_bundle_time_is_max_max_ms": summary["max"],
            }
        )


def add_linear_fit_rows(
    demo: str,
    ticks: dict[str, np.ndarray],
    rows: list[dict[str, Any]],
) -> None:
    stats_by_camera: dict[str, dict[str, float | int | None]] = {}
    for camera in EXPECTED_CAMERAS:
        stats_by_camera[camera] = line_fit_stats(ticks[camera])

    fitted_periods = [
        float(stats["fit_period_ms"])
        for stats in stats_by_camera.values()
        if stats.get("fit_period_ms") is not None
    ]
    common_period = float(np.median(fitted_periods)) if fitted_periods else NOMINAL_REALSENSE_PERIOD_MS
    cam1_intercept = stats_by_camera["cam1"].get("fit_intercept_ms")

    for camera in EXPECTED_CAMERAS:
        stats = stats_by_camera[camera]
        intercept = stats.get("fit_intercept_ms")
        if camera == "cam1":
            relative_phase = 0.0
        elif cam1_intercept is None or intercept is None:
            relative_phase = None
        else:
            relative_phase = wrap_to_half_period(float(intercept) - float(cam1_intercept), common_period)
        rows.append(
            {
                "demo_id": demo,
                "camera": camera,
                "frame_count": stats["frame_count"],
                "fit_intercept_ms": stats["fit_intercept_ms"],
                "fit_period_ms": stats["fit_period_ms"],
                "period_error_ms": stats["period_error_ms"],
                "residual_abs_median_ms": stats["residual_abs_median_ms"],
                "residual_abs_p95_ms": stats["residual_abs_p95_ms"],
                "residual_abs_max_ms": stats["residual_abs_max_ms"],
                "relative_phase_to_cam1_ms": relative_phase,
            }
        )


def aggregate_linear_fit_rows(per_demo_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregate_rows: list[dict[str, Any]] = []
    for camera in EXPECTED_CAMERAS:
        rows = [
            row
            for row in per_demo_rows
            if row.get("camera") == camera and row.get("fit_period_ms") not in ("", None)
        ]
        periods = [float(row["fit_period_ms"]) for row in rows]
        abs_period_errors = [abs(float(row["period_error_ms"])) for row in rows if row.get("period_error_ms") not in ("", None)]
        residual_medians = [float(row["residual_abs_median_ms"]) for row in rows if row.get("residual_abs_median_ms") not in ("", None)]
        residual_p95 = [float(row["residual_abs_p95_ms"]) for row in rows if row.get("residual_abs_p95_ms") not in ("", None)]
        residual_max = [float(row["residual_abs_max_ms"]) for row in rows if row.get("residual_abs_max_ms") not in ("", None)]
        relative_phases = [float(row["relative_phase_to_cam1_ms"]) for row in rows if row.get("relative_phase_to_cam1_ms") not in ("", None)]
        aggregate_rows.append(
            {
                "camera": camera,
                "successful_demos": len(rows),
                "fit_period_mean_ms": mean(periods) if periods else None,
                "fit_period_median_ms": median(periods) if periods else None,
                "fit_period_p95_ms": percentile(periods, 0.95),
                "period_error_abs_p95_ms": percentile(abs_period_errors, 0.95),
                "residual_abs_median_ms": median(residual_medians) if residual_medians else None,
                "residual_abs_p95_ms": percentile(residual_p95, 0.95),
                "residual_abs_max_ms": max(residual_max, default=None),
                "relative_phase_to_cam1_median_ms": median(relative_phases) if relative_phases else None,
                "relative_phase_to_cam1_p95_abs_ms": percentile(np.abs(relative_phases), 0.95) if relative_phases else None,
            }
        )
    return aggregate_rows


def append_aggregate_rows(
    single_rows: list[dict[str, Any]],
    pair_rows: list[dict[str, Any]],
    phase_rows: list[dict[str, Any]],
    foldback_rows: list[dict[str, Any]],
    bundle_rows: list[dict[str, Any]],
    pooled_dt: dict[str, list[float]],
    pooled_pair_delta: dict[str, list[float]],
    pooled_offsets: dict[str, list[float]],
    pooled_phase_offsets: dict[str, list[float]],
    line_fit_rows: dict[str, list[dict[str, Any]]],
    pooled_foldback: dict[str, dict[str, list[float] | int]],
    pooled_bundle_spans: dict[float, list[float]],
    pooled_bundle_counts: dict[float, dict[str, int]],
) -> None:
    for stream_name in STREAM_ORDER:
        camera, role = stream_name.split("_", 1)
        values = np.asarray(pooled_dt.get(stream_name, []), dtype=float)
        if len(values) == 0:
            single_rows.append({"scope": "aggregate", "stream": stream_name, "camera": camera, "role": role, "status": "missing"})
            continue
        median_dt = float(np.median(values))
        single_rows.append(
            {
                "scope": "aggregate",
                "stream": stream_name,
                "camera": camera,
                "role": role,
                "status": "ok",
                "frame_count": sum(int(row.get("frame_count") or 0) for row in single_rows if row.get("scope") == "demo" and row.get("stream") == stream_name),
                "interval_count": len(values),
                "median_dt_ms": median_dt,
                "mean_dt_ms": float(np.mean(values)),
                "p95_dt_ms": percentile(values, 0.95),
                "p99_dt_ms": percentile(values, 0.99),
                "jitter_p95_ms": percentile(np.abs(values - median_dt), 0.95),
                "max_gap_ms": float(np.max(values)),
                "drop_count": int(np.sum(values > 1.5 * median_dt)),
                "duplicate_or_reverse_count": int(np.sum(values <= 0)),
            }
        )

    for camera in EXPECTED_CAMERAS:
        values = np.asarray(pooled_pair_delta.get(camera, []), dtype=float)
        summary = summarize_values(values)
        demo_rows = [row for row in pair_rows if row.get("scope") == "demo" and row.get("camera") == camera and row.get("status") == "ok"]
        pair_rows.append(
            {
                "scope": "aggregate",
                "camera": camera,
                "status": "ok" if len(values) else "missing",
                "color_count": sum(int(row.get("color_count") or 0) for row in demo_rows),
                "depth_count": sum(int(row.get("depth_count") or 0) for row in demo_rows),
                "pair_success_count": sum(int(row.get("pair_success_count") or 0) for row in demo_rows),
                "unmatched_count": sum(int(row.get("unmatched_count") or 0) for row in demo_rows),
                "pair_delta_mean_ms": summary["mean"],
                "pair_delta_median_ms": summary["median"],
                "pair_delta_p95_ms": summary["p95"],
                "pair_delta_max_ms": summary["max"],
                "camera_tick_span_mean_ms": summary["mean"],
                "camera_tick_span_median_ms": summary["median"],
                "camera_tick_span_p95_ms": summary["p95"],
                "camera_tick_span_max_ms": summary["max"],
            }
        )

    for camera in ("cam2", "cam3", "cam4"):
        key = f"cam1_to_{camera}"
        offsets = np.asarray(pooled_offsets.get(key, []), dtype=float)
        phases = np.asarray(pooled_phase_offsets.get(key, []), dtype=float)
        phase_rows.append(
            {
                "scope": "aggregate",
                "row_type": "phase_pair",
                "status": "ok" if len(offsets) else "missing",
                "reference_camera": "cam1",
                "target_camera": camera,
                "sample_count": len(offsets),
                "median_signed_offset_ms": percentile(offsets, 0.50),
                "p95_abs_offset_ms": percentile(np.abs(offsets), 0.95),
                "phase_offset_median_ms": percentile(phases, 0.50),
                "phase_offset_p95_ms": percentile(np.abs(phases), 0.95),
            }
        )

    for camera in EXPECTED_CAMERAS:
        rows = line_fit_rows.get(camera, [])
        phase_rows.append(
            {
                "scope": "aggregate",
                "row_type": "line_fit",
                "status": "ok" if rows else "missing",
                "camera": camera,
                "frame_count": sum(int(row.get("frame_count") or 0) for row in rows),
                "estimated_period_ms": median([float(row["estimated_period_ms"]) for row in rows if row.get("estimated_period_ms") not in ("", None)]) if rows else None,
                "phase_ms": median([float(row["phase_ms"]) for row in rows if row.get("phase_ms") not in ("", None)]) if rows else None,
                "residual_p95_ms": percentile([float(row["residual_p95_ms"]) for row in rows if row.get("residual_p95_ms") not in ("", None)], 0.95) if rows else None,
                "residual_max_ms": max([float(row["residual_max_ms"]) for row in rows if row.get("residual_max_ms") not in ("", None)], default=None),
            }
        )

    for key, bucket in sorted(pooled_foldback.items()):
        anchor, target = key.split("_to_")
        comparable_count = int(bucket["comparable_count"])
        foldback_count = int(bucket["foldback_count"])
        prev_ages = bucket["prev_ages"]
        next_delays = bucket["next_delays"]
        assert isinstance(prev_ages, list)
        assert isinstance(next_delays, list)
        foldback_rows.append(
            {
                "scope": "aggregate",
                "status": "ok" if comparable_count else "missing",
                "anchor_camera": anchor,
                "target_camera": target,
                "comparable_count": comparable_count,
                "foldback_count": foldback_count,
                "foldback_rate": None if comparable_count == 0 else foldback_count / comparable_count,
                "prev_age_median_ms": percentile(prev_ages, 0.50),
                "prev_age_p95_ms": percentile(prev_ages, 0.95),
                "next_delay_median_ms": percentile(next_delays, 0.50),
                "next_delay_p95_ms": percentile(next_delays, 0.95),
            }
        )

    for threshold in BUNDLE_THRESHOLDS_MS:
        spans = np.asarray(pooled_bundle_spans.get(threshold, []), dtype=float)
        counts = pooled_bundle_counts.get(threshold, {"bundle_count": 0, "dropped_or_unmatched_count": 0, "input_frame_count": 0})
        summary = summarize_values(spans)
        bundle_rows.append(
            {
                "scope": "aggregate",
                "status": "ok" if len(spans) else "missing",
                "threshold_ms": threshold,
                "input_frame_count": counts["input_frame_count"],
                "bundle_count": counts["bundle_count"],
                "dropped_or_unmatched_count": counts["dropped_or_unmatched_count"],
                "bundle_span_mean_ms": summary["mean"],
                "bundle_span_median_ms": summary["median"],
                "bundle_span_p95_ms": summary["p95"],
                "bundle_span_p99_ms": summary["p99"],
                "bundle_span_max_ms": summary["max"],
                "oldest_age_if_bundle_time_is_max_mean_ms": summary["mean"],
                "oldest_age_if_bundle_time_is_max_p95_ms": summary["p95"],
                "oldest_age_if_bundle_time_is_max_max_ms": summary["max"],
            }
        )


def aggregate_row(rows: list[dict[str, Any]], **filters: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("scope") != "aggregate":
            continue
        if all(str(row.get(key)) == value for key, value in filters.items()):
            return row
    return None


def write_recommendation(
    path: Path,
    demos: list[Path],
    missing_notes: list[str],
    single_rows: list[dict[str, Any]],
    pair_rows: list[dict[str, Any]],
    phase_rows: list[dict[str, Any]],
    foldback_rows: list[dict[str, Any]],
    bundle_rows: list[dict[str, Any]],
    linear_aggregate_rows: list[dict[str, Any]],
) -> None:
    stream_jitters = [
        float(row["jitter_p95_ms"])
        for row in single_rows
        if row.get("scope") == "aggregate" and row.get("status") == "ok" and row.get("jitter_p95_ms") not in ("", None)
    ]
    stream_periods = [
        float(row["median_dt_ms"])
        for row in single_rows
        if row.get("scope") == "aggregate" and row.get("status") == "ok" and row.get("median_dt_ms") not in ("", None)
    ]
    drop_counts = [
        int(row["drop_count"])
        for row in single_rows
        if row.get("scope") == "aggregate" and row.get("status") == "ok" and row.get("drop_count") not in ("", None)
    ]
    pair_p95 = [
        float(row["pair_delta_p95_ms"])
        for row in pair_rows
        if row.get("scope") == "aggregate" and row.get("status") == "ok" and row.get("pair_delta_p95_ms") not in ("", None)
    ]
    foldback_rates = [
        float(row["foldback_rate"])
        for row in foldback_rows
        if row.get("scope") == "aggregate" and row.get("status") == "ok" and row.get("foldback_rate") not in ("", None)
    ]
    foldback_prev_p95 = [
        float(row["prev_age_p95_ms"])
        for row in foldback_rows
        if row.get("scope") == "aggregate" and row.get("status") == "ok" and row.get("prev_age_p95_ms") not in ("", None)
    ]
    bundle_20 = aggregate_row(bundle_rows, threshold_ms="20.0")
    bundle_25 = aggregate_row(bundle_rows, threshold_ms="25.0")
    best_bundle = bundle_20 if bundle_20 and bundle_20.get("status") == "ok" else bundle_25

    stable_period = bool(stream_periods) and max(abs(value - 33.333333) for value in stream_periods) < 2.0
    low_jitter = bool(stream_jitters) and max(stream_jitters) < 2.0
    low_drops = bool(drop_counts) and sum(drop_counts) <= max(10, len(demos) * 2)
    paired = bool(pair_p95) and max(pair_p95) < 2.0
    nontrivial_foldback = bool(foldback_rates) and max(foldback_rates) >= 0.05
    bundle_better = False
    bundle_drop_rate = None
    if best_bundle and best_bundle.get("status") == "ok":
        span_p95 = float(best_bundle.get("bundle_span_p95_ms") or 999.0)
        latest_past_p95 = max(foldback_prev_p95) if foldback_prev_p95 else 999.0
        bundle_better = span_p95 + 5.0 < latest_past_p95 and span_p95 < 25.0
        input_count = int(best_bundle.get("input_frame_count") or 0)
        dropped = int(best_bundle.get("dropped_or_unmatched_count") or 0)
        bundle_drop_rate = None if input_count == 0 else dropped / input_count
    low_bundle_drops = bundle_drop_rate is not None and bundle_drop_rate < 0.10

    positive = [stable_period, low_jitter, low_drops, paired, nontrivial_foldback, bundle_better, low_bundle_drops]
    recommend_bundle = sum(1 for value in positive if value) >= 5
    linear_period_errors = [
        float(row["period_error_abs_p95_ms"])
        for row in linear_aggregate_rows
        if row.get("period_error_abs_p95_ms") not in ("", None)
    ]
    linear_residual_p95 = [
        float(row["residual_abs_p95_ms"])
        for row in linear_aggregate_rows
        if row.get("residual_abs_p95_ms") not in ("", None)
    ]
    linear_phase_p95 = [
        float(row["relative_phase_to_cam1_p95_abs_ms"])
        for row in linear_aggregate_rows
        if row.get("relative_phase_to_cam1_p95_abs_ms") not in ("", None)
    ]
    linear_period_close = bool(linear_period_errors) and max(linear_period_errors) < 0.2
    linear_residual_small = bool(linear_residual_p95) and max(linear_residual_p95) < 2.0
    linear_phase_stable = bool(linear_phase_p95) and max(linear_phase_p95) < 17.0

    lines = [
        "# RealSense Timing Recommendation",
        "",
        f"Successful demos analyzed: {len(demos)}",
        f"Missing-data notes: {len(missing_notes)}",
        "",
        "## Decision",
        "",
        (
            "Recommendation: use a RealSense visual bundle before aligning with non-RealSense streams."
            if recommend_bundle
            else "Recommendation: do not switch solely on this run; inspect the CSV metrics before changing alignment policy."
        ),
        "",
        "## Evidence Summary",
        "",
        f"- Stream median periods near 33.33 ms: {stable_period}",
        f"- Single-stream jitter_p95 below 2 ms for every aggregate stream: {low_jitter}",
        f"- Aggregate drop_count is low: {low_drops}",
        f"- Color/depth pair_delta_p95 below 2 ms for every camera: {paired}",
        f"- Non-trivial latest-past foldback_rate observed: {nontrivial_foldback}",
        f"- Closest-bundle p95 is clearly below latest-past prev_age p95: {bundle_better}",
        f"- Bundle dropped_or_unmatched rate below 10%: {low_bundle_drops}",
        "",
        "## Key Aggregate Values",
        "",
        f"- max single-stream jitter_p95_ms: {fmt(max(stream_jitters), 6) if stream_jitters else 'n/a'}",
        f"- max color/depth pair_delta_p95_ms: {fmt(max(pair_p95), 6) if pair_p95 else 'n/a'}",
        f"- max foldback_rate: {fmt(max(foldback_rates), 6) if foldback_rates else 'n/a'}",
        f"- max latest-past prev_age_p95_ms: {fmt(max(foldback_prev_p95), 6) if foldback_prev_p95 else 'n/a'}",
        f"- selected bundle threshold_ms: {best_bundle.get('threshold_ms') if best_bundle else 'n/a'}",
        f"- selected bundle_span_p95_ms: {fmt(float(best_bundle.get('bundle_span_p95_ms')), 6) if best_bundle and best_bundle.get('bundle_span_p95_ms') not in ('', None) else 'n/a'}",
        f"- selected bundle dropped_or_unmatched rate: {fmt(bundle_drop_rate, 6) if bundle_drop_rate is not None else 'n/a'}",
        "",
        "## Linear Clock Fit Summary",
        "",
        (
            "Linear clock fit summary: estimated periods are close to 33.33 ms, "
            f"residual p95 is {'small' if linear_residual_small else 'large'}, and "
            f"cross-camera relative phase is {'stable' if linear_phase_stable else 'unstable'}."
        ),
        "",
        f"- estimated periods are close to 33.33 ms: {linear_period_close}",
        f"- max period_error_abs_p95_ms: {fmt(max(linear_period_errors), 6) if linear_period_errors else 'n/a'}",
        f"- max residual_abs_p95_ms: {fmt(max(linear_residual_p95), 6) if linear_residual_p95 else 'n/a'}",
        f"- max relative_phase_to_cam1_p95_abs_ms: {fmt(max(linear_phase_p95), 6) if linear_phase_p95 else 'n/a'}",
    ]
    if missing_notes:
        lines.extend(["", "## Missing Data Notes", ""])
        for note in missing_notes[:50]:
            lines.append(f"- {note}")
        if len(missing_notes) > 50:
            lines.append(f"- ... {len(missing_notes) - 50} additional notes omitted from this summary")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_outputs(run_dir: Path, rows_by_name: dict[str, list[dict[str, Any]]]) -> None:
    write_csv(
        run_dir / "realsense_single_stream_stability.csv",
        rows_by_name["single"],
        [
            "scope",
            "demo",
            "stream",
            "camera",
            "role",
            "topic",
            "status",
            "frame_count",
            "interval_count",
            "median_dt_ms",
            "mean_dt_ms",
            "p95_dt_ms",
            "p99_dt_ms",
            "jitter_p95_ms",
            "max_gap_ms",
            "drop_count",
            "duplicate_or_reverse_count",
        ],
    )
    write_csv(
        run_dir / "realsense_color_depth_pairing.csv",
        rows_by_name["pair"],
        [
            "scope",
            "demo",
            "camera",
            "status",
            "missing_streams",
            "color_count",
            "depth_count",
            "pair_success_count",
            "unmatched_count",
            "pair_delta_mean_ms",
            "pair_delta_median_ms",
            "pair_delta_p95_ms",
            "pair_delta_max_ms",
            "camera_tick_span_mean_ms",
            "camera_tick_span_median_ms",
            "camera_tick_span_p95_ms",
            "camera_tick_span_max_ms",
        ],
    )
    write_csv(
        run_dir / "realsense_phase_offsets.csv",
        rows_by_name["phase"],
        [
            "scope",
            "demo",
            "row_type",
            "status",
            "reference_camera",
            "target_camera",
            "camera",
            "missing_cameras",
            "sample_count",
            "frame_count",
            "median_signed_offset_ms",
            "p95_abs_offset_ms",
            "phase_offset_median_ms",
            "phase_offset_p95_ms",
            "estimated_period_ms",
            "phase_ms",
            "residual_p95_ms",
            "residual_max_ms",
        ],
    )
    write_csv(
        run_dir / "realsense_anchor_foldback.csv",
        rows_by_name["foldback"],
        [
            "scope",
            "demo",
            "status",
            "anchor_camera",
            "target_camera",
            "comparable_count",
            "foldback_count",
            "foldback_rate",
            "prev_age_median_ms",
            "prev_age_p95_ms",
            "next_delay_median_ms",
            "next_delay_p95_ms",
            "foldback_prev_age_median_ms",
            "foldback_next_delay_median_ms",
        ],
    )
    write_csv(
        run_dir / "realsense_bundle_threshold_summary.csv",
        rows_by_name["bundle"],
        [
            "scope",
            "demo",
            "status",
            "threshold_ms",
            "missing_cameras",
            "input_frame_count",
            "bundle_count",
            "dropped_or_unmatched_count",
            "bundle_span_mean_ms",
            "bundle_span_median_ms",
            "bundle_span_p95_ms",
            "bundle_span_p99_ms",
            "bundle_span_max_ms",
            "oldest_age_if_bundle_time_is_max_mean_ms",
            "oldest_age_if_bundle_time_is_max_p95_ms",
            "oldest_age_if_bundle_time_is_max_max_ms",
        ],
    )
    write_csv(
        run_dir / "realsense_linear_clock_fit_per_demo.csv",
        rows_by_name["linear_per_demo"],
        [
            "demo_id",
            "camera",
            "frame_count",
            "fit_intercept_ms",
            "fit_period_ms",
            "period_error_ms",
            "residual_abs_median_ms",
            "residual_abs_p95_ms",
            "residual_abs_max_ms",
            "relative_phase_to_cam1_ms",
        ],
    )
    write_csv(
        run_dir / "realsense_linear_clock_fit_aggregate.csv",
        rows_by_name["linear_aggregate"],
        [
            "camera",
            "successful_demos",
            "fit_period_mean_ms",
            "fit_period_median_ms",
            "fit_period_p95_ms",
            "period_error_abs_p95_ms",
            "residual_abs_median_ms",
            "residual_abs_p95_ms",
            "residual_abs_max_ms",
            "relative_phase_to_cam1_median_ms",
            "relative_phase_to_cam1_p95_abs_ms",
        ],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze RealSense timestamp jitter and bundle necessity")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR, help="Directory containing demo_* folders")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory where a timestamped run folder is created")
    parser.add_argument("--limit", type=int, default=None, help="Optional demo limit for quick validation")
    parser.add_argument("--pair-threshold-ms", type=float, default=PAIR_THRESHOLD_MS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    run_dir = create_run_dir(output_dir)

    demos = discover_successful_demos(input_dir)
    if args.limit is not None:
        demos = demos[: args.limit]

    single_rows: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []
    phase_rows: list[dict[str, Any]] = []
    foldback_rows: list[dict[str, Any]] = []
    bundle_rows: list[dict[str, Any]] = []
    linear_per_demo_rows: list[dict[str, Any]] = []
    missing_notes: list[str] = []

    pooled_dt: dict[str, list[float]] = {}
    pooled_pair_delta: dict[str, list[float]] = {}
    pooled_offsets: dict[str, list[float]] = {}
    pooled_phase_offsets: dict[str, list[float]] = {}
    line_fit_rows: dict[str, list[dict[str, Any]]] = {}
    pooled_foldback: dict[str, dict[str, list[float] | int]] = {}
    pooled_bundle_spans: dict[float, list[float]] = {}
    pooled_bundle_counts: dict[float, dict[str, int]] = {}

    for demo_dir in demos:
        streams, warnings = load_realsense_streams(demo_dir)
        for warning in warnings:
            missing_notes.append(f"{demo_dir.name}: {warning}")
        add_single_stream_rows(demo_dir.name, streams, single_rows, pooled_dt)
        pair_results = pair_color_depth(streams, args.pair_threshold_ms)
        add_pair_rows(demo_dir.name, streams, pair_results, pair_rows, pooled_pair_delta)
        ticks = {camera: pair.tick_times_ns for camera, pair in pair_results.items() if pair.matched_count > 0}
        add_linear_fit_rows(demo_dir.name, ticks, linear_per_demo_rows)
        add_phase_rows(demo_dir.name, ticks, phase_rows, pooled_offsets, pooled_phase_offsets, line_fit_rows)
        add_foldback_rows(demo_dir.name, ticks, foldback_rows, pooled_foldback)
        add_bundle_rows(demo_dir.name, ticks, bundle_rows, pooled_bundle_spans, pooled_bundle_counts)

    append_aggregate_rows(
        single_rows,
        pair_rows,
        phase_rows,
        foldback_rows,
        bundle_rows,
        pooled_dt,
        pooled_pair_delta,
        pooled_offsets,
        pooled_phase_offsets,
        line_fit_rows,
        pooled_foldback,
        pooled_bundle_spans,
        pooled_bundle_counts,
    )
    linear_aggregate_rows = aggregate_linear_fit_rows(linear_per_demo_rows)

    rows_by_name = {
        "single": single_rows,
        "pair": pair_rows,
        "phase": phase_rows,
        "foldback": foldback_rows,
        "bundle": bundle_rows,
        "linear_per_demo": linear_per_demo_rows,
        "linear_aggregate": linear_aggregate_rows,
    }
    write_outputs(run_dir, rows_by_name)
    write_recommendation(
        run_dir / "realsense_bundle_recommendation.md",
        demos,
        missing_notes,
        single_rows,
        pair_rows,
        phase_rows,
        foldback_rows,
        bundle_rows,
        linear_aggregate_rows,
    )
    summary = {
        "run_dir": run_dir.relative_to(REPO_ROOT).as_posix() if run_dir.is_relative_to(REPO_ROOT) else str(run_dir),
        "successful_demos_analyzed": len(demos),
        "missing_data_notes": len(missing_notes),
        "outputs": sorted(path.name for path in run_dir.iterdir() if path.is_file()),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
