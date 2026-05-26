# 时间戳对齐具体方案

## 目标和边界

本文对照 `plan.md`、`implement_plan.md` 和当前代码，给出第一版时间戳对齐方案。当前 MainController 已完成原始时间戳采集、实时丢帧监控、`.npz` 索引和 `manifest.json` 保存；后续需要新增主控内部自动对齐模块，并在 `tools/` 下保留相似但独立的命令行对齐工具。

第一版目标：

- MainController 在用户输入 `d` 后，于 `FINALIZING` 阶段调用 `main_controller/timestamp_alignment.py`，输入一个已完成 demo 目录，例如 `runtime_sessions/demos/demo_*/`。
- 读取 `manifest.json`、主控保存的 `.npz`、RealSense rosbag image header；FT300S/XenseTacSensor 自己落盘的 `.npy` 路径记录到 source manifest，第一版不读取其内容。
- 统一转换为 `int64` Unix epoch nanoseconds。
- 自动生成对齐配置、对齐索引、对齐 manifest 和人工报告，保留每个来源的原始时间戳、匹配索引、时间差和有效性 mask。
- `tools/align_demo_timestamps.py` 作为相似但独立的 CLI 对齐工具，不跨目录 import `main_controller.timestamp_alignment`，可用于重跑、调参或诊断。
- 主控和独立 CLI 均提供 `--alignment-base-source realsense|xense`；默认 `realsense`，选择 `xense` 时固定使用 `timestamp_ns_0` 作为目标时间轴。

第一版不做：

- 不在采集过程中插值或阻塞保存。
- 不强行重采样 RealSense 图像内容到 `.npz`；图像仍通过 rosbag topic、frame index、frame_number 引用。
- 不把不同机器的远端 ZMQ 时间戳当成天然可信；必须做时钟偏移检查或修正。
- 主控自动对齐不生成 `aligned_numeric.npz` 等实际训练数据文件。
- materialize 实际数据集暂不实现；TODO：需要确认数据集具体组织格式后，再规划读取已有索引/配置并生成实际数据文件的工具。

## 当前实现中的可用时间戳

### MainController manifest

位置：`runtime_sessions/demos/demo_*/manifest.json`

关键字段：

- `started_ns`：主控创建 demo 时的 `time.time_ns()`，早于传感器 ACK 和 rosbag resume，只能作为粗边界。
- `finished_ns`：主控保存 manifest 时的 `time.time_ns()`，晚于传感器 flush 和 rosbag stop，只能作为粗边界。`FINALIZING` 中传感器 `DEMO_DONE_REQ` 与 rosbag `stop` 会并发发出；若本次采集显式使用 `sensor_flush_timeout_s=none` / `unbounded`，该边界仍可能明显晚于采集停止时刻，这是预期的无界 flush 等待结果，离线对齐不应把这段等待时间解释为有效采集窗口。
- `run_id`：本 demo 所属的 MainController 运行 ID。
- `rosbag_uri`：当前 demo 的 rosbag 目录，相对 demo 目录保存，通常为 `rosbag`。
- `sensor_paths.ft300`、`sensor_paths.xense`：可直接用于后处理的 `.npy` 路径，相对仓库根保存，例如 `runtime_frames/data_FT_*.npy`。active-demo abort 使用 `STOP_REQ` 尝试 flush sensor，因此 `saved_file` 是 best-effort optional 字段；缺失时对应路径为 `None`。
- `npz.ft300`、`npz.xense`、`npz.realsense`、`npz.zmq`：主控保存的时间戳索引，相对 demo 目录保存。
- `drop_monitors`：本 demo 内各来源丢帧和最大间隔统计，不包含前后 demo 的累计值。
- `realsense_restart_events`：本 demo 内 RealSense 重启时间点；run-wide 累计值使用独立的 `run_realsense_restart_*` 字段。
- `alignment`：自动对齐结果字段。采集 `status` 只表示 `done` / `discarded` / `failed` 采集事务；对齐成功、失败或跳过分别写入 `manifest.alignment.status`，不得改写采集 `status`。

### FT300S

主控索引：`ft300_timestamps.npz`

- `frame_id`
- `timestamp_ns`：FT300S 进程读帧前后的 `time.time_ns()`，当前实现中在 `SensorClient.read_frame()` 开头生成。
- `recv_time_ns`：主控 UDS 收到 `FRAME_READY` 时的 `time.time_ns()`。
- `recv_monotonic_ns`：主控 UDS 收到消息时的 `time.monotonic_ns()`。

完整数据：`runtime_frames/<saved_file>`

- 路径来自 `manifest.sensor_paths.ft300`。
- `runtime_frames` 指仓库根目录下的 `runtime_frames`，例如 `/home/robot/Desktop/gello-deploy/runtime_frames`。
- 路径解析规则：拼接为 `repo_root / manifest.sensor_paths.ft300`。
- 数据结构是 `np.load(..., allow_pickle=True).item()`，包含 `events` 和 `frames_data`。
- `frames_data["00000"]["ft300_timestamp_ns"]` 应与主控索引中的 `timestamp_ns` 按 `frame_id` 对应。
- wrench/力矩等完整值在同一个 per-frame dict 中。
- 当前第一版对齐只使用主控索引 `ft300_timestamps.npz`；完整 `.npy` 读取与 timestamp 交叉校验列为后续增强。

推荐主时间戳：`ft300_timestamp_ns` 或主控索引 `timestamp_ns`。两者按 `frame_id` 交叉校验，差值异常时用主控索引告警但不静默覆盖。

### XenseTacSensor

主控索引：`xense_timestamps.npz`

- `frame_id`
- `timestamp_ns_0`：读取 sensor 0 前的 `time.time_ns()`。
- `timestamp_ns_1`：读取 sensor 1 前的 `time.time_ns()`。
- `recv_time_ns`
- `recv_monotonic_ns`

完整数据：`runtime_frames/<saved_file>`

- 路径来自 `manifest.sensor_paths.xense`。
- `runtime_frames` 指仓库根目录下的 `runtime_frames`，例如 `/home/robot/Desktop/gello-deploy/runtime_frames`。
- 路径解析规则：拼接为 `repo_root / manifest.sensor_paths.xense`。
- 数据结构是 `np.load(..., allow_pickle=True).item()`，包含 `events` 和 `frames_data`。
- `frames_data["00000"]["OG000544_timestamp_ns"]`、`frames_data["00000"]["OG001009_timestamp_ns"]` 分别对应两个触觉传感器。
- 当前第一版对齐只使用主控索引 `xense_timestamps.npz`；完整 `.npy` 读取与 timestamp 交叉校验列为后续增强。

推荐主时间戳：

- 两个触觉传感器独立作为 `xense_0` 和 `xense_1` stream 参与匹配。
- `xense_0` 使用 `timestamp_ns_0`，`xense_1` 使用 `timestamp_ns_1`。
- 选择 Xense 作为因果对齐基准时固定使用 `timestamp_ns_0`，即 `--base xense:0`。

### RealSense

主控索引：`realsense_metadata.npz`

- `topic`
- `frame_number`
- `header_stamp_ns`：metadata ROS header stamp。
- `frame_timestamp_ns`：metadata JSON 中 `frame_timestamp` 从毫秒转纳秒；它来自 librealsense `frame.get_timestamp()`，必须结合 `clock_domain` 解释。
- `hw_timestamp_ns`：metadata payload 中 `RS2_FRAME_METADATA_FRAME_TIMESTAMP` 从毫秒转纳秒，用于诊断，不作为跨源对齐主轴。
- `clock_domain`：metadata JSON 中的 timestamp domain，保存进 `realsense_metadata.npz`。如果某帧 metadata JSON 缺少该字段，保存为空值并在 log/report 中告警，不导致采集失败；目前没有旧 `.npz`，不做缺列兼容。
- `recv_time_ns`
- `recv_monotonic_ns`

rosbag：`manifest.rosbag_uri`

- 当前 rosbag launch 记录 image topic，不记录 metadata topic。
- 第一版优先用 image topic 的 `header.stamp` 作为图像时间戳；rosbag 读取失败或不可用时 fallback 到 metadata `header_stamp_ns`。

推荐主时间戳：

- 用于跨源对齐时，默认使用 rosbag image `header.stamp` 或 metadata `header_stamp_ns`。二者应处于同一个 ROS wrapper 时间基准。
- `header_stamp_ns` 是 RealSense ROS wrapper 计算出的 ROS header time。同一帧的 image、camera_info 和 metadata 使用同一类 header time。
- `frame_timestamp_ns` 不是天然等于 `header_stamp_ns`。它的含义由 `clock_domain` 决定：
  - `HARDWARE_CLOCK`：相机硬件时钟。此时 `header_stamp_ns = ros_time_base_ns + (frame_timestamp_ms - camera_time_base_ms) * 1e6`，离线只看 `frame_timestamp` 无法唯一恢复 ROS epoch time。
  - `SYSTEM_TIME` 或 `GLOBAL_TIME`：通常已经映射到 OS/system time，`header_stamp_ns` 应接近 `frame_timestamp_ns`，但仍以实际 header stamp 为准。
- `hw_timestamp_ns` 只用于诊断设备侧时间，不直接和 `time.time_ns()`、ZMQ Unix stamp 混用。
- alignment report 第一版输出 `clock_domain` 分布和缺失统计；基于 `clock_domain` 的
  `metadata_header_ns - frame_timestamp_ns` 诊断列为后续增强。

注意：formal 采集默认要求 `cam1` 到 `cam4` 的 color `image_raw` 和 `aligned_depth_to_color/image_raw` 共 8 个 image topic。对齐工具必须以 `manifest.realsense_image_readiness.required_topics` 或 `manifest.realsense_rosbag_postcheck.required_topics` 为准；`debug_degraded` 采集则只使用 manifest 中记录的 required 子集。

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

第一版实现应支持 `--alignment-base-source realsense|xense`，并支持手动 `--base` 覆盖：

1. `--base realsense:<topic>` 或 `--alignment-base-source realsense`
   - 推荐给视觉模仿学习数据集。
   - 目标时间轴直接使用指定 RealSense metadata 或 rosbag image 的帧时间戳。
   - 图像不可插值，因此以相机帧为样本基准最稳。

2. `--base xense:0` 或 `--alignment-base-source xense`
   - 使用 Xense `timestamp_ns_0` 作为目标时间轴。
   - 两路触觉传感器仍分别输出 `xense_0_*` 和 `xense_1_*` 匹配字段。

3. `--base robot`
   - 推荐给控制/状态闭环分析。
   - 使用 ZMQ `source=2` robot telemetry 的时间戳作为目标时间轴。
   - 适合 50 Hz 左右的 robot state/action 数据。

4. `--base grid --hz <rate>`
   - 推荐给对齐质量检查或固定频率训练样本。
   - `start_ns = max(first_valid_time of required streams)`
   - `end_ns = min(last_valid_time of required streams)`
   - 生成 `[start_ns, end_ns]` 内的理想等间隔网格。

启动暖机裁剪参数：

- `--start-trim-s <seconds>`：默认 `0.0`。对每个 segment，目标时间轴从 `segment_overlap_start_ns + start_trim_s` 开始。
- `segment_overlap_start_ns = max(first_valid_time_ns of required streams)`，确保所有 required stream 已经有可用数据。
- `--stream-start-trim <stream>=<seconds>`：可重复传入，用于单独裁掉某个来源更长的启动暖机段。某 stream 的有效起点为 `first_valid_time_ns + stream_start_trim_s`，最终 segment 起点仍取所有 required stream 有效起点的最大值。
- 这些参数只裁剪对齐样本，不修改任何原始时间戳；不要把它们命名或理解为时间戳平移参数，避免和 ZMQ clock offset、RealSense clock-domain 映射混淆。
- 推荐起点：普通采集用 `--start-trim-s 1.0`，正式多 RealSense 采集先用 `--start-trim-s 2.0`，再根据真实 alignment report 调小。

默认建议：

- 有 RealSense 图像参与训练时，用 `--alignment-base-source realsense` 或 `--base realsense:<topic>` 指定某条 required image/metadata topic。
- 只做触觉优先的因果对齐时，用 `--alignment-base-source xense` 或 `--base xense:0`，目标时间轴为 `timestamp_ns_0`。
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

- FT300S：过去 20 ms 内有效。输出字段使用 `ft300s_*`，报告显示为 `FT300S`，其中 `S` 是型号名组成部分。
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
- 两个触觉传感器独立输出为 `xense_0_*` 和 `xense_1_*`；不生成两路固定偏差统计。

RealSense：

- image 不插值。
- color/depth/aligned depth 使用 rosbag image `header.stamp` 选择帧。
- metadata 的 `frame_number` 作为 continuity 信息。
- 默认按 rosbag image `header.stamp` 或 metadata `header_stamp_ns` 对齐；`frame_timestamp_ns` 只做一致性检查。
- 后续增强：若 `clock_domain == HARDWARE_CLOCK`，允许 `metadata_header_ns - frame_timestamp_ns` 存在大 offset，但其随时间的漂移应稳定；必要时按 topic 拟合线性/base-time 映射并写入报告。
- 后续增强：若 `clock_domain != HARDWARE_CLOCK`，`metadata_header_ns` 与 `frame_timestamp_ns` 应接近；若偏差持续超过 5 ms，应在报告中标记该 topic 的 timestamp-domain 检查失败。

ZMQ：

- `source=1` GELLO：joint 和 gripper command 可线性插值；训练因果观测时用最近过去。
- `source=2` robot：第一版 timestamp alignment 只输出索引和 delta；`q/dq/tau_J/tau_J_d/O_dP_EE` 线性插值、`O_T_EE` 插值和后续 SE(3) 插值升级应放到 materialize / 数据生成阶段。
- `source=3` gripper：`gPO/gCU` 默认最近帧，不做线性插值。
- `source=4` spacemouse：按 GELLO input 槽位解释，需根据 `valid_mask` 区分。

## 暂停、恢复和异常段处理

当前 MainController 只在 `COLLECTING` 状态写 demo buffer，因此暂停期间的主控 `.npz` 不包含样本。第一版对齐模块不做显式切段，`segment_id` 全部为 `0`；恢复后时间戳大间隔的自动切段列为后续增强：

active demo 中发生 `q`、ZMQ receiver fatal、RealSense metadata fatal、UDS 非命令期
disconnect 或 required subprocess unexpected exit 时，采集 manifest 写
`status: "failed"`，保存已有主控侧 `.npz` 供诊断，但不运行自动 timestamp alignment。

- 优先从对应 `run_id` 的 `controller_events_run_*.jsonl` 读取 `pause_started`、`pause_done`、`demo_collecting`、`realsense_fatal_detected`、`realsense_restart_*`。
- 如果事件缺失，按 stream gap 切段：
  - FT300S gap > 50 ms。
  - Xense/RealSense gap > 120 ms。
  - ZMQ gap > 80 ms。
- 后续增强中，每段单独生成目标时间轴，不跨段插值。
- 后续增强中，RealSense fatal restart 后，重启前后必须分段；不要跨相机重启插值或最近帧匹配。

暂停状态直接完成 demo 已由 FT300S/XenseTacSensor 服务端支持：`PAUSED -> DEMO_DONE_REQ` 和 `PAUSED -> DEMO_DISCARD_REQ` 均为合法路径。若真实运行仍出现 `saved_file is None`，第一版对齐工具仍可基于主控 `.npz` 生成索引，但 source manifest 中对应完整 `.npy` 路径为空。

## 推荐输出

输出目录：`<demo_dir>/aligned/`

MainController 自动对齐默认文件：

- `alignment_config.json`
  - 记录输入 demo 路径、源文件路径、base/mode/hz/tolerance 配置、启动暖机裁剪参数和 required stream 列表。
  - 作为 `tools/align_demo_timestamps.py` 独立重跑时的可选输入。

- `aligned_index.npz`
  - `t_ns`
  - `segment_id`：当前第一版全部为 `0`，表示尚未切分 pause/restart/gap segment；真实分段支持列为后续增强。
  - `sample_valid`
  - `<stream>_index`
  - `<stream>_time_ns`
  - `<stream>_delta_ns`
  - `<stream>_valid`
  - RealSense 额外保存 `<camera>_<stream>_topic`、`frame_number`、`bag_message_index`

- `aligned_manifest.json`
  - 输入 demo 路径和所有源文件路径。
  - base/mode/hz/tolerance 配置。
  - 每个 stream 的帧数、使用帧数、invalid 数量、最大/均值/中位数 `abs(delta_ns)`。
  - ZMQ 每个 source 的 clock offset 和 clock health。
  - RealSense `clock_domain` 分布和缺失统计。
  - drop monitor 摘要。

- `alignment_report.md`
  - 面向人工检查的简短报告。
  - 列出缺流、时钟偏移、超窗口样本比例、重启/暂停段。

- 更新 `manifest.json`
  - 写入独立 `alignment` 字段，包含 `status`、`config_path`、`index_path`、`manifest_path`、`report_path`、`started_ns`、`finished_ns` 和错误摘要。
  - 自动对齐失败只写 `manifest.alignment.status = "failed"`，不改写采集 `status`。

不作为自动输出的文件：

- `aligned_numeric.npz` 或其他实际训练数据文件不由主控自动生成。
- TODO：未来 materialize 工具读取已有 `alignment_config.json` / `aligned_index.npz` / `aligned_manifest.json` 生成实际数据文件；`--emit-data`、`--fields` 等字段选择参数应放在 materialize 工具中，而不是当前 timestamp alignment CLI 中。

## 实现步骤

建议新增两个相似但独立的入口：

- `MainController/src/main_controller/main_controller/timestamp_alignment.py`：主控内部自动对齐模块，由 `d` 的 `FINALIZING` 流程调用。
- `tools/align_demo_timestamps.py`：命令行对齐工具，不跨目录 import 主控模块；可以从主控内部实现复制后按 CLI 需求修改。

暂不新增 `tools/materialize_aligned_data.py`；只保留 TODO，等待确认数据集具体组织格式。

### 1. 读取和路径解析

- 主控内部模块输入：`demo_dir`、`output_dir`、`base`、`mode`、`hz` 和暖机裁剪配置。
- CLI 参数：`--demo-dir`、`--repo-root`、`--alignment-base-source`、`--base`、`--mode`、`--hz`、`--output-dir`。若同时传入 `--base` 和 `--alignment-base-source`，以 `--base` 为准。
- 启动暖机参数：`--start-trim-s`、可重复的 `--stream-start-trim <stream>=<seconds>`。
- 当前 timestamp alignment CLI 不提供 `--emit-data` / `--fields`；实际数据 materialize 等待数据集格式确认后另行实现。
- 读取 `manifest.json`。
- 读取四个主控 `.npz`。当前第一版不读取 FT300S/Xense 完整 `.npy` 内容；只把 `manifest.sensor_paths.*` 写入 source manifest。
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

- 第一版输出每个 stream 的匹配数量、invalid 数量和 `abs(delta_ns)` 摘要。
- 第一版输出 RealSense `clock_domain` 分布和缺失数量。
- 后续增强：frame id/seq/frame_number 连续性、时间单调性、主控索引和传感器 `.npy` timestamp 差值、ZMQ raw stamp 与 recv_time 的 offset/jitter、`metadata_header_ns - frame_timestamp_ns` 关系检查。

第一版 required stream 缺失或目标时间轴为空会让命令失败；更细粒度质量门控列为后续增强。

### 4. 生成目标时间轴

- `realsense:<topic>`：用该 topic 的有效帧时间。
- `xense:0`：用 Xense `timestamp_ns_0`。
- `robot`：用 ZMQ source 2 的有效帧时间。
- `grid`：按 required streams 的交集时间范围生成等间隔网格。
- 每个 segment 先计算 required stream 的 overlap 起点，再应用 `--start-trim-s` 和 `--stream-start-trim`；禁止通过平移原始时间戳来规避启动阶段不齐。
- 当前第一版不做 pause/restart/gap 分段，`segment_id` 全部为 `0`；根据暂停/重启/gap 分段并禁止跨段插值列为后续增强。

### 5. 匹配和插值

- 每个 stream 先用 `np.searchsorted` 找目标时间附近样本。
- 因果模式取左侧样本。
- 最近模式比较左右样本绝对差。
- 当前第一版不物化连续 payload，不做 `np.interp`；连续流插值属于后续 materialize / 数据生成阶段。
- 不可插值流只输出索引和引用。

### 6. 保存结果和报告

- 保存 `alignment_config.json`。
- 保存 `aligned_index.npz`。
- 保存 `aligned_manifest.json`。
- 保存 `alignment_report.md`。
- 更新 `manifest.alignment`。
- 终端打印摘要：独立 CLI 输出生成路径、样本数、有效数、base 和 warnings；更丰富摘要列为后续增强。

主控自动调用到此结束，不生成实际训练数据。实际训练数据生成留给未来 materialize 工具；该路径仍需遵守连续数值流才可插值、图像和离散字段只输出索引/引用的规则。

## 验收标准

最小单元测试：

- 加载 synthetic manifest + `.npz`，能生成 grid 时间轴。
- 因果模式不会选择未来帧。
- nearest 模式能正确选择左右最近帧。
- ZMQ offset 用稳健中位数修正后，delta 接近 0。
- Xense 作为 base 时使用 `timestamp_ns_0`，两路触觉传感器分别输出 index/time/delta/valid。
- 后续增强：暂停 gap 不跨段插值。

真实数据验收：

- 对一个 `s -> d` demo，生成 `aligned/` 四个文件。
- `aligned_manifest.json` 中每个 required stream 有效率大于 95%。
- RealSense report 包含每个 topic 的 `clock_domain` 分布和缺失数量。
- 后续增强验收：ZMQ 各 source 的 offset jitter 中位绝对偏差小于 5 ms；远端未同步时报告必须明确标记。
- 后续增强验收：FT300S/Xense 主控索引和 `.npy` 时间戳按 frame_id 对齐，差值为 0 或仅有极小序列化差异。

## 需要优先修正或确认的点

1. 暂停状态完成 demo 的服务端兼容性已确认：FT300S/XenseTacSensor 允许 `PAUSED -> DEMO_DONE_REQ` 和 `PAUSED -> DEMO_DISCARD_REQ`。
2. MainController 的 RealSense metadata topic 应运行时发现，或至少根据 manifest 的 formal/debug_degraded required image topic list 约束后处理输入。
3. 对齐工具应以 manifest 中的 RealSense readiness/post-check required topic list 为准，不硬编码具体相机数量。
4. 远端 ZMQ producer 需要明确是否和主控主机做 NTP/PTP 同步。未同步时只能使用 offset 估计，精度受网络延迟影响。
5. 主控代码应持续保存 RealSense metadata JSON 中的 `clock_domain`；缺字段按 warning 处理，不改变采集状态。

## TODO
确认数据集具体组织形式
