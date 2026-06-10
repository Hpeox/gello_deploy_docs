#!/usr/bin/env python3
"""Standalone timestamp alignment tool for one MainController demo.

This CLI intentionally does not import main_controller.timestamp_alignment, so it
can be copied or evolved independently from the controller package.

Typical usage from the repository root:

    python tools/align_demo_timestamps.py \
      --demo-dir runtime_sessions/demos/demo_YYYYmmdd_HHMMSS \
      --repo-root . \
      --alignment-base-source realsense \
      --mode causal \
      --start-trim-s 2.0

Arguments:

    --demo-dir PATH
        Required demo directory containing manifest.json and the timestamp npz
        files referenced by that manifest.
    --output-dir PATH
        Optional output directory. Defaults to <demo-dir>/aligned.
    --repo-root PATH
        Optional repository root used by callers for stable path context.
        Defaults to the parent directory of this tools/ directory.
    --alignment-base-source {realsense,xense}
        Base-source policy used when --base is auto. The realsense default keeps
        the historical behavior and picks a required RealSense color image topic
        when available. The xense option uses the same-row Xense pair timeline,
        i.e. max(timestamp_ns_0, timestamp_ns_1).
    --base VALUE
        Explicit target timeline override. Supported values include auto,
        realsense:<topic>, realsense:bundle, xense:pair, xense:0, xense:1,
        robot, and grid.
    --mode {causal,nearest}
        Matching policy. causal uses the latest stream sample at or before each
        target time. nearest uses the nearest stream sample within tolerance.
    --hz FLOAT
        Grid frequency in Hz when --base grid is selected. Default is 30.0.
    --start-trim-s FLOAT
        Seconds trimmed from the beginning of the overlapping timeline.
        This trims samples only; it does not mutate raw timestamps.
    --stream-start-trim STREAM=SECONDS
        Per-stream start trim. May be repeated, for example
        --stream-start-trim zmq_source_1=1.5.
    --allow-degraded
        Allow alignment for manifests whose status is not done. Default behavior
        requires manifest.status == "done".

The tool writes alignment_config.json, aligned_index.npz, aligned_manifest.json,
and alignment_report.md, then prints a JSON summary to stdout.
"""

from __future__ import annotations

import argparse
from itertools import product
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


NSEC_PER_SEC = 1_000_000_000
ZMQ_CLOCK_OFFSET_WARN_MS = 100.0
ZMQ_CLOCK_OFFSET_CHECK_HINT = 'check chrony/NTP sync: run `chronyc sources -v`; expected first line: ^*192.168.10.1'
REPO_ROOT = Path(__file__).resolve().parents[1]
REALSENSE_BUNDLE_INITIAL_SEARCH_RADIUS = 2
REALSENSE_BUNDLE_FALLBACK_SEARCH_RADIUS = 1
REALSENSE_BUNDLE_SPAN_WARN_NS = 20_000_000


@dataclass(frozen=True)
class Options:
    repo_root: Path = REPO_ROOT
    output_dir: Path | None = None
    base: str = 'auto'
    alignment_base_source: str = 'realsense'
    mode: str = 'causal'
    hz: float = 30.0
    start_trim_s: float = 0.0
    stream_start_trim: dict[str, float] = field(default_factory=dict)
    allow_degraded: bool = False


@dataclass
class Stream:
    name: str
    display_name: str
    time_ns: np.ndarray
    source_index: np.ndarray
    tolerance_causal_ns: int
    tolerance_nearest_ns: int
    frame_number: np.ndarray | None = None
    topic: str | None = None
    recorded_time_ns: np.ndarray | None = None
    timestamp_source: str = 'npz'

    def sorted_valid(self) -> 'Stream':
        valid = self.time_ns > 0
        order = np.argsort(self.time_ns[valid], kind='stable')
        indices = np.nonzero(valid)[0][order]
        frame_number = None if self.frame_number is None else self.frame_number[indices]
        recorded_time_ns = None if self.recorded_time_ns is None else self.recorded_time_ns[indices]
        return Stream(
            self.name,
            self.display_name,
            self.time_ns[indices].astype(np.int64, copy=False),
            self.source_index[indices].astype(np.int64, copy=False),
            self.tolerance_causal_ns,
            self.tolerance_nearest_ns,
            frame_number,
            self.topic,
            recorded_time_ns,
            self.timestamp_source,
        )


@dataclass
class RosbagImageStream:
    header_time_ns: np.ndarray
    recorded_time_ns: np.ndarray


@dataclass
class TargetTimeline:
    t_ns: np.ndarray
    kind: str
    direct_matches: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)
    arrays: dict[str, np.ndarray] = field(default_factory=dict)
    base_details: dict[str, Any] = field(default_factory=dict)


def align_demo(demo_dir: Path, options: Options) -> dict[str, Any]:
    demo_dir = demo_dir.resolve()
    output_dir = (options.output_dir or demo_dir / 'aligned').resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = read_json(demo_dir / 'manifest.json')
    if manifest.get('status') != 'done' and not options.allow_degraded:
        raise RuntimeError(f"alignment requires manifest.status == 'done', got {manifest.get('status')!r}")

    warnings: list[str] = []
    npz_paths = resolve_npz_paths(demo_dir, manifest)
    streams, zmq_clock_offsets = load_streams(demo_dir, manifest, npz_paths, warnings)
    streams = {name: stream for name, stream in streams.items() if len(stream.time_ns) > 0}
    if not streams:
        raise RuntimeError('no timestamp streams found')

    base = resolve_base(options, manifest, streams)
    timeline = build_target_timeline(base, streams, demo_dir, manifest, npz_paths, options, warnings)
    t_ns = timeline.t_ns
    if len(t_ns) == 0:
        raise RuntimeError('target timeline is empty after trims')

    arrays: dict[str, np.ndarray] = {
        't_ns': t_ns,
        'segment_id': np.zeros(len(t_ns), dtype=np.int64),
    }
    arrays.update(timeline.arrays)
    stats: dict[str, dict[str, Any]] = {}
    valid_masks: list[np.ndarray] = []
    for stream in streams.values():
        match = timeline.direct_matches.get(stream.name)
        if match is None:
            match = match_stream(t_ns, stream, options.mode)
        arrays[f'{stream.name}_index'] = match['index']
        arrays[f'{stream.name}_time_ns'] = match['time_ns']
        arrays[f'{stream.name}_delta_ns'] = match['delta_ns']
        arrays[f'{stream.name}_valid'] = match['valid']
        if stream.frame_number is not None:
            frame_number = np.full(len(t_ns), -1, dtype=np.int64)
            good = match['position'] >= 0
            frame_number[good] = stream.frame_number[match['position'][good]]
            arrays[f'{stream.name}_frame_number'] = frame_number
        if stream.topic is not None:
            arrays[f'{stream.name}_topic'] = np.asarray([stream.topic] * len(t_ns))
        if stream.recorded_time_ns is not None:
            recorded_time = np.full(len(t_ns), -1, dtype=np.int64)
            good = match['position'] >= 0
            recorded_time[good] = stream.recorded_time_ns[match['position'][good]]
            arrays[f'{stream.name}_recorded_time_ns'] = recorded_time
        valid_masks.append(match['valid'])
        stats[stream.name] = stream_stats(stream, match)

    sample_valid = np.logical_and.reduce(valid_masks) if valid_masks else np.ones(len(t_ns), dtype=bool)
    arrays['sample_valid'] = sample_valid
    index_path = output_dir / 'aligned_index.npz'
    np.savez(index_path, **arrays)

    sources = source_paths(manifest)
    config = {
        'demo_dir': '.',
        'output_dir': relative_to(output_dir, demo_dir),
        'base': base,
        'requested_base': options.base,
        'alignment_base_source': options.alignment_base_source,
        'resolved_base_kind': timeline.kind,
        'mode': options.mode,
        'hz': options.hz,
        'start_trim_s': options.start_trim_s,
        'stream_start_trim': options.stream_start_trim,
        'sources': sources,
        'streams': {name: {'display_name': stream.display_name, 'topic': stream.topic} for name, stream in streams.items()},
        'base_details': timeline.base_details,
    }
    config_path = output_dir / 'alignment_config.json'
    write_json(config_path, config)

    aligned_manifest = {
        'status': 'done',
        'demo_dir': '.',
        'sample_count': int(len(t_ns)),
        'valid_count': int(sample_valid.sum()),
        'base': base,
        'resolved_base_kind': timeline.kind,
        'mode': options.mode,
        'hz': options.hz,
        'sources': sources,
        'streams': stats,
        'base_details': timeline.base_details,
        'zmq_clock_offsets': zmq_clock_offsets,
        'clock_domain': clock_domain_summary(npz_paths.get('realsense'), warnings),
        'drop_monitors': manifest.get('drop_monitors', {}),
        'warnings': warnings,
    }
    aligned_manifest_path = output_dir / 'aligned_manifest.json'
    write_json(aligned_manifest_path, aligned_manifest)
    report_path = output_dir / 'alignment_report.md'
    report_path.write_text(render_report(aligned_manifest), encoding='utf-8')
    return {
        'config_path': relative_to(config_path, demo_dir),
        'index_path': relative_to(index_path, demo_dir),
        'manifest_path': relative_to(aligned_manifest_path, demo_dir),
        'report_path': relative_to(report_path, demo_dir),
        'sample_count': int(len(t_ns)),
        'valid_count': int(sample_valid.sum()),
        'base': base,
        'resolved_base_kind': timeline.kind,
        'zmq_clock_offsets': zmq_clock_offsets,
        'warnings': warnings,
    }


def load_streams(
    demo_dir: Path,
    manifest: dict[str, Any],
    npz_paths: dict[str, Path],
    warnings: list[str],
) -> tuple[dict[str, Stream], dict[str, dict[str, Any]]]:
    streams: dict[str, Stream] = {}
    zmq_clock_offsets: dict[str, dict[str, Any]] = {}
    if 'ft300' in npz_paths:
        data = np.load(npz_paths['ft300'], allow_pickle=True)
        streams['ft300s'] = Stream('ft300s', 'FT300S', int_array(data['timestamp_ns']), np.arange(len(data['timestamp_ns']), dtype=np.int64), 20_000_000, 10_000_000).sorted_valid()
    if 'xense' in npz_paths:
        data = np.load(npz_paths['xense'], allow_pickle=True)
        streams['xense_0'] = Stream('xense_0', 'Xense sensor 0', int_array(data['timestamp_ns_0']), np.arange(len(data['timestamp_ns_0']), dtype=np.int64), 66_700_000, 33_400_000).sorted_valid()
        streams['xense_1'] = Stream('xense_1', 'Xense sensor 1', int_array(data['timestamp_ns_1']), np.arange(len(data['timestamp_ns_1']), dtype=np.int64), 66_700_000, 33_400_000).sorted_valid()
    if 'zmq' in npz_paths:
        data = np.load(npz_paths['zmq'], allow_pickle=True)
        sources = int_array(data['source'])
        raw_stamp_ns = np.asarray([int(round(float(value) * NSEC_PER_SEC)) for value in data['stamp_s']], dtype=np.int64)
        recv_time_ns = int_array(data['recv_time_ns'])
        for source in sorted(set(int(value) for value in sources if value > 0)):
            mask = sources == source
            offset_ns = int(np.median(recv_time_ns[mask] - raw_stamp_ns[mask]))
            stream_name = f'zmq_source_{source}'
            offset_ms = float(offset_ns / 1_000_000.0)
            zmq_clock_offsets[stream_name] = {
                'source': source,
                'offset_ms': offset_ms,
                'offset_ns': offset_ns,
                'frame_count': int(mask.sum()),
            }
            if abs(offset_ms) > ZMQ_CLOCK_OFFSET_WARN_MS:
                warnings.append(zmq_clock_offset_warning(source, offset_ms))
            streams[stream_name] = Stream(
                stream_name,
                f'ZMQ source {source}',
                raw_stamp_ns[mask] + offset_ns,
                np.nonzero(mask)[0].astype(np.int64),
                40_000_000,
                20_000_000,
            ).sorted_valid()
    streams.update(realsense_streams(demo_dir, manifest, npz_paths.get('realsense'), warnings))
    return streams, zmq_clock_offsets


def zmq_clock_offset_warning(source: int, offset_ms: float) -> str:
    return (
        f'ZMQ source {source} clock offset {offset_ms:.3f} ms exceeds '
        f'{ZMQ_CLOCK_OFFSET_WARN_MS:.3f} ms; {ZMQ_CLOCK_OFFSET_CHECK_HINT}'
    )


def realsense_streams(demo_dir: Path, manifest: dict[str, Any], npz_path: Path | None, warnings: list[str]) -> dict[str, Stream]:
    if npz_path is None:
        return {}
    metadata = np.load(npz_path, allow_pickle=True)
    topics = np.asarray(metadata['topic']).astype(str)
    metadata_by_topic: dict[str, dict[str, np.ndarray]] = {}
    for topic in sorted(set(topics)):
        mask = topics == topic
        metadata_by_topic[topic] = {
            'time_ns': int_array(metadata['header_stamp_ns'][mask]),
            'source_index': np.nonzero(mask)[0].astype(np.int64),
            'frame_number': int_array(metadata['frame_number'][mask]),
        }
    required_topics = required_image_topics(manifest)
    rosbag = read_rosbag_image_streams(resolve_rosbag_uri(demo_dir, manifest), required_topics, warnings)
    streams: dict[str, Stream] = {}
    for image_topic in required_topics:
        name = realsense_stream_name(image_topic)
        if image_topic in rosbag and len(rosbag[image_topic].header_time_ns) > 0:
            image_stream = rosbag[image_topic]
            streams[name] = Stream(
                name,
                f'RealSense {image_topic}',
                image_stream.header_time_ns,
                np.arange(len(image_stream.header_time_ns), dtype=np.int64),
                66_700_000,
                33_400_000,
                topic=image_topic,
                recorded_time_ns=image_stream.recorded_time_ns,
                timestamp_source='rosbag_image',
            ).sorted_valid()
            continue
        meta = metadata_by_topic.get(image_topic_to_metadata_topic(image_topic))
        if meta is not None:
            warnings.append(f'RealSense {image_topic} using metadata fallback; recorded timestamps unavailable')
            streams[name] = Stream(
                name,
                f'RealSense {image_topic}',
                meta['time_ns'],
                meta['source_index'],
                66_700_000,
                33_400_000,
                meta['frame_number'],
                image_topic,
                np.full(len(meta['time_ns']), -1, dtype=np.int64),
                'metadata_header',
            ).sorted_valid()
    if not streams:
        for topic, meta in metadata_by_topic.items():
            name = realsense_stream_name(topic)
            streams[name] = Stream(
                name,
                f'RealSense {topic}',
                meta['time_ns'],
                meta['source_index'],
                66_700_000,
                33_400_000,
                meta['frame_number'],
                topic,
                np.full(len(meta['time_ns']), -1, dtype=np.int64),
                'metadata_header',
            ).sorted_valid()
    return streams


def read_rosbag_image_streams(rosbag_uri: Path | None, topics: list[str], warnings: list[str]) -> dict[str, RosbagImageStream]:
    if rosbag_uri is None or not rosbag_uri.exists() or not topics:
        return {}
    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message
    except Exception as exc:
        warnings.append(f'rosbag image header read skipped: {exc}')
        return {}
    try:
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(rosbag_uri), storage_id=detect_storage_id(rosbag_uri)), rosbag2_py.ConverterOptions('', ''))
        topic_types = {topic.name: topic.type for topic in reader.get_all_topics_and_types()}
        selected = [topic for topic in topics if topic in topic_types]
        reader.set_filter(rosbag2_py.StorageFilter(topics=selected))
        classes = {topic: get_message(topic_types[topic]) for topic in selected}
        header_times: dict[str, list[int]] = {topic: [] for topic in selected}
        recorded_times: dict[str, list[int]] = {topic: [] for topic in selected}
        while reader.has_next():
            topic, serialized, _recorded = reader.read_next()
            header_times[topic].append(stamp_to_ns(deserialize_message(serialized, classes[topic]).header.stamp))
            recorded_times[topic].append(int(_recorded))
        return {
            topic: RosbagImageStream(
                np.asarray(header_times[topic], dtype=np.int64),
                np.asarray(recorded_times[topic], dtype=np.int64),
            )
            for topic in selected
        }
    except Exception as exc:
        warnings.append(f'rosbag image header read failed, using metadata fallback: {exc}')
        return {}


def build_target_timeline(
    base: str,
    streams: dict[str, Stream],
    demo_dir: Path,
    manifest: dict[str, Any],
    npz_paths: dict[str, Path],
    options: Options,
    warnings: list[str],
) -> TargetTimeline:
    if base == 'xense:pair':
        return build_xense_pair_timeline(streams, npz_paths, options)
    if base == 'realsense:bundle':
        return build_realsense_bundle_timeline(streams, manifest, options, warnings)
    base_stream = base_stream_for(base, streams)
    if base_stream is None:
        raise RuntimeError(f'base stream has no data: {base}')
    return TargetTimeline(
        target_times(base, base_stream, streams, options),
        resolved_base_kind(base),
        base_details={'kind': resolved_base_kind(base)},
    )


def build_xense_pair_timeline(
    streams: dict[str, Stream],
    npz_paths: dict[str, Path],
    options: Options,
) -> TargetTimeline:
    if 'xense' not in npz_paths:
        raise RuntimeError('xense:pair base requires xense timestamp npz')
    if 'xense_0' not in streams or 'xense_1' not in streams:
        raise RuntimeError('xense:pair base requires xense_0 and xense_1 streams')
    data = np.load(npz_paths['xense'], allow_pickle=True)
    ts0 = int_array(data['timestamp_ns_0'])
    ts1 = int_array(data['timestamp_ns_1'])
    if len(ts0) != len(ts1):
        raise RuntimeError(f'xense:pair requires equal timestamp array lengths, got {len(ts0)} and {len(ts1)}')
    valid_pair = (ts0 > 0) & (ts1 > 0)
    if not bool(valid_pair.all()):
        raise RuntimeError(f'xense:pair requires both sensor timestamps on every row; invalid rows: {int((~valid_pair).sum())}')
    source_index = np.arange(len(ts0), dtype=np.int64)
    pair_time = np.maximum(ts0, ts1).astype(np.int64, copy=False)
    order = np.argsort(pair_time, kind='stable')
    pair_time = pair_time[order]
    source_index = source_index[order]
    ts0 = ts0[order]
    ts1 = ts1[order]

    overlap_start, overlap_end = stream_overlap_bounds(streams, options)
    start_ns = max(pair_time[0], overlap_start) + int(round(options.start_trim_s * NSEC_PER_SEC))
    end_ns = min(pair_time[-1], overlap_end)
    keep = (pair_time >= start_ns) & (pair_time <= end_ns)
    pair_time = pair_time[keep]
    source_index = source_index[keep]
    ts0 = ts0[keep]
    ts1 = ts1[keep]
    if len(pair_time) == 0:
        return TargetTimeline(np.asarray([], dtype=np.int64), 'xense_pair')

    direct_matches = {
        'xense_0': direct_match_from_source_index(streams['xense_0'], source_index, pair_time, ts0),
        'xense_1': direct_match_from_source_index(streams['xense_1'], source_index, pair_time, ts1),
    }
    arrays = {
        'xense_pair_time_ns': pair_time,
        'xense_pair_source_index': source_index,
    }
    details = {
        'kind': 'xense_pair',
        'pair_count': int(len(pair_time)),
        'source_index_start': int(source_index[0]),
        'source_index_end': int(source_index[-1]),
    }
    return TargetTimeline(pair_time, 'xense_pair', direct_matches, arrays, details)


def direct_match_from_source_index(
    stream: Stream,
    source_index: np.ndarray,
    target_time_ns: np.ndarray,
    source_time_ns: np.ndarray,
) -> dict[str, np.ndarray]:
    source_to_position = {int(value): pos for pos, value in enumerate(stream.source_index)}
    position = np.asarray([source_to_position.get(int(value), -1) for value in source_index], dtype=np.int64)
    valid = position >= 0
    index = np.full(len(target_time_ns), -1, dtype=np.int64)
    time_ns = np.full(len(target_time_ns), -1, dtype=np.int64)
    index[valid] = source_index[valid]
    time_ns[valid] = source_time_ns[valid]
    return {
        'index': index,
        'position': position,
        'time_ns': time_ns,
        'delta_ns': time_ns - target_time_ns,
        'valid': valid,
    }


def build_realsense_bundle_timeline(
    streams: dict[str, Stream],
    manifest: dict[str, Any],
    options: Options,
    warnings: list[str],
) -> TargetTimeline:
    required_topics = required_image_topics(manifest)
    if not required_topics:
        raise RuntimeError('realsense:bundle requires manifest required image topics')
    missing_topics = []
    non_image_sources = []
    for topic in required_topics:
        stream = streams.get(realsense_stream_name(topic))
        if stream is None:
            missing_topics.append(topic)
        elif stream.timestamp_source != 'rosbag_image':
            non_image_sources.append(topic)
    if missing_topics:
        raise RuntimeError(f'realsense:bundle requires readable rosbag image topics; missing: {missing_topics}')
    if non_image_sources:
        raise RuntimeError(f'realsense:bundle requires image-topic header timestamps for every required topic; not rosbag image: {non_image_sources}')

    camera_topics = group_realsense_topics_by_camera(required_topics)
    representative_topics: dict[str, str] = {}
    representative_streams: dict[str, Stream] = {}
    for camera, topics in camera_topics.items():
        color_topics = [topic for topic in topics if '/color/image_raw' in topic]
        if not color_topics:
            raise RuntimeError(f'realsense:bundle requires a color image topic for camera {camera!r}')
        topic = sorted(color_topics)[0]
        representative_topics[camera] = topic
        representative_streams[camera] = streams[realsense_stream_name(topic)]

    overlap_start, overlap_end = stream_overlap_bounds(streams, options)
    start_ns = max(max(stream.time_ns[0] for stream in representative_streams.values()), overlap_start)
    start_ns += int(round(options.start_trim_s * NSEC_PER_SEC))
    end_ns = min(min(stream.time_ns[-1] for stream in representative_streams.values()), overlap_end)
    if end_ns < start_ns:
        return TargetTimeline(np.asarray([], dtype=np.int64), 'realsense_bundle')

    cameras = sorted(representative_streams)
    initial = choose_initial_bundle(cameras, representative_streams, start_ns)
    if initial is None:
        return TargetTimeline(np.asarray([], dtype=np.int64), 'realsense_bundle')

    selected_indices: list[dict[str, int]] = []
    modes: list[str] = []
    resync: list[bool] = []
    reused: list[bool] = []
    current = initial
    current_mode = 'initial_search'
    current_resync = False
    current_reused = False
    last_t = -1
    while True:
        bundle_time = max(int(representative_streams[camera].time_ns[current[camera]]) for camera in cameras)
        if bundle_time > end_ns:
            break
        if bundle_time > last_t:
            selected_indices.append(dict(current))
            modes.append(current_mode)
            resync.append(current_resync)
            reused.append(current_reused)
            last_t = bundle_time
        expected = {camera: current[camera] + 1 for camera in cameras}
        if any(expected[camera] >= len(representative_streams[camera].time_ns) for camera in cameras):
            break
        locked_span = bundle_span_ns(cameras, representative_streams, expected)
        if locked_span <= REALSENSE_BUNDLE_SPAN_WARN_NS:
            current = expected
            current_mode = 'locked_plus_one'
            current_resync = False
            current_reused = False
            continue
        fallback = choose_local_bundle(cameras, representative_streams, expected, last_t, end_ns)
        if fallback is None:
            current = expected
            current_mode = 'degraded_best_effort'
            current_resync = False
            current_reused = False
            if max(int(representative_streams[camera].time_ns[current[camera]]) for camera in cameras) <= last_t:
                break
            continue
        current = fallback
        current_mode = 'fallback_search'
        current_resync = True
        current_reused = any(current[camera] <= selected_indices[-1][camera] for camera in cameras)

    if not selected_indices:
        return TargetTimeline(np.asarray([], dtype=np.int64), 'realsense_bundle')

    bundle_time_ns = np.asarray(
        [max(int(representative_streams[camera].time_ns[indices[camera]]) for camera in cameras) for indices in selected_indices],
        dtype=np.int64,
    )
    bundle_span = np.asarray(
        [bundle_span_ns(cameras, representative_streams, indices) for indices in selected_indices],
        dtype=np.int64,
    )
    degraded = bundle_span > REALSENSE_BUNDLE_SPAN_WARN_NS
    quality = np.asarray(['degraded_span' if value else 'ok' for value in degraded], dtype='<U32')
    mode_array = np.asarray(modes, dtype='<U32')
    resync_array = np.asarray(resync, dtype=bool)
    reused_array = np.asarray(reused, dtype=bool)

    arrays: dict[str, np.ndarray] = {
        'realsense_bundle_time_ns': bundle_time_ns,
        'realsense_bundle_span_ns': bundle_span,
        'realsense_bundle_mode': mode_array,
        'realsense_bundle_quality': quality,
        'realsense_bundle_resync': resync_array,
        'realsense_bundle_degraded': degraded,
        'realsense_bundle_reused': reused_array,
    }
    direct_matches: dict[str, dict[str, np.ndarray]] = {}
    selected_recorded_times: list[np.ndarray] = []
    for camera in cameras:
        rep_stream = representative_streams[camera]
        rep_positions = np.asarray([indices[camera] for indices in selected_indices], dtype=np.int64)
        arrays[f'realsense_bundle_{safe_key(camera)}_index'] = rep_stream.source_index[rep_positions]
        arrays[f'realsense_bundle_{safe_key(camera)}_time_ns'] = rep_stream.time_ns[rep_positions]
        arrays[f'realsense_bundle_{safe_key(camera)}_recorded_time_ns'] = rep_stream.recorded_time_ns[rep_positions]
        rep_times = rep_stream.time_ns[rep_positions]
        for topic in camera_topics[camera]:
            stream = streams[realsense_stream_name(topic)]
            positions = nearest_positions_for_times(stream.time_ns, rep_times)
            deltas = np.abs(stream.time_ns[positions] - rep_times)
            mismatch_count = int(np.count_nonzero(deltas))
            if mismatch_count:
                warnings.append(
                    f'RealSense bundle camera {camera} topic {topic} has {mismatch_count} frame(s) '
                    'whose selected header stamp differs from the representative color stamp'
                )
            valid = positions >= 0
            direct_matches[stream.name] = {
                'index': stream.source_index[positions],
                'position': positions,
                'time_ns': stream.time_ns[positions],
                'delta_ns': stream.time_ns[positions] - bundle_time_ns,
                'valid': valid,
            }
            selected_recorded_times.append(stream.recorded_time_ns[positions])

    recorded_stack = np.vstack(selected_recorded_times)
    recorded_time_ns = recorded_stack.max(axis=0).astype(np.int64, copy=False)
    recorded_span_ns = (recorded_stack.max(axis=0) - recorded_stack.min(axis=0)).astype(np.int64, copy=False)
    arrays['realsense_bundle_recorded_time_ns'] = recorded_time_ns
    arrays['realsense_bundle_recorded_span_ns'] = recorded_span_ns

    details = {
        'kind': 'realsense_bundle',
        'required_topics': required_topics,
        'representative_topics': representative_topics,
        'bundle_count': int(len(bundle_time_ns)),
        'span_ns': numeric_summary(bundle_span),
        'recorded_span_ns': numeric_summary(recorded_span_ns),
        'mode_counts': value_counts(mode_array),
        'quality_counts': value_counts(quality),
        'resync_count': int(resync_array.sum()),
        'degraded_count': int(degraded.sum()),
        'reused_count': int(reused_array.sum()),
        'span_warn_ns': REALSENSE_BUNDLE_SPAN_WARN_NS,
    }
    return TargetTimeline(bundle_time_ns, 'realsense_bundle', direct_matches, arrays, details)


def target_times(base: str, base_stream: Stream, streams: dict[str, Stream], options: Options) -> np.ndarray:
    overlap_start = max(stream.time_ns[0] + int(round(options.stream_start_trim.get(stream.name, 0.0) * NSEC_PER_SEC)) for stream in streams.values())
    overlap_end = min(stream.time_ns[-1] for stream in streams.values())
    start_ns = max(base_stream.time_ns[0], overlap_start) + int(round(options.start_trim_s * NSEC_PER_SEC))
    end_ns = min(base_stream.time_ns[-1], overlap_end)
    if end_ns < start_ns:
        return np.asarray([], dtype=np.int64)
    if base == 'grid':
        return np.arange(start_ns, end_ns + 1, int(round(NSEC_PER_SEC / options.hz)), dtype=np.int64)
    return base_stream.time_ns[(base_stream.time_ns >= start_ns) & (base_stream.time_ns <= end_ns)]


def match_stream(t_ns: np.ndarray, stream: Stream, mode: str) -> dict[str, np.ndarray]:
    right = np.searchsorted(stream.time_ns, t_ns, side='right')
    if mode == 'causal':
        chosen = right - 1
    elif mode == 'nearest':
        left = np.maximum(right - 1, 0)
        next_ = np.minimum(right, len(stream.time_ns) - 1)
        chosen = np.where(np.abs(stream.time_ns[next_] - t_ns) < np.abs(stream.time_ns[left] - t_ns), next_, left)
    else:
        raise ValueError(f'unsupported mode: {mode}')
    valid_index = (chosen >= 0) & (chosen < len(stream.time_ns))
    matched_time = np.full(len(t_ns), -1, dtype=np.int64)
    matched_time[valid_index] = stream.time_ns[chosen[valid_index]]
    delta_ns = matched_time - t_ns
    tolerance = stream.tolerance_causal_ns if mode == 'causal' else stream.tolerance_nearest_ns
    valid = valid_index & (np.abs(delta_ns) <= tolerance)
    if mode == 'causal':
        valid &= delta_ns <= 0
    index = np.full(len(t_ns), -1, dtype=np.int64)
    index[valid_index] = stream.source_index[chosen[valid_index]]
    position = np.full(len(t_ns), -1, dtype=np.int64)
    position[valid_index] = chosen[valid_index]
    return {'index': index, 'position': position, 'time_ns': matched_time, 'delta_ns': delta_ns, 'valid': valid}


def stream_overlap_bounds(streams: dict[str, Stream], options: Options) -> tuple[int, int]:
    overlap_start = max(
        stream.time_ns[0] + int(round(options.stream_start_trim.get(stream.name, 0.0) * NSEC_PER_SEC))
        for stream in streams.values()
    )
    overlap_end = min(stream.time_ns[-1] for stream in streams.values())
    return int(overlap_start), int(overlap_end)


def group_realsense_topics_by_camera(topics: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for topic in topics:
        parts = [part for part in topic.split('/') if part]
        camera = parts[0] if parts else 'camera'
        result.setdefault(camera, []).append(topic)
    return result


def choose_initial_bundle(
    cameras: list[str],
    streams: dict[str, Stream],
    start_ns: int,
) -> dict[str, int] | None:
    candidate_lists: list[list[int]] = []
    for camera in cameras:
        times = streams[camera].time_ns
        center = int(np.searchsorted(times, start_ns, side='left'))
        candidates = bounded_index_window(len(times), center, REALSENSE_BUNDLE_INITIAL_SEARCH_RADIUS)
        candidate_lists.append(candidates)
    return choose_best_bundle(cameras, streams, candidate_lists, min_time_ns=start_ns, max_time_ns=None)


def choose_local_bundle(
    cameras: list[str],
    streams: dict[str, Stream],
    expected: dict[str, int],
    last_time_ns: int,
    end_ns: int,
) -> dict[str, int] | None:
    candidate_lists = [
        bounded_index_window(len(streams[camera].time_ns), expected[camera], REALSENSE_BUNDLE_FALLBACK_SEARCH_RADIUS)
        for camera in cameras
    ]
    return choose_best_bundle(cameras, streams, candidate_lists, min_time_ns=last_time_ns + 1, max_time_ns=end_ns)


def choose_best_bundle(
    cameras: list[str],
    streams: dict[str, Stream],
    candidate_lists: list[list[int]],
    min_time_ns: int,
    max_time_ns: int | None,
) -> dict[str, int] | None:
    best: tuple[int, int, dict[str, int]] | None = None
    for combo in product(*candidate_lists):
        indices = dict(zip(cameras, combo))
        times = [int(streams[camera].time_ns[indices[camera]]) for camera in cameras]
        bundle_time = max(times)
        if bundle_time < min_time_ns:
            continue
        if max_time_ns is not None and bundle_time > max_time_ns:
            continue
        span = max(times) - min(times)
        score = (span, bundle_time)
        if best is None or score < (best[0], best[1]):
            best = (span, bundle_time, indices)
    return None if best is None else best[2]


def bounded_index_window(length: int, center: int, radius: int) -> list[int]:
    start = max(0, center - radius)
    stop = min(length - 1, center + radius)
    if stop < start:
        return []
    return list(range(start, stop + 1))


def bundle_span_ns(cameras: list[str], streams: dict[str, Stream], indices: dict[str, int]) -> int:
    times = [int(streams[camera].time_ns[indices[camera]]) for camera in cameras]
    return max(times) - min(times)


def nearest_positions_for_times(stream_time_ns: np.ndarray, target_time_ns: np.ndarray) -> np.ndarray:
    right = np.searchsorted(stream_time_ns, target_time_ns, side='left')
    left = np.maximum(right - 1, 0)
    right = np.minimum(right, len(stream_time_ns) - 1)
    return np.where(np.abs(stream_time_ns[right] - target_time_ns) < np.abs(stream_time_ns[left] - target_time_ns), right, left).astype(np.int64)


def numeric_summary(values: np.ndarray) -> dict[str, Any]:
    if len(values) == 0:
        return {'count': 0, 'min': None, 'median': None, 'p95': None, 'max': None}
    return {
        'count': int(len(values)),
        'min': int(np.min(values)),
        'median': float(np.median(values)),
        'p95': float(np.percentile(values, 95)),
        'max': int(np.max(values)),
    }


def value_counts(values: np.ndarray) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        key = str(value)
        result[key] = result.get(key, 0) + 1
    return result


def resolved_base_kind(base: str) -> str:
    if base == 'grid':
        return 'grid'
    if base == 'robot':
        return 'robot'
    if base == 'xense:pair':
        return 'xense_pair'
    if base.startswith('xense:'):
        return 'xense_single'
    if base == 'realsense:bundle':
        return 'realsense_bundle'
    if base.startswith('realsense:'):
        return 'realsense_single'
    return 'stream'


def resolve_base(options: Options, manifest: dict[str, Any], streams: dict[str, Stream]) -> str:
    if options.base != 'auto':
        return options.base
    if options.alignment_base_source == 'xense':
        return 'xense:pair'
    for topic in required_image_topics(manifest):
        if '/color/' in topic and realsense_stream_name(topic) in streams:
            return f'realsense:{topic}'
    for name, stream in streams.items():
        if name.startswith('realsense_'):
            return f'realsense:{stream.topic or name}'
    return 'xense:1' if 'xense_1' in streams else next(iter(streams))


def base_stream_for(base: str, streams: dict[str, Stream]) -> Stream | None:
    if base == 'grid':
        return next(iter(streams.values()))
    if base == 'robot':
        return streams.get('zmq_source_2')
    if base == 'xense:0':
        return streams.get('xense_0')
    if base == 'xense:1':
        return streams.get('xense_1')
    if base == 'xense:pair':
        return None
    if base.startswith('realsense:'):
        target = base.split(':', 1)[1]
        if target == 'auto':
            return next((stream for name, stream in streams.items() if name.startswith('realsense_')), None)
        if target == 'bundle':
            return None
        return streams.get(realsense_stream_name(target))
    return streams.get(base)


def stream_stats(stream: Stream, match: dict[str, np.ndarray]) -> dict[str, Any]:
    valid = match['valid']
    abs_delta = np.abs(match['delta_ns'][valid])
    return {
        'display_name': stream.display_name,
        'frame_count': int(len(stream.time_ns)),
        'used_count': int(valid.sum()),
        'invalid_count': int(len(valid) - valid.sum()),
        'max_abs_delta_ns': None if len(abs_delta) == 0 else int(abs_delta.max()),
        'mean_abs_delta_ns': None if len(abs_delta) == 0 else float(abs_delta.mean()),
        'median_abs_delta_ns': None if len(abs_delta) == 0 else float(np.median(abs_delta)),
    }


def clock_domain_summary(npz_path: Path | None, warnings: list[str]) -> dict[str, Any]:
    if npz_path is None:
        return {}
    data = np.load(npz_path, allow_pickle=True)
    domains = np.asarray(data['clock_domain']).astype(str)
    counts: dict[str, int] = {}
    missing = 0
    for value in domains:
        key = value if value and value != 'None' else '<missing>'
        counts[key] = counts.get(key, 0) + 1
        if key == '<missing>':
            missing += 1
    if missing:
        warnings.append(f'RealSense metadata clock_domain missing on {missing} frame(s)')
    return {'counts': counts, 'missing_count': missing}


def render_report(manifest: dict[str, Any]) -> str:
    lines = [
        '# Alignment Report',
        '',
        f"Status: {manifest['status']}",
        f"Base: {manifest['base']}",
        f"Resolved base kind: {manifest.get('resolved_base_kind', 'stream')}",
        f"Samples: {manifest['valid_count']} / {manifest['sample_count']} valid",
        '',
        '## Streams',
    ]
    for name, stats in manifest['streams'].items():
        median = stats.get('median_abs_delta_ns')
        median_ms = 'n/a' if median is None else f'{median / 1e6:.3f}'
        lines.append(f"- {stats['display_name']} (`{name}`): used {stats['used_count']}/{stats['frame_count']}, median abs delta {median_ms} ms")
    if manifest.get('zmq_clock_offsets'):
        lines.extend(['', '## ZMQ Clock Offsets'])
        for name, offset in sorted(manifest['zmq_clock_offsets'].items()):
            lines.append(
                f"- ZMQ source {offset['source']} (`{name}`): "
                f"offset {offset['offset_ms']:.3f} ms over {offset['frame_count']} frame(s)"
            )
    if manifest.get('clock_domain'):
        lines.extend(['', '## RealSense Clock Domain', json.dumps(manifest['clock_domain'].get('counts', {}), ensure_ascii=True)])
    base_details = manifest.get('base_details') or {}
    if base_details.get('kind') == 'realsense_bundle':
        span = base_details.get('span_ns') or {}
        recorded_span = base_details.get('recorded_span_ns') or {}
        lines.extend(
            [
                '',
                '## RealSense Bundle',
                f"Bundles: {base_details.get('bundle_count', 0)}",
                (
                    'Header span ns: '
                    f"median={span.get('median')}, p95={span.get('p95')}, max={span.get('max')}"
                ),
                (
                    'Recorded span ns: '
                    f"median={recorded_span.get('median')}, p95={recorded_span.get('p95')}, max={recorded_span.get('max')}"
                ),
                f"Modes: {json.dumps(base_details.get('mode_counts', {}), ensure_ascii=True)}",
                f"Quality: {json.dumps(base_details.get('quality_counts', {}), ensure_ascii=True)}",
                (
                    f"Resync/degraded/reused: {base_details.get('resync_count', 0)} / "
                    f"{base_details.get('degraded_count', 0)} / {base_details.get('reused_count', 0)}"
                ),
            ]
        )
    elif base_details.get('kind') == 'xense_pair':
        lines.extend(['', '## Xense Pair Base', f"Pairs: {base_details.get('pair_count', 0)}"])
    if manifest.get('warnings'):
        lines.extend(['', '## Warnings'])
        lines.extend(f"- {warning}" for warning in manifest['warnings'])
    return '\n'.join(lines) + '\n'


def parse_stream_start_trim(values: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for value in values:
        name, seconds = value.split('=', 1)
        result[name] = float(seconds)
    return result


def resolve_npz_paths(demo_dir: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for key, value in (manifest.get('npz') or {}).items():
        path = Path(value)
        if not path.is_absolute():
            path = demo_dir / path
        if path.exists():
            result[key] = path
    return result


def source_paths(manifest: dict[str, Any]) -> dict[str, Any]:
    sensor_paths = manifest.get('sensor_paths') or {}
    return {
        'npz': dict(manifest.get('npz') or {}),
        'ft300s_saved_file': sensor_paths.get('ft300'),
        'xense_saved_file': sensor_paths.get('xense'),
        'rosbag_uri': manifest.get('rosbag_uri'),
    }


def resolve_rosbag_uri(demo_dir: Path, manifest: dict[str, Any]) -> Path | None:
    value = manifest.get('rosbag_uri')
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else demo_dir / path


def relative_to(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()


def required_image_topics(manifest: dict[str, Any]) -> list[str]:
    postcheck = manifest.get('realsense_rosbag_postcheck') or {}
    readiness = manifest.get('realsense_image_readiness') or {}
    return [str(topic) for topic in (postcheck.get('required_topics') or readiness.get('required_topics') or [])]


def image_topic_to_metadata_topic(topic: str) -> str:
    if '/color/' in topic:
        return topic.replace('/color/image_raw', '/color/metadata')
    return re.sub(r'/aligned_depth_to_color/image_raw$', '/depth/metadata', topic)


def realsense_stream_name(topic: str) -> str:
    parts = [part for part in topic.split('/') if part]
    camera = parts[0] if parts else 'camera'
    if 'color' in parts:
        role = 'color'
    elif 'aligned_depth_to_color' in parts:
        role = 'aligned_depth'
    elif 'depth' in parts:
        role = 'depth'
    else:
        role = 'stream'
    return f'realsense_{safe_key(camera)}_{role}'


def safe_key(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9_]+', '_', value).strip('_').lower()


def int_array(values: Any) -> np.ndarray:
    result = []
    for value in values:
        try:
            if value is None or (isinstance(value, float) and math.isnan(value)):
                result.append(-1)
            else:
                result.append(int(value))
        except Exception:
            result.append(-1)
    return np.asarray(result, dtype=np.int64)


def stamp_to_ns(stamp: Any) -> int:
    return int(stamp.sec) * NSEC_PER_SEC + int(stamp.nanosec)


def detect_storage_id(bag_dir: Path) -> str:
    metadata_file = bag_dir / 'metadata.yaml'
    if metadata_file.exists():
        match = re.search(r'storage_identifier:\s*([A-Za-z0-9_\-]+)', metadata_file.read_text(encoding='utf-8', errors='ignore'))
        if match:
            return match.group(1)
    if list(bag_dir.glob('*.mcap')):
        return 'mcap'
    return 'sqlite3'


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Align timestamps for one MainController demo')
    parser.add_argument('--demo-dir', required=True)
    parser.add_argument('--output-dir', default=None)
    parser.add_argument('--repo-root', default=None)
    parser.add_argument('--alignment-base-source', choices=['realsense', 'xense'], default='realsense')
    parser.add_argument(
        '--base',
        default='auto',
        help='auto, realsense:<topic>, realsense:bundle, xense:pair, xense:0, xense:1, robot, or grid',
    )
    parser.add_argument('--mode', choices=['causal', 'nearest'], default='causal')
    parser.add_argument('--hz', type=float, default=30.0)
    parser.add_argument('--start-trim-s', type=float, default=0.0)
    parser.add_argument('--stream-start-trim', action='append', default=[], metavar='STREAM=SECONDS')
    parser.add_argument('--allow-degraded', action='store_true')
    args = parser.parse_args()

    options = Options(
        repo_root=(
            REPO_ROOT
            if args.repo_root is None
            else Path(args.repo_root).expanduser().resolve()
        ),
        output_dir=None if args.output_dir is None else Path(args.output_dir),
        base=args.base,
        alignment_base_source=args.alignment_base_source,
        mode=args.mode,
        hz=args.hz,
        start_trim_s=args.start_trim_s,
        stream_start_trim=parse_stream_start_trim(args.stream_start_trim),
        allow_degraded=args.allow_degraded,
    )
    result = align_demo(Path(args.demo_dir), options)
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == '__main__':
    main()

# NOTE: This standalone tool now supports realsense:bundle and xense:pair bases.
# MainController timestamp_alignment.py has not been updated for these standalone
# RealSense bundle-base and Xense pair/max-base behaviors.
