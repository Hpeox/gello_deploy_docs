# 时间戳对齐具体方案

## 目标和边界

本文对照 `plan.md`、`implement_plan.md` 和当前代码，给出第一版离线时间戳对齐方案。当前 MainController 已完成原始时间戳采集、实时丢帧监控、`.npz` 索引和 `manifest.json` 保存；对齐应作为独立后处理工具实现，不放进采集实时路径。

第一版目标：

- 输入一个已完成 demo 目录，例如 `runtime_sessions/session_*/demos/demo_*/`。
- 读取 `manifest.json`、主控保存的 `.npz`、FT300S/XenseTacSensor 自己落盘的 `.npy`、RealSense rosbag。
- 统一转换为 `int64` Unix epoch nanoseconds。
- 生成可训练/可分析的对齐索引和对齐数据，保留每个来源的原始时间戳、匹配索引、时间差和有效性 mask。

第一版不做：

- 不在采集过程中插值或阻塞保存。
- 不强行重采样 RealSense 图像内容到 `.npz`；图像仍通过 rosbag topic、frame index、frame_number 引用。
- 不把不同机器的远端 ZMQ 时间戳当成天然可信；必须做时钟偏移检查或修正。

## 当前实现中的可用时间戳

### MainController manifest

位置：`runtime_sessions/session_*/demos/demo_*/manifest.json`

关键字段：

- `started_ns`：主控创建 demo 时的 `time.time_ns()`，早于传感器 ACK 和 rosbag resume，只能作为粗边界。
- `finished_ns`：主控保存 manifest 时的 `time.time_ns()`，晚于传感器 flush 和 rosbag stop，只能作为粗边界。
- `rosbag_uri`：当前 demo 的 rosbag 目录。
- `sensor_saved_files.ft300`、`sensor_saved_files.xense`：传感器服务 ACK 返回的 `.npy` 文件名。
- `npz.ft300`、`npz.xense`、`npz.realsense`、`npz.zmq`：主控保存的时间戳索引。
- `drop_monitors`：各来源丢帧和最大间隔统计。
- `realsense_restart_events`：RealSense 重启时间点。

### FT300S

主控索引：`ft300_timestamps.npz`

- `frame_id`
- `timestamp_ns`：FT300S 进程读帧前后的 `time.time_ns()`，当前实现中在 `SensorClient.read_frame()` 开头生成。
- `recv_time_ns`：主控 UDS 收到 `FRAME_READY` 时的 `time.time_ns()`。
- `recv_monotonic_ns`：主控 UDS 收到消息时的 `time.monotonic_ns()`。

完整数据：`FT300S/runtime_frames/<saved_file>`

- 文件名来自 `manifest.sensor_saved_files.ft300`。
- 数据结构是 `np.load(..., allow_pickle=True).item()`，包含 `events` 和 `frames_data`。
- `frames_data["00000"]["ft300_timestamp_ns"]` 应与主控索引中的 `timestamp_ns` 按 `frame_id` 对应。
- wrench/力矩等完整值在同一个 per-frame dict 中。

推荐主时间戳：`ft300_timestamp_ns` 或主控索引 `timestamp_ns`。两者按 `frame_id` 交叉校验，差值异常时用主控索引告警但不静默覆盖。

### XenseTacSensor

主控索引：`xense_timestamps.npz`

- `frame_id`
- `timestamp_ns_0`：读取 sensor 0 前的 `time.time_ns()`。
- `timestamp_ns_1`：读取 sensor 1 前的 `time.time_ns()`。
- `recv_time_ns`
- `recv_monotonic_ns`

完整数据：`XenseTacSensor/runtime_frames/<saved_file>`

- 文件名来自 `manifest.sensor_saved_files.xense`。
- 数据结构是 `np.load(..., allow_pickle=True).item()`，包含 `events` 和 `frames_data`。
- `frames_data["00000"]["OG000544_timestamp_ns"]`、`frames_data["00000"]["OG001009_timestamp_ns"]` 分别对应两个触觉传感器。

推荐主时间戳：

- 对单个传感器张量，使用自己的 `timestamp_ns_0` 或 `timestamp_ns_1`。
- 对一帧双传感器组合特征，默认使用 `max(timestamp_ns_0, timestamp_ns_1)`，表示这一组合在两个读数都完成后才可被因果使用。
- 分析模式可额外保存 `mid_timestamp_ns = round((timestamp_ns_0 + timestamp_ns_1) / 2)`，但不要替代因果时间戳。

### RealSense

主控索引：`realsense_metadata.npz`

- `topic`
- `frame_number`
- `header_stamp_ns`：metadata ROS header stamp。
- `frame_timestamp_ns`：metadata JSON 中 `frame_timestamp` 从毫秒转纳秒。
- `hw_timestamp_ns`：metadata JSON 中 `hw_timestamp` 从毫秒转纳秒。
- `recv_time_ns`
- `recv_monotonic_ns`

rosbag：`manifest.rosbag_uri`

- 当前 rosbag launch 记录 image topic，不记录 metadata topic。
- 需要用 image topic 的 `header.stamp` 作为图像时间戳，并用 `tools/realsense_bag_compare.py` 或新工具验证 image header 与 metadata header 的对应关系。

推荐主时间戳：

- 用于跨源对齐时，默认使用 `header_stamp_ns` 或 rosbag image `header.stamp`。
- `frame_timestamp_ns` 和 `hw_timestamp_ns` 更像相机/设备内部时钟，不要直接和 `time.time_ns()`、ZMQ Unix stamp 混用。
- 若后续确认某型号 RealSense 的 `frame_timestamp` 与 ROS header 存在线性关系，可按 topic 拟合 `header_stamp_ns = a * frame_timestamp_ns + b`，并把拟合参数写入对齐报告。

注意：当前 `four_realsense_640x480_30.launch.py` 只启用了 `cam3`，但 `RuntimeConfig.cameras` 默认订阅 `cam1` 到 `cam4` 的 metadata。对齐工具必须按实际 `.npz` 和 rosbag topic 自动发现可用相机，不要假设四路都有数据。

### ZMQ telemetry

主控索引：`zmq_telemetry.npz`

- `source`：`1=gello`、`2=robot`、`3=gripper`、代码还兼容 `4=spacemouse`。
- `seq`
- `stamp_s`：ZMQ producer 写入的 Unix time seconds。
- `valid_mask`
- `floats_58`
- `gripper_gPO`
- `gripper_gCU`
- `recv_time_ns`
- `recv_monotonic_ns`

推荐主时间戳：

- 如果 ZMQ producer 和 MainController 在同一台机器，或已做 NTP/PTP 同步，使用 `round(stamp_s * 1e9)`。
- 如果 producer 在远端机器，先按 source 估计偏移：
  - `raw_stamp_ns = round(stamp_s * 1e9)`
  - `offset_ns = median(recv_time_ns - raw_stamp_ns)`，建议用去掉最大/最小 5% 的稳健中位数。
  - `aligned_stamp_ns = raw_stamp_ns + offset_ns`
- 如果 `recv_time_ns - raw_stamp_ns` 抖动超过 20 ms 或有明显漂移，标记该 source 为 `clock_unreliable`，优先用 `recv_time_ns` 做保守对齐，并在报告里提示需要主机时钟同步。

## 统一时间轴选择

第一版实现应支持三种 `--base`：

1. `--base realsense:<topic>`
   - 推荐给视觉模仿学习数据集。
   - 目标时间轴直接使用指定 RealSense metadata 或 rosbag image 的帧时间戳。
   - 图像不可插值，因此以相机帧为样本基准最稳。

2. `--base robot`
   - 推荐给控制/状态闭环分析。
   - 使用 ZMQ `source=2` robot telemetry 的时间戳作为目标时间轴。
   - 适合 50 Hz 左右的 robot state/action 数据。

3. `--base grid --hz <rate>`
   - 推荐给对齐质量检查或固定频率训练样本。
   - `start_ns = max(first_valid_time of required streams)`
   - `end_ns = min(last_valid_time of required streams)`
   - 生成 `[start_ns, end_ns]` 内的理想等间隔网格。

默认建议：

- 有 RealSense 图像参与训练时，用 `--base realsense:/cam3/camera/color/metadata`，或自动选择第一条有数据的 color metadata topic。
- 只做力/触觉/机器人状态对齐时，用 `--base robot`。
- 多相机时，每个相机都保留自己的 matched image index 和 `delta_ns`，不要强行假设完全同帧。

## 对齐策略

每个目标时间 `t` 输出各来源的 matched index、source timestamp、`delta_ns = source_time_ns - t` 和 valid flag。

### 因果模式

命令建议：`--mode causal`

用于训练在线策略，默认规则：

- 所有观测只能使用 `source_time_ns <= t` 的最近一帧。
- 连续数值流可使用前向保持或只基于过去样本的插值；不要使用未来帧。
- action/label 可以显式指定 horizon，例如 `--action-horizon-ms 20`，从 `t + horizon` 附近取 robot/GELLO 指令。

推荐容忍窗口：

- FT300S：过去 20 ms 内有效。
- XenseTacSensor：过去 66.7 ms 内有效。
- ZMQ robot/GELLO/gripper：过去 40 ms 内有效。
- RealSense：过去 66.7 ms 内有效。

若超过窗口：

- 设置该来源 `valid=false`。
- 该样本如果缺少 required stream，则整行丢弃或写入 `sample_valid=false`，由参数决定。

### 分析模式

命令建议：`--mode nearest`

用于离线分析和可视化，默认规则：

- 不强调因果，选择 `abs(source_time_ns - t)` 最小的帧。
- 对连续数值流做线性插值。
- 对不可插值数据选择最近帧。

推荐容忍窗口：

- FT300S：10 ms。
- XenseTacSensor：33.3 ms。
- ZMQ robot/GELLO/gripper：20 ms。
- RealSense：33.3 ms。

### 各来源数据处理

FT300S：

- `wrench`、`fx/fy/fz/tx/ty/tz` 可线性插值。
- 因果模式默认用最近过去帧，必要时可对过去两帧线性外推，但第一版不建议开启外推。

XenseTacSensor：

- `force`、`force_norm`、`force_resultant` 可按需求线性插值。
- `rec` 这类触觉图/矩阵默认不可插值，使用最近过去帧或最近帧。
- 双传感器组合时用 `max(timestamp_ns_0, timestamp_ns_1)` 做 causality gate，同时保留两个原始 timestamp 和两个 `delta_ns`。

RealSense：

- image 不插值。
- color/depth/aligned depth 使用 rosbag image `header.stamp` 选择帧。
- metadata 的 `frame_number` 作为 continuity 和 image-metadata 复核信息。
- 若 `tools/realsense_bag_compare.py` 显示 image header 与 metadata header 偏移超过 5 ms，先按 rosbag image header 对齐，并在报告中标记 metadata 不可信。

ZMQ：

- `source=1` GELLO：joint 和 gripper command 可线性插值；训练因果观测时用最近过去。
- `source=2` robot：`q/dq/tau_J/tau_J_d/O_dP_EE` 可线性插值；`O_T_EE` 第一版按 16 个 float 线性插值并在报告说明，后续可升级为 SE(3) 插值。
- `source=3` gripper：`gPO/gCU` 默认最近帧，不做线性插值。
- `source=4` spacemouse：按 GELLO input 槽位解释，需根据 `valid_mask` 区分。

## 暂停、恢复和异常段处理

当前 MainController 只在 `COLLECTING` 状态写 demo buffer，因此暂停期间的主控 `.npz` 不包含样本。但恢复后时间戳会出现大间隔，对齐工具需要显式切段：

- 优先从 `controller_events.jsonl` 读取 `pause_started`、`pause_done`、`demo_collecting`、`realsense_fatal_detected`、`realsense_restart_*`。
- 如果事件缺失，按 stream gap 切段：
  - FT300S gap > 50 ms。
  - Xense/RealSense gap > 120 ms。
  - ZMQ gap > 80 ms。
- 每段单独生成目标时间轴，不跨段插值。
- RealSense fatal restart 后，重启前后必须分段；不要跨相机重启插值或最近帧匹配。

注意一个当前实现风险：MainController 允许在 `PAUSED` 状态执行 `d`，但 FT300S/XenseTacSensor 服务端当前只在 `COLLECTING` 状态接受 `DEMO_DONE_REQ`。如果从暂停直接完成，可能拿不到 `saved_file`，进而无法读取完整 `.npy`。对齐工具应在 `saved_file is None` 时直接报错并提示先修正采集流程，或只生成主控索引级对齐。

## 推荐输出

输出目录：`<demo_dir>/aligned/`

文件：

- `aligned_index.npz`
  - `t_ns`
  - `segment_id`
  - `sample_valid`
  - `<stream>_index`
  - `<stream>_time_ns`
  - `<stream>_delta_ns`
  - `<stream>_valid`
  - RealSense 额外保存 `<camera>_<stream>_topic`、`frame_number`、`bag_message_index`

- `aligned_numeric.npz`
  - 可直接进入 numpy 训练的数据。
  - 第一版建议包含 FT300S wrench、Xense force summary、ZMQ robot/GELLO/gripper 数值。
  - 不直接保存 RealSense 图像。

- `aligned_manifest.json`
  - 输入 demo 路径和所有源文件路径。
  - base/mode/hz/tolerance 配置。
  - 每个 stream 的帧数、使用帧数、invalid 数量、最大/均值/中位数 `abs(delta_ns)`。
  - ZMQ 每个 source 的 clock offset 和 clock health。
  - RealSense image-metadata 对比结果。
  - drop monitor 摘要。

- `alignment_report.md`
  - 面向人工检查的简短报告。
  - 列出缺流、时钟偏移、超窗口样本比例、重启/暂停段。

## 实现步骤

建议新增工具：`tools/align_demo_timestamps.py`

### 1. 读取和路径解析

- 参数：`--demo-dir`、`--base`、`--mode`、`--hz`、`--output-dir`。
- 读取 `manifest.json`。
- 读取四个主控 `.npz`。
- 用 `manifest.sensor_saved_files.ft300` 拼接 `FT300S/runtime_frames/<filename>`。
- 用 `manifest.sensor_saved_files.xense` 拼接 `XenseTacSensor/runtime_frames/<filename>`。
- 检查 `rosbag_uri` 是否存在，并自动探测 storage backend。

### 2. 标准化 stream table

内部统一成表：

```text
stream_name
time_ns:int64
frame_key:int64 or str
payload_ref
recv_time_ns:int64 optional
quality flags
```

必须保留原始字段，不覆盖原始 timestamp。

### 3. 质量检查

- frame id/seq/frame_number 连续性。
- 时间单调性。
- 主控索引和传感器 `.npy` 的 timestamp 差值。
- ZMQ raw stamp 与 recv_time 的 offset/jitter。
- RealSense metadata header 与 rosbag image header 对比。

质量检查失败不一定中止，但 required stream 缺失、timestamp 非单调严重错误、ZMQ clock 漂移严重时应让命令返回非零，除非传入 `--allow-degraded`。

### 4. 生成目标时间轴

- `realsense:<topic>`：用该 topic 的有效帧时间。
- `robot`：用 ZMQ source 2 的有效帧时间。
- `grid`：按 required streams 的交集时间范围生成等间隔网格。
- 根据暂停/重启/gap 分段，禁止跨段插值。

### 5. 匹配和插值

- 每个 stream 先用 `np.searchsorted` 找目标时间附近样本。
- 因果模式取左侧样本。
- 最近模式比较左右样本绝对差。
- 连续流在最近模式使用 `np.interp` 或逐列插值。
- 不可插值流只输出索引和引用。

### 6. 保存结果和报告

- 保存 `aligned_index.npz`、`aligned_numeric.npz`。
- 保存 `aligned_manifest.json`。
- 保存 `alignment_report.md`。
- 终端打印摘要：样本数、有效率、每个 stream 最大/中位 delta、ZMQ offset、RealSense 对比 verdict。

## 验收标准

最小单元测试：

- 加载 synthetic manifest + `.npz`，能生成 grid 时间轴。
- 因果模式不会选择未来帧。
- nearest 模式能正确选择左右最近帧。
- ZMQ offset 用稳健中位数修正后，delta 接近 0。
- Xense 双传感器组合时间使用 `max(ts0, ts1)`。
- 暂停 gap 不跨段插值。

真实数据验收：

- 对一个 `s -> d` demo，生成 `aligned/` 四个文件。
- `aligned_manifest.json` 中每个 required stream 有效率大于 95%。
- RealSense image header 与 metadata header 对比 95% 样本在 5 ms 内。
- ZMQ 各 source 的 offset jitter 中位绝对偏差小于 5 ms；远端未同步时报告必须明确标记。
- FT300S 主控索引和 `.npy` 时间戳按 frame_id 对齐，差值为 0 或仅有极小序列化差异。
- Xense 主控索引和 `.npy` 时间戳按 frame_id 对齐，两个 sensor timestamp 均能匹配。

## 需要优先修正或确认的点

1. 修正暂停状态完成 demo 的服务端兼容性：FT300S/XenseTacSensor 应允许 `PAUSED -> DEMO_DONE_REQ` 和 `PAUSED -> DEMO_DISCARD_REQ`，否则 manifest 可能没有 `saved_file`。
2. MainController 的 RealSense metadata topic 应运行时发现，或至少根据实际启用相机配置生成，避免长期订阅不存在的 `cam1/cam2/cam4`。
3. rosbag recorder 当前记录四路 image topic，而相机 launch 当前只启用 `cam3`；对齐工具应自动发现实际 topic，但采集配置最好保持一致。
4. 远端 ZMQ producer 需要明确是否和主控主机做 NTP/PTP 同步。未同步时只能使用 offset 估计，精度受网络延迟影响。
5. RealSense `frame_timestamp_ns/hw_timestamp_ns` 的时钟语义需要一次实测确认。确认前，跨源对齐不要直接使用它们替代 ROS header stamp。
