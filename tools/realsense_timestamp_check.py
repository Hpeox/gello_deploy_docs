#!/usr/bin/env python3
"""
简易 ROS2 节点：比较 RealSense image topic 的 `header.stamp` 与 depth metadata 的时间字段。

用法示例：
  python3 tools/realsense_timestamp_check.py --image /camera/camera/depth/image_rect_raw \
      --metadata /camera/camera/depth/metadata --match-ms 5

默认 topic 对应 realsense wrapper 的常见命名。脚本会打印 CSV 行：
  frame_count,image_header_ns,meta_header_ns,meta_frame_timestamp,meta_hw_timestamp,delta_header_ns,delta_frame_ns,delta_hw_ns

注意：需要在有 ROS2 环境并且 `realsense2_camera_msgs` 可用的情况下运行。
"""
import rclpy
from rclpy.node import Node
import argparse
import json
from sensor_msgs.msg import Image
from collections import deque
try:
    from realsense2_camera_msgs.msg import Metadata
except Exception:
    Metadata = None


class TimestampChecker(Node):
    def __init__(self, image_topic, metadata_topic, match_ms=5, use_aligned=False):
        super().__init__('realsense_ts_check')
        self.image_topic = image_topic
        self.metadata_topic = metadata_topic
        self.match_ns = int(match_ms * 1e6)
        # keep a short history of metadata messages to allow matching by frame_number/frame_timestamp
        self.meta_history = deque(maxlen=200)
        self.frame_count = 0
        self.mismatch_count = 0

        self.create_subscription(Image, self.image_topic, self.image_cb, 10)
        if Metadata is None:
            self.get_logger().warn('realsense2_camera_msgs.Metadata not importable; subscribe will attempt by topic type at runtime')
        else:
            self.create_subscription(Metadata, self.metadata_topic, self.meta_cb, 10)

        # print CSV header
        print('frame_count,image_header_ns,meta_header_ns,meta_frame_timestamp_ns,meta_hw_timestamp_ns,delta_header_ns,delta_frame_ns,delta_hw_ns,matched_frame_number')

    def meta_cb(self, msg):
        # metadata contains json_data string and header
        try:
            data = json.loads(msg.json_data)
        except Exception:
            data = {}
        frame_ts_ns = None
        hw_ts_ns = None
        frame_number = None
        # In realsense wrapper JSON, frame_timestamp is in milliseconds
        if 'frame_timestamp' in data:
            try:
                frame_ts_ns = int(round(float(data['frame_timestamp']) * 1e6))
            except Exception:
                frame_ts_ns = None
        if 'hw_timestamp' in data:
            try:
                hw_ts_ns = int(round(float(data['hw_timestamp']) * 1e6))
            except Exception:
                hw_ts_ns = None
        if 'frame_number' in data:
            try:
                frame_number = int(data['frame_number'])
            except Exception:
                frame_number = None
        entry = {'header': msg.header, 'frame_timestamp_ns': frame_ts_ns, 'hw_timestamp_ns': hw_ts_ns, 'frame_number': frame_number}
        # append to history
        self.meta_history.append(entry)

    def image_cb(self, msg):
        self.frame_count += 1
        img_ts = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        meta_h_ns = ''
        meta_frame_ns = ''
        meta_hw_ns = ''
        delta_header = ''
        delta_frame = ''
        delta_hw = ''
        matched_frame_number = ''

        # find nearest metadata entry by frame_timestamp or header stamp
        best = None
        best_diff = None
        for meta in list(self.meta_history):
            # prefer frame_timestamp if available
            if meta['frame_timestamp_ns'] is not None:
                diff = abs(img_ts - meta['frame_timestamp_ns'])
            else:
                mh = meta['header'].stamp.sec * 1_000_000_000 + meta['header'].stamp.nanosec
                diff = abs(img_ts - mh)
            if best is None or diff < best_diff:
                best = meta
                best_diff = diff

        if best is not None:
            meta_h = best['header'].stamp.sec * 1_000_000_000 + best['header'].stamp.nanosec
            meta_h_ns = str(meta_h)
            if best['frame_timestamp_ns'] is not None:
                meta_frame_ns = str(best['frame_timestamp_ns'])
            if best['hw_timestamp_ns'] is not None:
                meta_hw_ns = str(best['hw_timestamp_ns'])
            delta_header = str(img_ts - meta_h)
            if best['frame_timestamp_ns'] is not None:
                delta_frame = str(img_ts - best['frame_timestamp_ns'])
            if best['hw_timestamp_ns'] is not None:
                delta_hw = str(img_ts - best['hw_timestamp_ns'])
            if best['frame_number'] is not None:
                matched_frame_number = str(best['frame_number'])

            # basic matching check against header timestamp
            try:
                if abs(img_ts - meta_h) > self.match_ns:
                    self.mismatch_count += 1
            except Exception:
                pass

        print(f"{self.frame_count},{img_ts},{meta_h_ns},{meta_frame_ns},{meta_hw_ns},{delta_header},{delta_frame},{delta_hw},{matched_frame_number}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', '-i', default='/camera/camera/depth/image_rect_raw')
    parser.add_argument('--metadata', '-m', default='/camera/camera/depth/metadata')
    parser.add_argument('--match-ms', type=float, default=5.0, help='header match tolerance in ms')
    args = parser.parse_args()

    rclpy.init()
    node = TimestampChecker(args.image, args.metadata, match_ms=args.match_ms)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info(f'Processed {node.frame_count} frames, mismatches: {node.mismatch_count}')
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
