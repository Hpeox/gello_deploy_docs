# MainController 主控制器计划

## Summary

这是一个用于具身智能模仿学习数据采集的代码项目，目前已经实现了 FT300S、XenseTacSensor、RealSense、ZMQ 参考模块，以及 `MainController/src/main_controller` 下的统一主控。后续重点是完整真实硬件链路联调、正式四相机验收，以及主控内置自动时间戳对齐模块和 `tools` 独立对齐工具实现。

主控启动后自动初始化，交互命令只保留 `s/p/d/x/q`。每个 demo 保存原始时间戳、ZMQ 遥测、各模块落盘文件、rosbag 信息，并实时监控所有来源的丢帧和异常帧间隔。用户输入 `d` 后，主控在 `FINALIZING` 中自动生成时间戳对齐配置、索引和报告；对齐结束前不回到 `WAIT_START`，因此不能开始下一次采集。

## Key Changes

- 新增 `main_controller` console script，作为主控启动入口。
- MainController 是 ROS2 `ament_python` 包，但主体按普通 Python 控制程序设计；通过 `ros2 run main_controller main_controller` 启动，内部使用线程、队列、子进程、UDS、ZMQ 和少量 rclpy service/subscriber。
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
  - FT300S: `conda run -n modbus314 python -m FT300S.app --uds-path /tmp/ft300_sensor.sock --shm-name ft300_sensor_frame --fps 100`
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
  - metadata JSON 中的 `clock_domain` 必须保存到 `realsense_metadata.npz`；如果单帧缺字段，保存为空值并告警，不导致采集失败。
  - 跨源对齐以 metadata `header_stamp_ns` 或 rosbag image `header.stamp` 为主时间；`frame_timestamp_ns` 必须结合 `clock_domain` 解释，`hw_timestamp_ns` 仅作诊断。
  - rosbag2 仍记录 image topic；离线可用 `tools/realsense_bag_compare.py` 验证 metadata header 与 image header 的对应关系。

## Data And Logs

- 控制日志保存为 `controller_events.jsonl`：低频控制日志、状态迁移、错误、丢帧告警、RealSense 重启事件，方便 tail 和人工排查。
- 高频采集索引保存为 `.npz`：
  - `ft300_timestamps.npz`
  - `xense_timestamps.npz`
  - `realsense_metadata.npz`
  - `zmq_telemetry.npz`
- 使用 `.npz` 的理由：高频数据结构规则、数值密集，后处理对齐会直接进入 numpy；比 JSONL 更小、更快。默认不压缩，避免 demo 完成时 CPU 压缩阻塞。
- `manifest.json` 记录 demo 起止时间、bag URI/segment、传感器 `.npy` 文件、主控 `.npz` 文件、帧数、告警计数、RealSense 重启次数和完成/放弃状态。采集 `status` 只表示 `done` / `discarded` / `failed` 采集事务；自动对齐结果写入独立 `manifest.alignment` 字段。

## State Machine

### 状态定义

- `BOOT`：主控进程刚启动，尚未启动依赖服务。
- `STARTING_SERVICES`：正在启动 FT300S、XenseTacSensor、RealSense camera launch、rosbag2 recorder、ZMQ receiver。
- `INIT`：依赖服务已启动，正在连接 UDS、等待 ZMQ 首帧、初始化传感器。
- `WAIT_START`：全部就绪，等待用户输入 `s` 开始 demo。
- `COLLECTING`：正在采集 demo，UDS/ZMQ/RealSense metadata buffers 正在写入。
- `PAUSED`：当前 demo 暂停；ZMQ 仍持续读取但不写入 demo buffer；恢复后重置 drop baseline。
- `FINALIZING`：正在完成 demo，等待传感器 flush ACK、停止 rosbag、保存 `.npz` 和 manifest，并调用主控内部时间戳对齐模块生成对齐配置、索引和报告。
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
- `FINALIZING -> WAIT_START`：采集收尾和自动对齐流程均结束后
- `FINALIZING -> STOPPING`：finalizing 完成后执行已排队的 `q`
- `DISCARDING -> WAIT_START`
- `STOPPING -> STOPPED`
- `ERROR -> STOPPING`

### 状态语义

- 只有 `COLLECTING` 写入当前 demo buffer。
- `PAUSED` 不写入 demo buffer，但 ZMQ receiver 必须继续 drain socket。
- `d` 或 `x` 后回到 `WAIT_START`，ZMQ receiver 仍必须继续 drain socket；这些 demo 间数据不写入任何已结束或未开始的 demo buffer。
- `FINALIZING` 不接受新的 `s/p/d/x`，但允许排队 `q`，待落盘和自动对齐流程结束后停止。自动对齐结束前不能开始下一次采集。
- 传感器 `DEMO_DONE_REQ` flush 默认使用有限 `sensor_flush_timeout_s=300`；只有显式配置为 `none` 或 `unbounded` 时才进入无界等待，并持续周期性输出进度和写 log。该无界等待是操作者显式选择的预期模式，用于现场确实可能超长 flush 的 sensor；在该模式下不会生成 `ack_timeout`，等待只由 ACK、对应 sensor `ERROR`、UDS disconnect 或主控停止打断。

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
4. 若任一 required sensor 的 `PAUSE_REQ` 返回 `ERROR` 或超时，MainController 记录
   `status: "failed"` manifest 和 per-sensor command result，进入 `ERROR` 并停止系统，
   避免一个 sensor 仍 collecting 而主控处于 `PAUSED` 的歧义状态。

### `d`: finish and save demo

1. 同时向 FT300S/XenseTacSensor 发送 `DEMO_DONE_REQ`，并调用 `/rosbag2_recorder/stop` 停止当前 recording。
2. 等待两个传感器 ACK 并读取 ACK payload 中的 `saved_file` filename，同时等待 rosbag stop 结果；默认最多等待 `sensor_flush_timeout_s=300`，只有显式配置为 `none` 或 `unbounded` 时才无界等待传感器 ACK，等待期间周期性写进度日志。
3. 汇总 sensor finish、rosbag stop 和 RealSense rosbag post-check 结果。
4. 保存主控侧 `.npz`、`manifest.json` 和告警统计。
5. 若采集 `status` 为 `done`，调用 `main_controller/timestamp_alignment.py` 生成 `<demo_dir>/aligned/alignment_config.json`、`aligned_index.npz`、`aligned_manifest.json` 和 `alignment_report.md`，并把结果写入 `manifest.alignment`；自动对齐不生成实际训练数据文件。
6. `done` 表示所有 required finish 操作完成。若任一 required sensor finish 失败，
   manifest 使用 `status: "failed"`，记录 `failure_stage`、`failure_reason`、
   per-sensor command result 和已有 `saved_file`，并停止系统。
   自动对齐失败只更新 `manifest.alignment.status = "failed"`，不改写采集 `status`。

### `x`: discard demo

1. 向 FT300S/XenseTacSensor 发送 `DEMO_DISCARD_REQ`。
2. 调用 `/rosbag2_recorder/stop` 停止当前 recording。
3. 写入 lightweight `manifest.json`，`status: "discarded"`，包含 summary fields 和
   `frame_counts`，但 `npz` 记录为空且不保存高频 `.npz` artifacts。
4. 丢弃主控侧 demo buffer。
5. `discarded` 只表示用户发起的 discard transaction 成功完成。若 required sensor
   discard 或 rosbag stop 失败，manifest 使用 `status: "failed"`，记录 per-sensor /
   rosbag command result，并停止系统。

### `q`: stop all

1. 若正在 `FINALIZING`，等待保存和自动对齐流程结束后再退出。
2. 若正在采集或暂停，进入 active-demo abort：并发停止 rosbag recording，向
   FT300S/XenseTacSensor 发送 `STOP_REQ`，写入 `status: "failed"` manifest，
   保存已有主控侧 `.npz`，记录 `failure_stage`、`failure_reason`、per-operation
   command result、`frame_counts` 和已有 `sensor_paths`。`STOP_REQ` ACK 中的
   `saved_file` 是 best-effort optional 字段；缺失时对应 `sensor_paths` 为 `None`，
   不阻止 manifest 写入。
3. active-demo abort 不发送 `DEMO_DONE_REQ` / `DEMO_DISCARD_REQ`，也不运行自动
   timestamp alignment；`status: "discarded"` 仍只表示用户 `x` 成功完成。
4. 关闭 ZMQ、ROS node、子进程。

### asynchronous fatal handling

ZMQ receiver fatal、RealSense metadata fatal、UDS 非命令期断连、required subprocess
unexpected exit 发生在 active demo 时，与 `q` 使用同一 active-demo abort 保存策略，
但状态路径为 `ERROR -> STOPPING -> STOPPED`。无 active demo 时直接进入
`ERROR -> STOPPING -> STOPPED` 并清理资源。RealSense launch fatal 只在自动暂停成功
后重启；若 `pause_demo()` 失败或主控已处于 `ERROR` / `STOPPING` / `STOPPED`，不得再
重启相机进程。

## Timestamp Alignment

- MainController 内部需要新增 `main_controller/timestamp_alignment.py`，作为 `d` 收尾流程中的自动对齐模块；该模块只生成对齐配置、索引和报告，不生成 `aligned_numeric.npz` 等实际训练数据文件。
- `tools/align_demo_timestamps.py` 是相似但独立的命令行对齐工具，不跨目录 import 主控对齐模块；允许从主控版本复制后按 CLI 场景修改。
- 主控和独立 CLI 均支持 `--alignment-base-source realsense|xense`。默认 `realsense`；选择 `xense` 时等价于 `--base xense:0`，使用 `timestamp_ns_0` 作为目标时间轴。手动 CLI 的 `--base realsense:<topic>|xense:0|robot|grid` 优先级高于 base source。
- Xense 两个触觉传感器在对齐索引中独立输出为 `xense_0_*` 和 `xense_1_*`；不生成两路固定偏差统计。
- FT300S 在报告中按型号名显示为 `FT300S`，输出字段使用 `ft300s_*`，其中 `S` 不作为复数理解。
- materialize 实际数据集只作为未来扩展提及；在确认数据集具体组织格式前，不规划本轮实现 `tools/materialize_aligned_data.py`。
- 详细设计以 `timestamp_alignment_plan.md` 为准；主计划只保留关键约束。
- 所有来源的原始时间戳必须分别保存，用于后处理对齐；后处理不得通过平移原始时间戳来掩盖启动阶段不齐。
- 第一版对齐阶段使用主控保存的 `.npz` 时间戳索引、ZMQ 数据和 RealSense rosbag image header / metadata header；FT300S/XenseTacSensor 的 `saved_file` 路径只写入 alignment source manifest，完整 `.npy` 内容读取和主控索引交叉校验列为后续增强。
- 对于 XenseTacSensor 和 FT300S，必须等待 UDS `DEMO_DONE_REQ` ACK，并读取 ACK payload 中的 `saved_file` filename 后，再写 repo-root 相对 `sensor_paths` 并启动自动对齐；`saved_file` 不是任意路径 channel。
- RealSense 对齐优先使用 rosbag image `header.stamp` 或 metadata `header_stamp_ns`；`frame_timestamp_ns` 必须结合 `clock_domain` 判断，`HARDWARE_CLOCK` 需要按 topic 检查稳定 offset / 漂移，`SYSTEM_TIME` 或 `GLOBAL_TIME` 才期望接近 header time。
- 对齐工具应提供 `--start-trim-s` 和可重复的 `--stream-start-trim <stream>=<seconds>`，用于裁掉启动暖机段；这些参数只裁剪样本，不修改原始时间戳。
- 默认只对 `manifest.status == "done"` 的 demo 自动生成对齐索引和报告；`failed` 和 `discarded` manifest 只用于诊断，除非独立 CLI 显式启用 degraded / index-only 模式。
- 采集 `status` 和 `manifest.alignment.status` 是两个独立状态；对齐失败不得改写采集 `done` / `failed` / `discarded` 语义。

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
  - mock 环境下重点覆盖正常连续 demo 流程：`s -> d -> s -> d` 和 `s -> x -> s -> d`；验证 `d` 后自动对齐结束前不能进入下一次采集，`d/x` 后 ZMQ 仍持续 drain，且 demo 间数据不污染下一段或上一段 demo buffer。
  - 覆盖自动对齐成功和失败路径：成功时写入 `manifest.alignment.status = "done"`，失败时只写 `manifest.alignment.status = "failed"`，不改写采集 `status`。
  - 验证 `tools/align_demo_timestamps.py` 可独立重跑对齐，且不跨目录 import `main_controller/timestamp_alignment.py`。
  - 真实 FT300S/XenseTacSensor 下执行 `s -> p -> s -> d -> q`、`s -> d -> s -> d -> q`、`s -> x -> s -> d -> q`，检查 manifest、传感器 `.npy`、主控 `.npz`、rosbag、告警统计齐全。

## Assumptions

- 默认 ZMQ endpoint 为 `tcp://127.0.0.1:6000`。
- ZMQ telemetry 是必须模块，不能通过普通参数关闭。
- RealSense metadata topic 对当前启用相机可用。
- RealSense metadata JSON 中的 `clock_domain` 保存到 `realsense_metadata.npz`；缺字段按 warning 处理。
- 主控负责启动、控制、采集索引、丢帧监控、manifest 和自动对齐索引/报告；实际训练数据集 materialize 需等数据集具体组织格式确认后另行实现。
