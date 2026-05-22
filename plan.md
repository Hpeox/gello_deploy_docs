# MainController 主控制器计划

## Summary

这是一个用于具身智能模仿学习数据采集的代码项目，目前已经实现了 FT300S、XenseTacSensor、RealSense 和 ZMQ 参考模块。下一步需要在 `MainController/src/MainController` 内实现统一主控：启动 FT300S、XenseTacSensor、RealSense 两个 launch，并强制连接 ZMQ telemetry relay。

主控启动后自动初始化，交互命令只保留 `s/p/d/x/q`。每个 demo 保存原始时间戳、ZMQ 遥测、各模块落盘文件、rosbag 信息，并实时监控所有来源的丢帧和异常帧间隔。

## Key Changes

- 新增 `main_controller` console script，作为主控启动入口。
- MainController 是 ROS2 `ament_python` 包，但主体按普通 Python 控制程序设计；通过 `ros2 run MainController main_controller` 启动，内部使用线程、队列、子进程、UDS、ZMQ 和少量 rclpy service/subscriber。
- ZMQ telemetry 是必须项，但保留默认参数：
  - `--zmq-connect` 默认 `tcp://127.0.0.1:6000`
  - 远程 relay 不在本机时，用 `--zmq-connect tcp://<host>:6000` 覆盖。
  - 主控启动时必须建立 ZMQ receiver，并在进入采集前等到首个合法 telemetry frame 或超时报错退出。
  - ZMQ receiver 从主控启动到退出必须持续读取，即使主控处于 `WAIT_START`、`PAUSED`、`FINALIZING` 或 `DISCARDING`，也不能停止 drain socket。
- ZMQ 协议在 `MainController` 内独立实现：
  - 参考 `Zmq_Ref` 的 504-byte binary layout。
  - 不 import `Zmq_Ref` 目录内代码。
  - 使用 `zmq.Poller` 阻塞接收，不使用 1ms 空轮询。
  - demo 间保持读取，并维护环形缓冲或直接丢弃非 demo 数据。
- 启动子进程：
  - FT300S: `conda run -n Modbus314 python -m FT300S.app --uds-path /tmp/ft300_sensor.sock --shm-name ft300_sensor_frame --fps 100`
  - XenseTacSensor: `conda run -n Xense310 python -m XenseTacSensor.app --uds-path /tmp/xense_sensor.sock --shm-name xense_sensor_frame --fps 30`
  - RealSense: `conda deactivate` 后直接执行 `ros2 launch`，不重复 `source`。
- UDS 控制：
  - 主控启动后自动连接 FT300S/XenseTacSensor UDS，并自动完成 `INIT_REQ`/`INIT_READY` 握手。
  - 用户不输入 `i`。
  - `s/p/d/x/q` 分别映射到 `START_REQ/PAUSE_REQ/DEMO_DONE_REQ/DEMO_DISCARD_REQ/STOP_REQ`。
- RealSense 时间戳策略：
  - 主控实时订阅 `/camX/camera/color/metadata` 和 `/camX/camera/depth/metadata`，不持续订阅 `image_raw`。
  - 开始或恢复录制前，主控会对 required image topics 做一次短暂 readiness baseline 检查。
  - 实时丢帧监控使用 metadata 的 `frame_number`、`header.stamp`、`frame_timestamp`、`hw_timestamp`。
  - rosbag2 仍记录 image topic；离线可用 `tools/realsense_bag_compare.py` 验证 metadata 与 image header 的对应关系。

## Data And Logs

- 控制日志保存为 `controller_events.jsonl`：低频控制日志、状态迁移、错误、丢帧告警、RealSense 重启事件，方便 tail 和人工排查。
- 高频采集索引保存为 `.npz`：
  - `ft300_timestamps.npz`
  - `xense_timestamps.npz`
  - `realsense_metadata.npz`
  - `zmq_telemetry.npz`
- 使用 `.npz` 的理由：高频数据结构规则、数值密集，后处理对齐会直接进入 numpy；比 JSONL 更小、更快。默认不压缩，避免 demo 完成时 CPU 压缩阻塞。
- `manifest.json` 记录 demo 起止时间、bag URI/segment、传感器 `.npy` 文件、主控 `.npz` 文件、帧数、告警计数、RealSense 重启次数和完成/放弃状态。

## State Machine

### 状态定义

- `BOOT`：主控进程刚启动，尚未启动依赖服务。
- `STARTING_SERVICES`：正在启动 FT300S、XenseTacSensor、RealSense camera launch、rosbag2 recorder、ZMQ receiver。
- `INIT`：依赖服务已启动，正在连接 UDS、等待 ZMQ 首帧、初始化传感器。
- `WAIT_START`：全部就绪，等待用户输入 `s` 开始 demo。
- `COLLECTING`：正在采集 demo，UDS/ZMQ/RealSense metadata buffers 正在写入。
- `PAUSED`：当前 demo 暂停；ZMQ 仍持续读取但不写入 demo buffer；恢复后重置 drop baseline。
- `FINALIZING`：正在完成 demo，等待传感器 flush ACK、暂停或停止 rosbag、保存 `.npz` 和 manifest。
- `DISCARDING`：正在放弃 demo，停止 rosbag 并丢弃 demo buffer。
- `STOPPING`：正在停止所有服务和子进程。
- `STOPPED`：主控退出前的终态。
- `ERROR`：不可恢复错误状态，例如必要模块启动失败、ZMQ 无首帧、UDS 初始化失败。

### 允许迁移

- `BOOT -> STARTING_SERVICES`
- `STARTING_SERVICES -> INIT`
- `STARTING_SERVICES -> ERROR`
- `INIT -> WAIT_START`
- `INIT -> ERROR`
- `WAIT_START -> COLLECTING`：用户输入 `s`
- `WAIT_START -> STOPPING`：用户输入 `q`
- `COLLECTING -> PAUSED`：用户输入 `p`，或 RealSense fatal error 自动触发暂停
- `COLLECTING -> FINALIZING`：用户输入 `d`
- `COLLECTING -> DISCARDING`：用户输入 `x`
- `COLLECTING -> STOPPING`：用户输入 `q`
- `PAUSED -> COLLECTING`：用户输入 `s`
- `PAUSED -> FINALIZING`：用户输入 `d`
- `PAUSED -> DISCARDING`：用户输入 `x`
- `PAUSED -> STOPPING`：用户输入 `q`
- `FINALIZING -> WAIT_START`
- `FINALIZING -> STOPPING`：finalizing 完成后执行已排队的 `q`
- `DISCARDING -> WAIT_START`
- `STOPPING -> STOPPED`
- `ERROR -> STOPPING`

### 状态语义

- 只有 `COLLECTING` 写入当前 demo buffer。
- `PAUSED` 不写入 demo buffer，但 ZMQ receiver 必须继续 drain socket。
- `d` 或 `x` 后回到 `WAIT_START`，ZMQ receiver 仍必须继续 drain socket；这些 demo 间数据不写入任何已结束或未开始的 demo buffer。
- `FINALIZING` 不接受新的 `s/p/d/x`，但允许排队 `q`，待落盘完成后停止。
- 传感器 `DEMO_DONE_REQ` flush 不设硬超时，只周期性输出进度并写 log。

## Drop Monitoring

主控为每个来源维护独立监控器，实时检查：

- frame id、seq 或 metadata frame_number 不连续。
- 帧间隔显著大于目标周期。

告警必须同时输出到终端和 `controller_events.jsonl`。

每个检测到的正向 key 不连续事件都会发出一个 `drop_warning`。监控器按正向 key gap
累计 `missing_frame_count`，并按实际发出的 warning 数累计 `warning_count`。该规则同样
适用于 FT300S `frame_id`、Xense `frame_id`、ZMQ 每个 source 的 `seq`，以及 RealSense
metadata 每个 topic 的 `frame_number`。大 timestamp interval 是独立的 `drop_warning`
reason；同一帧既可能因为 non-contiguous key 告警，也可能因为 large interval 告警。

默认阈值：

- FT300S: 100 Hz，目标间隔 10 ms，超过 20 ms 告警。
- XenseTacSensor: 30 Hz，目标间隔 33.3 ms，超过 66.7 ms 告警。
- ZMQ: 每个 source 独立 50 Hz，目标间隔 20 ms，超过 40 ms 告警；seq 按 source 独立检查。
- RealSense metadata: 每个 stream 独立 30 Hz，目标间隔 33.3 ms，超过 66.7 ms 告警。

其他规则：

- 暂停、恢复、demo 边界不计入丢帧；恢复后首帧重置该来源的 interval baseline。
- 每个 demo 的 manifest 记录各来源 monitor summary，包括 `warning_count`、
  `missing_frame_count` 和 `max_interval_ns`。这些 warning/statistics 字段用于采集后
  operator review，不触发自动 pause/abort。

## RealSense Error Recovery

- `ProcessMonitorThread` 持续读取 RealSense launch stdout/stderr。
- 捕捉到以下字符串时触发恢复：
  - `Hardware Error`
  - `Depth stream start failure`
- 如果 fatal error 发生在 `COLLECTING`：
  - 主控先迁移到 `PAUSED`。
  - 向 FT300S/XenseTacSensor 发送 `PAUSE_REQ`。
  - 调用 `/rosbag2_recorder/pause` 暂停当前 bag recording。
  - 暂停 demo buffer，并重置 RealSense metadata drop baseline。
  - 然后重启 RealSense camera launch。
- 如果 fatal error 发生在 `WAIT_START` 或 `PAUSED`，直接重启 RealSense，不改变业务状态。
- 如果 fatal error 发生在 `FINALIZING` 或 `DISCARDING`，记录告警和重启需求，但不打断当前收尾流程。
- 重启流程：
  - 对 RealSense camera launch 进程组发送 SIGINT，等同 Ctrl-C。
  - 等待短暂 grace period。
  - 未退出再 SIGTERM/SIGKILL。
  - 用原命令重新启动 launch。
  - 自动重启不会自动恢复采集；用户确认设备恢复后再输入 `s` 继续。

## Runtime Flow

### 启动阶段

1. 创建 `runtime_sessions/session_<timestamp>/`。
2. 启动 FT300S、XenseTacSensor、RealSense camera launch、rosbag2 recorder launch。
3. 启动 ZMQ receiver，使用默认或用户传入 endpoint 等待首个合法 telemetry frame。
4. 连接 FT300S/XenseTacSensor UDS，并自动完成 `INIT_READY`。
5. 启动 RealSense metadata 订阅。
6. 所有必要模块就绪后进入 `WAIT_START`。

### `s`: start/resume demo

1. 创建或继续当前 demo。
2. 向 FT300S/XenseTacSensor 发送 `START_REQ` 并等待 ACK。
3. 首次启动该 segment 时调用 `/rosbag2_recorder/record` 指定当前 demo bag URI。
4. 每次开始或恢复录制时都调用一次 `/rosbag2_recorder/resume`。这是 rosbag2 recorder 的必要步骤，`record` 后也必须调用 `resume` 才进入实际录制。
5. 开启 FT300S/XenseTacSensor/ZMQ/RealSense metadata demo buffer 和丢帧监控。

MainController 是多传感器 start/resume 事务的 owner。`START_REQ` 对 FT300S 和
XenseTacSensor 是 all-or-nothing：若任一 required sensor 返回 `ERROR`、超时，
或 rosbag `record` / `resume` 失败，MainController 必须向已经 ACK `START_REQ`
的 sensor 发送 `DEMO_DISCARD_REQ` 回滚，写入轻量 `manifest.json`，并清空当前
demo context。该 manifest 使用 `status: "failed"`，记录 `failure_stage`、
`failure_reason`、已 ACK 的 sensor、rollback action/result，以及当前 rosbag
record/resume 状态。start/resume 事务失败不保存高频 `.npz`。`status: "discarded"`
只表示用户通过 `x` 发起且成功完成的 discard；系统事务失败即使使用
`DEMO_DISCARD_REQ` 回滚，也必须记录为 `failed`。`PAUSE_REQ`、`DEMO_DONE_REQ`
和用户 `DEMO_DISCARD_REQ` 的 partial failure 规则单独定义，不依赖“两个 sensor
总是同时成功或失败”的假设。

RealSense image topic list 是录制准入和 rosbag post-check 的权威来源。正式模式
默认要求 4 台相机 / 8 个 image topics：`cam1` 到 `cam4` 的 color `image_raw` 和
`aligned_depth_to_color/image_raw`。只有显式配置 `debug_degraded` 模式时，才允许使用
配置中的 topic 子集；该子集成为本次运行的 required list。每次 rosbag `record` /
`resume` 前，主控必须收到每个 required image topic 的至少一帧，并记录稳定 baseline
字段：topic name、message type、width、height、encoding、step、stream role。缺失
topic 或 baseline mismatch 会阻止正式录制，并写入 `status: "failed"` manifest。
完成 demo 时，主控使用当前 demo 的实际 rosbag URI 读取 rosbag metadata，检查 required
topics 是否存在、message type 是否匹配、frame count 是否非零，并要求 required topics
之间的 count skew 不超过配置阈值。post-check 失败时 manifest 状态为 `failed`，不是
`done`。metadata topics 仍是实时监控来源；image topics 是 readiness 与 rosbag recording
校验来源。

### `p`: pause demo

1. 向 FT300S/XenseTacSensor 发送 `PAUSE_REQ` 并等待 ACK。
2. 调用 `/rosbag2_recorder/pause` 暂停当前 bag recording，不使用 `stop`。
3. 暂停 buffers，并重置恢复后的 interval baseline。

### `d`: finish and save demo

1. 向 FT300S/XenseTacSensor 发送 `DEMO_DONE_REQ`。
2. 等待 ACK，并读取 ACK payload 中的 `saved_file`；等待期间不设硬超时，只周期性写进度日志。
3. 调用 `/rosbag2_recorder/stop` 停止当前 recording。
4. 保存主控侧 `.npz`、`manifest.json` 和告警统计。

### `x`: discard demo

1. 向 FT300S/XenseTacSensor 发送 `DEMO_DISCARD_REQ`。
2. 调用 `/rosbag2_recorder/stop` 停止当前 recording。
3. 丢弃主控侧 demo buffer，只保留 controller log。

### `q`: stop all

1. 若正在采集或暂停，先安全停止当前 demo/recording。
2. 向 FT300S/XenseTacSensor 发送 `STOP_REQ`。
3. 关闭 ZMQ、ROS node、子进程。

## Timestamp Alignment

- 第一版完成原始数据收集、实时丢帧监控和 manifest，不生成最终插值对齐数据集。
- 所有来源的时间戳必须分别保存，用于后处理对齐。
- 后续对齐应支持两种模式：
  - 以触觉传感器或 RealSense 相机时间戳作为帧基准，对可插值数据做因果插值。
  - 根据主控开始录制时间生成理想时间网格，对不可插值流选择最近帧，对可插值流做插值，不强调因果。
- 对齐阶段应从各模块实际保存路径读取完整传感器数据，例如 `runtime_frames` 中的 `.npy`，ZMQ 数据直接使用主控保存的 `zmq_telemetry.npz`。
- 对于 XenseTacSensor 和 FT300S，必须等待 UDS `DEMO_DONE_REQ` ACK，并读取 ACK payload 中的 `saved_file` 后，再读取模块落盘文件。
- RealSense 对齐优先使用 metadata 时间戳；必要时用 rosbag image header 离线复核。

## Test Plan

- 单元测试：
  - ZMQ unpacker 验证 504-byte payload 字段解析。
  - mock UDS server 验证自动初始化与 `s/p/d/x/q` ACK 流程。
  - drop monitor 验证连续帧、跳 seq、超时 interval、暂停恢复 baseline。
  - RealSense metadata JSON parser。
  - `.npz` 保存测试验证字段长度一致。
- RealSense 测试：
  - 在正确 ROS2 Python 环境下运行 `tools/realsense_bag_compare.py`，验证 metadata 与 image header 时间戳一致性。
  - 注入 `Hardware Error` 和 `Depth stream start failure` 日志，验证 RealSense 自动暂停和重启。
- 集成测试：
  - 默认 `--zmq-connect tcp://127.0.0.1:6000` 本机 relay 可启动。
  - 远程 relay 可用显式 endpoint 覆盖。
  - ZMQ 在 pause/finalizing 时持续发送，验证主控仍持续 drain。
  - mock 环境下重点覆盖正常连续 demo 流程：`s -> d -> s -> d` 和 `s -> x -> s -> d`；验证 `d/x` 后 ZMQ 仍持续 drain，且 demo 间数据不污染下一段或上一段 demo buffer。
  - 真实 FT300S/XenseTacSensor 下执行 `s -> p -> s -> d -> q`、`s -> d -> s -> d -> q`、`s -> x -> s -> d -> q`，检查 manifest、传感器 `.npy`、主控 `.npz`、rosbag、告警统计齐全。

## Assumptions

- 默认 ZMQ endpoint 为 `tcp://127.0.0.1:6000`。
- ZMQ telemetry 是必须模块，不能通过普通参数关闭。
- RealSense metadata topic 对当前启用相机可用。
- 主控仅负责启动、控制、采集索引、丢帧监控和 manifest；最终对齐数据集生成另行实现。
