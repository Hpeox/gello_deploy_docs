#!/usr/bin/env python3
"""Compare RealSense image and metadata timestamps from a recorded rosbag2.

This tool is intended for an offline 20s capture test. It reads two topics from a
rosbag2 directory, pairs messages in recording order by default, and reports
whether the metadata timestamp can be used to align the depth stream offline.

Typical workflow:

  1. Record ~20 seconds of data.
  2. Run this script on the resulting bag directory.

Example:

  python3 tools/realsense_bag_compare.py \
      --bag ./tmp/realsense_20s \
      --image-topic /cam3/camera/aligned_depth_to_color/image_raw \
      --metadata-topic /cam3/camera/depth/metadata

Output includes:
  - total paired frames
  - exact-match and 33ms-bin statistics
  - maximum / mean / median absolute timestamp delta
  - a short verdict about whether metadata timestamps look safe for offline alignment
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import re

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message


NSEC_PER_SEC = 1_000_000_000


@dataclass
class Sample:
    idx: int
    image_stamp_ns: int
    metadata_stamp_ns: int
    delta_ns: int
    frame_number: Optional[int]


def stamp_to_ns(stamp: Any) -> int:
    return int(stamp.sec) * NSEC_PER_SEC + int(stamp.nanosec)


def detect_storage_id(bag_dir: Path) -> str:
    metadata_file = bag_dir / 'metadata.yaml'
    if metadata_file.exists():
        try:
            content = metadata_file.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            content = ''
        match = re.search(r"storage_identifier:\s*([A-Za-z0-9_\-]+)", content)
        if match:
            return match.group(1)

    mcap_files = list(bag_dir.glob('*.mcap'))
    if mcap_files:
        return 'mcap'

    return 'sqlite3'


def read_topic_messages(bag_dir: Path, topic_name: str) -> List[Any]:
    reader = rosbag2_py.SequentialReader()
    storage_id = detect_storage_id(bag_dir)
    storage_options = rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id=storage_id)
    converter_options = rosbag2_py.ConverterOptions('', '')
    reader.open(storage_options, converter_options)
    reader.set_filter(rosbag2_py.StorageFilter(topics=[topic_name]))

    topic_types = {topic.name: topic.type for topic in reader.get_all_topics_and_types()}
    if topic_name not in topic_types:
        raise RuntimeError(f'Topic not found in bag: {topic_name}')

    msg_cls = get_message(topic_types[topic_name])
    messages: List[Any] = []
    while reader.has_next():
        _topic, serialized, _t = reader.read_next()
        messages.append(deserialize_message(serialized, msg_cls))
    return messages


def extract_metadata_frame_number(msg: Any) -> Optional[int]:
    try:
        data = json.loads(msg.json_data)
    except Exception:
        return None
    value = data.get('frame_number')
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def summarize(samples: List[Sample], threshold_ms: float) -> None:
    if not samples:
        print('No paired samples found.')
        return

    deltas_ms = [sample.delta_ns / 1e6 for sample in samples]
    abs_deltas_ms = [abs(value) for value in deltas_ms]
    buckets = Counter(int(round(abs(value) / threshold_ms)) for value in abs_deltas_ms)
    exact_zero = sum(1 for value in abs_deltas_ms if value < 1e-3)
    within_threshold = sum(1 for value in abs_deltas_ms if value <= threshold_ms)
    around_one_frame = sum(1 for value in abs_deltas_ms if abs(value - 33.333) <= 4.0)
    around_two_frames = sum(1 for value in abs_deltas_ms if abs(value - 66.666) <= 4.0)

    mean_abs = statistics.fmean(abs_deltas_ms)
    median_abs = statistics.median(abs_deltas_ms)
    max_abs = max(abs_deltas_ms)

    print(f'Paired frames: {len(samples)}')
    print(f'Exact zero deltas: {exact_zero}')
    print(f'Within {threshold_ms:.1f} ms: {within_threshold} / {len(samples)}')
    print(f'Around 33 ms: {around_one_frame}')
    print(f'Around 66 ms: {around_two_frames}')
    print(f'Mean abs delta: {mean_abs:.3f} ms')
    print(f'Median abs delta: {median_abs:.3f} ms')
    print(f'Max abs delta: {max_abs:.3f} ms')

    print('Bucketed abs delta counts (bucket = round(abs(delta_ms) / threshold_ms)):')
    for bucket, count in sorted(buckets.items()):
        print(f'  {bucket}: {count}')

    print('')
    print('First 12 paired samples:')
    print('idx,image_stamp_ns,metadata_stamp_ns,delta_ns,delta_ms,frame_number')
    for sample in samples[:12]:
        print(f'{sample.idx},{sample.image_stamp_ns},{sample.metadata_stamp_ns},{sample.delta_ns},{sample.delta_ns / 1e6:.3f},{sample.frame_number}')

    if within_threshold >= int(len(samples) * 0.95) and max_abs <= threshold_ms * 1.5:
        print('Verdict: metadata timestamps look safe enough for offline alignment at this threshold.')
    elif around_one_frame >= int(len(samples) * 0.5):
        print('Verdict: many samples are offset by about one frame period; metadata is not safe to use directly without pairing by frame_number or a better sync key.')
    else:
        print('Verdict: mixed results; inspect frame_number continuity and consider pairing by frame_number before trusting metadata timestamps.')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--bag', required=True, help='rosbag2 directory')
    parser.add_argument('--image-topic', required=True, help='image topic name')
    parser.add_argument('--metadata-topic', required=True, help='metadata topic name')
    parser.add_argument('--pairing', choices=['order'], default='order', help='pairing strategy (currently order only)')
    parser.add_argument('--threshold-ms', type=float, default=5.0, help='acceptable timestamp difference in ms')
    args = parser.parse_args()

    bag_dir = Path(args.bag)
    if not bag_dir.exists():
        raise SystemExit(f'Bag directory does not exist: {bag_dir}')
    storage_id = detect_storage_id(bag_dir)
    print(f'Using storage backend: {storage_id}')

    image_msgs = read_topic_messages(bag_dir, args.image_topic)
    metadata_msgs = read_topic_messages(bag_dir, args.metadata_topic)

    if not image_msgs:
        raise SystemExit(f'No messages found in image topic: {args.image_topic}')
    if not metadata_msgs:
        raise SystemExit(f'No messages found in metadata topic: {args.metadata_topic}')

    pairs = min(len(image_msgs), len(metadata_msgs))
    samples: List[Sample] = []
    for idx in range(pairs):
        image_msg = image_msgs[idx]
        metadata_msg = metadata_msgs[idx]
        image_stamp_ns = stamp_to_ns(image_msg.header.stamp)
        metadata_stamp_ns = stamp_to_ns(metadata_msg.header.stamp)
        delta_ns = image_stamp_ns - metadata_stamp_ns
        samples.append(Sample(
            idx=idx + 1,
            image_stamp_ns=image_stamp_ns,
            metadata_stamp_ns=metadata_stamp_ns,
            delta_ns=delta_ns,
            frame_number=extract_metadata_frame_number(metadata_msg),
        ))

    print(f'Image messages: {len(image_msgs)}')
    print(f'Metadata messages: {len(metadata_msgs)}')
    print(f'Paired by order: {pairs}')
    summarize(samples, args.threshold_ms)


if __name__ == '__main__':
    main()
