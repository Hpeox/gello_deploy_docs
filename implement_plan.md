# MainController 具体实现计划

## 总体路线

`main_controller` 做成 ROS2 `ament_python` 包，但主体按普通 Python 控制程序设计。通过 `ros2 run main_controller main_controller` 启动，内部使用线程、队列、子进程管理、UDS socket、ZMQ 和少量 rclpy service/subscriber。

主控不持续订阅 RealSense `image_raw`。实时监控依赖 `/camX/camera/color/metadata` 和 `/camX/camera/depth/metadata`；开始或恢复录制前会短暂订阅 required image topics 做 readiness baseline，rosbag2 仍负责记录 image topic。ZMQ receiver 必须从主控启动后一直读取，直到主控退出。后续实现中，`d` 的 `FINALIZING` 流程还需要在采集保存后自动调用主控内部时间戳对齐模块，生成对齐配置、索引和报告。

## 当前已实现内容

截至当前实现轮次，MainController 已完成可编译、可单元测试和 mock runtime 覆盖的主控实现；尚未做真实 FT300S、XenseTacSensor、RealSense 和 rosbag2 recorder 全链路硬件联调。

### 已落地模块

- 包入口：`setup.py` 已新增 `main_controller = main_controller.main:main`。
- 依赖声明：`package.xml` 已补充 `rosbag2_interfaces`、`realsense2_camera_msgs`、`python3-zmq`、`python3-numpy`。
- `config.py`：集中定义默认路径、ZMQ endpoint、UDS socket、频率阈值、RealSense metadata topic、required image topic、formal/debug_degraded capture mode、rosbag count-skew threshold 和 fatal error pattern。
- `main.py`：实现 CLI、命令队列、主状态机、demo start/pause/done/discard/stop 流程，以及 RealSense fatal error 自动暂停和重启路径。
- `uds_client.py`：实现 FT300S/XenseTacSensor 共用 UDS client，包含协议 pack/unpack、自动连接、后台接收、`INIT_READY` 等待、ACK 等待、断连唤醒 pending ACK waiter 和可配置 flush timeout。
- `zmq_telemetry.py`：在 MainController 内独立实现 ZMQ 504-byte telemetry frame 解包和 always-on receiver，不 import `Zmq_Ref`。
- `realsense_metadata.py`：实现 RealSense metadata 订阅与 JSON 解析，输出 `frame_number`、`header.stamp`、`frame_timestamp`、`hw_timestamp` 等轻量 timing event，不订阅 `image_raw`；当前尚未把 metadata JSON 中的 `clock_domain` 保存到 `realsense_metadata.npz`，后续需要补充。
- `realsense_image_guard.py`：定义 RealSense image topic baseline、readiness result 和 rosbag metadata post-check。
- `rosbag_control.py`：封装 `/rosbag2_recorder/record`、`/resume`、`/pause`、`/stop` service call，并提供 image readiness / recorded metadata validation 入口。
- `processes.py`：实现子进程启动、日志捕获、fatal pattern 检测、SIGINT/SIGTERM/SIGKILL 停止和重启。
- `drop_monitor.py`：实现 frame/seq/frame_number 连续性和帧间隔告警检测。
- `buffers.py`：实现 controller JSONL log、demo `.npz` buffer 和 manifest 写入。

### 已实现业务语义

- ZMQ receiver 从启动后持续 drain socket；demo 外数据不写入 demo buffer，但不会停读，包括 `d/x` 后回到 `WAIT_START` 的 demo 间隔。
- 主控启动时等待 ZMQ 首个合法 frame、等待 FT300S/XenseTacSensor UDS 连接和 `INIT_READY`。
- `s`：创建或恢复 demo，发送两个传感器 `START_REQ`，先验证 required RealSense image topics readiness，首次 segment 调用 rosbag2 `record`，随后调用 `resume`。
- `p`：发送两个传感器 `PAUSE_REQ`，调用 rosbag2 `pause`，不再用 `stop` 暂停；若任一 required sensor pause 失败，写 `failed` manifest、记录 per-sensor command result，并进入 `ERROR -> STOPPING -> STOPPED`。
- `d`：进入 `FINALIZING`，发送 `DEMO_DONE_REQ`，默认在有限 `sensor_flush_timeout_s` 内等待 ACK 和 `saved_file`，期间周期性写进度日志；显式配置 `none` / `unbounded` 时允许无界等待，这是操作者为超长 sensor flush 保留数据而接受的预期模式。随后 stop rosbag，使用实际 rosbag URI 做 required image topic metadata post-check，并保存 `.npz`/manifest。只有 required sensors finished、rosbag stop 成功且 required post-check 通过时 status 才为 `done`；sensor finish、有限 flush timeout、UDS disconnect、rosbag stop 或 post-check 失败时 status 为 `failed`，并记录 command result 或 post-check result。
- `x`：发送 `DEMO_DISCARD_REQ`，stop rosbag，写 lightweight discarded manifest，然后丢弃当前 demo buffer；成功 discard 不保存高频 `.npz`，manifest 中 `npz` 为空并包含 `frame_counts`。只有用户 discard transaction 成功完成时 status 才为 `discarded`，sensor discard 或 rosbag stop 失败时 status 为 `failed`。
- `q`：停止 rosbag、传感器、ZMQ、RealSense metadata monitor 和子进程。
- RealSense stdout/stderr 中检测到 `Hardware Error` 或 `Depth stream start failure` 时，会向主控命令队列投递 fatal event；若当前为 `COLLECTING`，先自动暂停，再重启 RealSense camera launch。

### 已验证内容

- 系统 Python 导入检查通过：`PYTHONPATH=MainController/src/main_controller /usr/bin/python3 ...` 能导入 MainController 核心模块。
- `zmq` 在 `/usr/bin/python3` 下可用，版本为 `24.0.1`；默认 shell 的 conda Python 3.13 仍看不到 apt 安装的 `python3-zmq`。
- 编译检查通过：`/usr/bin/python3 -m compileall MainController/src/main_controller/main_controller`。
- 单元测试 `test_maincontroller_core.py` 通过，当前结果为 `9 passed`。
- mock runtime 集成测试 `test_maincontroller_mock_runtime.py` 在非 sandbox 环境下通过，当前结果为 `27 passed`。普通 sandbox 会因 Unix domain socket `bind` 权限限制失败，不能代表业务断言失败。
- ROS service 接口字段已确认：`Record.Request` 字段为 `uri`，`Pause/Resume/Stop.Request` 均为空请求。

### 尚未完成/需要联调

- 尚未在真实 FT300S、XenseTacSensor、RealSense 和 rosbag2 recorder 全链路上运行 `main_controller`。
- 尚未验证 conda 启动命令在当前 shell/ROS2 启动方式下是否需要额外环境清理。
- 尚未验证 `ros2 run main_controller main_controller` 的 colcon build/install 流程。
- 尚未做真实 metadata topic 与当前启用相机列表的运行时发现；目前 topic 来自配置默认值。
- 尚未把 RealSense metadata JSON 中的 `clock_domain` 写入主控 `realsense_metadata.npz`；后处理对齐阶段在该字段落盘前只能把 `frame_timestamp_ns/hw_timestamp_ns` 作为诊断字段。
- 尚未实现 `main_controller/timestamp_alignment.py` 主控内部自动对齐模块；该模块应只生成 `alignment_config.json`、`aligned_index.npz`、`aligned_manifest.json` 和 `alignment_report.md`，不生成实际训练数据文件。
- 尚未实现 `tools/align_demo_timestamps.py` 独立 CLI 对齐工具；该工具可以与主控内部模块保持相似逻辑，但不能跨目录 import `main_controller.timestamp_alignment`。
- materialize 实际数据集暂不列为本轮待实现模块；需要先在 `timestamp_alignment_plan.md` 中保留 TODO，等待确认数据集具体组织格式。
- 尚未做真实 RealSense fatal log 注入到子进程 stdout/stderr 的端到端测试。
- 尚未做真实 demo 采集输出目录、`.npz` 内容和 manifest 的完整验收。

## 技术选择

- 并发模型：
  - 主线程运行控制状态机，串行处理 `s/p/d/x/q`。
  - `InputThread` 阻塞读取用户输入，只把命令放入队列，不执行业务逻辑。
  - `UdsClientThread` 两个，分别处理 FT300S 和 XenseTacSensor 的 UDS 收发。
  - `ZmqReceiverThread` 常驻运行，从启动到退出持续读取跨主机 ZMQ。
  - `ProcessMonitorThread` 监控子进程 stdout/stderr 和退出状态。
  - rclpy 用于 rosbag2 service 调用和 RealSense metadata 订阅。
- GIL 处理：
  - UDS、ZMQ poll、stdin、subprocess stdout 都是阻塞 I/O，线程足够。
  - 不订阅 `image_raw`，避免大图像反序列化抢 GIL 和 CPU。
  - RealSense metadata 消息很小，适合在主控进程内订阅并解析。
- RealSense 时间戳：
  - 使用 `/camX/camera/color/metadata` 和 `/camX/camera/depth/metadata`。
  - 实时监控用 metadata 的 `frame_number`、`header.stamp`、`frame_timestamp`、`hw_timestamp`。
  - 跨源对齐以 metadata `header_stamp_ns` 或 rosbag image `header.stamp` 为主时间；`frame_timestamp_ns` 必须结合 `clock_domain` 解释，`hw_timestamp_ns` 仅作诊断。
  - rosbag 仍记录 image topic；离线可继续用 `tools/realsense_bag_compare.py` 验证 metadata header 与 image header 的对应关系。

## 实现模块

- `main.py`：CLI、启动入口、主状态机。
- `config.py`：默认参数、topic 列表、频率阈值、路径配置。
- `uds_client.py`：共用 UDS 客户端，支持自动连接、ACK 等待、FRAME_READY 接收。
- `zmq_telemetry.py`：在 MainController 内独立实现 504-byte telemetry unpack，不 import `Zmq_Ref`。
- `realsense_metadata.py`：订阅 metadata topic，解析 JSON，输出 frame timing 事件。
- `drop_monitor.py`：统一检测 frame_id/seq/frame_number 不连续和帧间隔异常。
- `buffers.py`：管理 demo buffer，完成后保存 `.npz`。
- `processes.py`：启动/停止/重启 FT300S、XenseTacSensor、RealSense launch。
- `rosbag_control.py`：封装 `/rosbag2_recorder/record`、`/resume`、`/pause`、`/stop`。
- `timestamp_alignment.py`：待新增；作为 MainController 内部自动对齐模块，在 `FINALIZING` 中生成对齐配置、索引和报告。

`tools/align_demo_timestamps.py` 是待新增的独立命令行对齐工具，不属于 `main_controller` 包内部模块；它不跨目录 import `main_controller.timestamp_alignment`，可以从主控内部实现复制后按 CLI 需求演化。

## 状态机

### 状态定义

- `BOOT`：主控进程刚启动，尚未启动依赖服务。
- `STARTING_SERVICES`：正在启动 FT300S、XenseTacSensor、RealSense camera launch、rosbag2 recorder、ZMQ receiver。
- `INIT`：依赖服务已启动，正在连接 UDS、等待 ZMQ 首帧、初始化传感器。
- `WAIT_START`：全部就绪，等待用户输入 `s` 开始 demo。
- `COLLECTING`：正在采集 demo，UDS/ZMQ/RealSense metadata buffers 正在写入。
- `PAUSED`：当前 demo 暂停，ZMQ 仍持续读取但不写入 demo buffer；恢复后重置 drop baseline。
- `FINALIZING`：正在完成 demo，等待传感器 flush ACK、停止 rosbag、保存 `.npz` 和 manifest，并调用主控内部自动对齐模块生成配置、索引和报告。
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
- RealSense 自动重启期间，如果主控处于 `COLLECTING`，必须先迁移到 `PAUSED`。

## 运行流程

### 启动阶段

1. 创建 `runtime_sessions/session_<timestamp>/`。
2. 启动 FT300S、XenseTacSensor、RealSense camera launch、rosbag2 recorder launch。
3. 启动 ZMQ receiver，默认连接 `tcp://127.0.0.1:6000`。
4. ZMQ 必须收到首个合法 frame 后，主控才继续进入就绪流程。
5. 连接两个 UDS 服务并自动完成 `INIT_REQ/INIT_READY`。
6. 启动 RealSense metadata 订阅。

### `s`: start/resume demo

1. 创建或恢复当前 demo。
2. 向 FT300S/XenseTacSensor 发送 `START_REQ` 并等待 ACK。
3. 如果是新 recording segment，先调用 `/rosbag2_recorder/record` 指定 demo bag URI。
4. 调用 `/rosbag2_recorder/resume`。注意：rosbag2 使用 `record` 服务后还需要调用一次 `resume`，恢复暂停后也同样调用 `resume`。
5. 开启 UDS/ZMQ/RealSense metadata demo buffer。

### `p`: pause demo

1. 向 FT300S/XenseTacSensor 发送 `PAUSE_REQ`。
2. 调用 `/rosbag2_recorder/pause`，不要调用 `stop`。
3. 若任一 sensor `PAUSE_REQ` 或 rosbag `pause` 失败，写 `status: "failed"` manifest，
   记录 `ft300`、`xense` 和 `rosbag_pause` command result，并进入
   `ERROR -> STOPPING -> STOPPED`。
4. 暂停 demo buffer，但 ZMQ receiver 继续读消息。
5. 恢复后重置 drop monitor baseline。

### `d`: finish and save demo

1. 发送 `DEMO_DONE_REQ`。
2. 进入 `FINALIZING`，等待传感器 ACK 和 `saved_file`。
3. 传感器 flush 使用可配置 `sensor_flush_timeout_s`，并周期性打印进度和写 log；
   默认有限超时；显式配置为 `none` / `unbounded` 时才允许无界等待。
   无界等待是预期的操作者模式，不生成 `ack_timeout`，只由 ACK、对应 sensor
   `ERROR`、UDS disconnect 或主控停止唤醒。
4. 调用 `/rosbag2_recorder/stop` 停止当前 recording。
5. 若 rosbag `stop` 失败，跳过 RealSense rosbag post-check，写 `status: "failed"`
   manifest，保留可用 sensor `saved_file` 和 controller `.npz`，并进入
   `ERROR -> STOPPING -> STOPPED`。
6. 保存 `.npz` 和 `manifest.json`。
7. 若采集 `status` 为 `done`，调用 `main_controller/timestamp_alignment.py` 自动生成 `<demo_dir>/aligned/alignment_config.json`、`aligned_index.npz`、`aligned_manifest.json` 和 `alignment_report.md`。
8. 将自动对齐结果写入 `manifest.alignment`。自动对齐失败只设置 `manifest.alignment.status = "failed"` 和错误详情，不改写采集 `status`。

### `x`: discard demo

1. 发送 `DEMO_DISCARD_REQ`。
2. 调用 `/rosbag2_recorder/stop` 停止当前 recording。
3. 丢弃本次 demo buffer，只保留 controller log。

### `q`: stop all

1. 安全停止当前 demo/recording。
2. 向 FT300S/XenseTacSensor 发送 `STOP_REQ`。
3. 关闭 ZMQ、rclpy、子进程。

## ZMQ 特别规则

ZMQ receiver 从主控启动后一直运行，直到主控退出。即使主控处于 `WAIT_START`、`PAUSED`、`FINALIZING`、`DISCARDING`，也必须持续读取 socket，避免跨主机对端队列堆积或溢出。`d/x` 完成后回到 `WAIT_START` 的 demo 间隙同样不能停读。

demo 之外的数据进入环形缓冲或直接丢弃，但不能停止读。demo 内数据写入当前 demo buffer，并同步做 drop monitor。

## RealSense 节点监控和重启

- `ProcessMonitorThread` 持续读取 RealSense launch stdout/stderr。
- 捕捉到以下字符串时触发重启：
  - `Hardware Error`
  - `Depth stream start failure`
- 若当前状态为 `COLLECTING`，先自动执行暂停流程并进入 `PAUSED`：
  - 向 FT300S/XenseTacSensor 发送 `PAUSE_REQ`。
  - 调用 `/rosbag2_recorder/pause` 暂停当前 recording。
  - 暂停 demo buffer。
  - 重置 RealSense metadata drop baseline。
- 写终端和 `controller_events.jsonl`：错误内容、触发时间、自动暂停结果。
- 对 RealSense camera launch 进程组发送 SIGINT，等待短暂 grace period。
- 未退出再 SIGTERM/SIGKILL。
- 用原命令重新启动 launch。
- 重启完成后记录 restart event，并在 manifest 中累计重启次数。
- 自动重启不会自动恢复采集；用户确认设备恢复后再输入 `s` 继续。

## 丢帧监控

- FT300S：
  - 检查 UDS `frame_id` 连续。
  - 检查 `timestamp_ns` 间隔，默认 100 Hz，超过 20 ms 告警。
- XenseTacSensor：
  - 检查 UDS `frame_id` 连续。
  - 检查 `timestamp_ns_0/timestamp_ns_1`，默认 30 Hz，超过 66.7 ms 告警。
- ZMQ：
  - 按 source 独立检查 `seq` 连续。
  - 默认 50 Hz，超过 40 ms 告警。
- RealSense：
  - 按 metadata stream 独立检查 `frame_number` 连续。
  - 默认 30 Hz，超过 66.7 ms 告警。
- 每个正向 key 不连续事件发出一个 `drop_warning`，同时输出到终端和
  `controller_events.jsonl`。
- `missing_frame_count` 按正向 key gap 累计，`warning_count` 按实际发出的 warning
  数累计。
- large timestamp interval 是独立的 `drop_warning` reason，可与 non-contiguous key
  warning 同时出现。
- 每个 demo manifest 记录各 stream monitor summary，包括 `warning_count`、
  `missing_frame_count` 和 `max_interval_ns`。这些统计仅供采集后 operator review，不触发
  自动 pause/abort。

## 数据保存

- 低频控制日志：`controller_events.jsonl`。
- 高频数据：`.npz`，默认不压缩，避免 demo 完成时 CPU 压缩阻塞。
  - `ft300_timestamps.npz`
  - `xense_timestamps.npz`
  - `realsense_metadata.npz`
  - `zmq_telemetry.npz`
- `manifest.json`：
  - demo 起止时间。
  - rosbag URI/segment。
  - FT300S/XenseTacSensor `saved_file`。
  - 各 `.npz` 路径和 `frame_counts`。discarded /部分 failed manifest 可以不保存高频 `.npz`，但仍记录 buffer 清空前的 frame count summary。
  - 丢帧告警统计。
  - RealSense 重启次数和时间点。
  - RealSense image readiness baseline 和 rosbag image metadata post-check 结果。
  - start/resume 事务失败时，写轻量 failed manifest，记录 `failure_stage`、
    `failure_reason`、已 ACK `START_REQ` 的 sensor、rollback target sensor、
    `DEMO_DISCARD_REQ` rollback result，以及 rosbag record/resume 状态；不保存高频 `.npz`。
  - 自动对齐结果写入独立 `alignment` 字段；采集 `status` 只描述 `done` / `discarded` / `failed` 采集事务。

时间对齐输入约束：

- MainController 内部自动对齐只生成配置、索引和报告，不生成 `aligned_numeric.npz` 等实际训练数据文件。
- 自动对齐输出目录为 `<demo_dir>/aligned/`，默认包含 `alignment_config.json`、`aligned_index.npz`、`aligned_manifest.json` 和 `alignment_report.md`。
- `tools/align_demo_timestamps.py` 是相似但独立的 CLI 对齐工具，不跨目录 import 主控模块；可用于重跑对齐、调参或显式 degraded / index-only 诊断。
- materialize 实际数据集暂不实现；需要先确认数据集具体组织格式。
- 完整传感器 `.npy` 文件位于仓库根目录 `./runtime_frames/<saved_file>`；若 ACK payload 中的 `saved_file` 是绝对路径则直接使用，否则拼接为 `repo_root / "runtime_frames" / saved_file`。
- 默认只对 `manifest.status == "done"` 的 demo 自动生成 aligned index/report；`failed` 和 `discarded` manifest 只用于诊断，除非后处理工具显式启用 degraded / index-only 模式。
- RealSense 后处理以 manifest 中的 formal/debug_degraded required image topic list 为权威来源；不硬编码具体相机数量。
- 启动暖机裁剪使用 `--start-trim-s` 和可重复的 `--stream-start-trim <stream>=<seconds>`；这些参数只裁剪样本，不平移原始时间戳。
- `timestamp_alignment_plan.md` 是详细对齐规范；其中的 `clock_domain`、`HARDWARE_CLOCK`、`SYSTEM_TIME`、`GLOBAL_TIME` 规则应作为后续对齐工具实现依据。

MainController 负责多传感器 start/resume 事务协调。FT300S 和 XenseTacSensor
必须全部 ACK `START_REQ`，且 rosbag `record` / `resume` 必须成功，demo 才能进入
`COLLECTING`。若新 demo start 的后续 sensor 或 rosbag 步骤失败，MainController
对已 ACK start 的 sensor 发送 `DEMO_DISCARD_REQ` 回滚。若 paused resume 失败，
rollback target 是所有已经持有 paused demo context 的 required sensor，不限于本次
resume 已 ACK `START_REQ` 的 sensor。rollback 全部确认后清空当前 demo context 并回到
`WAIT_START`；若任一 rollback target 无法确认 discard，或 rosbag stop cleanup 失败，
则写入 `rollback_unconfirmed_sensors` 并进入 `ERROR -> STOPPING -> STOPPED`。所有
start/resume 事务失败的 manifest 状态均为 `failed`。`discarded` 仅用于用户 `x`
命令成功完成；start/resume 事务失败即使用 discard 命令回滚 sensor，也不是用户放弃。

非 start/resume 的 command transaction 也由 MainController 统一记录结果：`PAUSE_REQ`、
rosbag `pause`、`DEMO_DONE_REQ`、finish-time rosbag `stop`、用户 `DEMO_DISCARD_REQ`
任一 required operation 返回 `ERROR`、超时或抛错时，failed manifest 记录
`failure_stage`、`failure_reason` 和 per-operation command result。pause/discard/failed
finish 会进入 `ERROR -> STOPPING -> STOPPED`，避免主控状态与物理 sensor 状态不一致。
`done` 表示 required sensors finished、rosbag stopped successfully 且 required post-checks
passed；`discarded` 表示用户 discard 成功完成；`failed` 表示系统或 command transaction
未成功。时间戳对齐结果不复用采集 `status`，而是写入 `manifest.alignment.status`。

RealSense image topic list 是正式采集的权威 required list。formal 模式默认要求
`cam1` 到 `cam4` 的 color `image_raw` 和 `aligned_depth_to_color/image_raw` 共 8 个
topic。`debug_degraded` 模式必须显式配置 topic 子集，且该子集必须来自 formal baseline。
pre-record readiness 记录 topic、message type、width、height、encoding、step 和 stream
role；缺失或 schema mismatch 会阻止录制并写 failed manifest。post-record check 从当前
demo 的实际 rosbag URI 读取 metadata，验证 required topics 存在、类型匹配、count 非零，
并检查 count skew 不超过配置阈值。metadata topics 只负责实时监控，image topics 负责
readiness 和 rosbag 记录校验；精确 fps/rate 验证留作后续扩展。

## 测试计划

- 单元测试：
  - UDS mock server。
  - ZMQ 504-byte unpack。
  - RealSense metadata JSON parser。
  - DropMonitor 连续帧、跳号、超间隔、暂停恢复 baseline。
  - `.npz` 字段长度一致性。
- 集成测试：
  - ZMQ 在 pause/finalizing 时持续发送，验证主控仍持续 drain。
  - 正常连续 demo 流程 `s -> d -> s -> d`，验证每段 demo 独立保存，且 `d` 后自动对齐结束前不能开始下一次采集，`WAIT_START` 期间 ZMQ 持续 drain。
  - 丢弃后继续采集流程 `s -> x -> s -> d`，验证 discard 写入 lightweight `status: "discarded"` manifest、不保存高频 `.npz`，且 `x` 后 `WAIT_START` 期间 ZMQ 持续 drain。
  - 自动对齐成功路径只生成 `alignment_config.json`、`aligned_index.npz`、`aligned_manifest.json` 和 `alignment_report.md`，并写入 `manifest.alignment.status = "succeeded"`。
  - 自动对齐失败路径只写入 `manifest.alignment.status = "failed"` 和错误详情，不改写采集 `status`。
  - `tools/align_demo_timestamps.py` 可独立重跑对齐，且不跨目录 import `main_controller.timestamp_alignment`。
  - 注入 `Hardware Error` 和 `Depth stream start failure` 日志，验证 RealSense 自动暂停和重启。
  - 执行 `s -> p -> s -> d -> q`，检查 `.npz`、manifest、controller log。
  - 在正确 ROS2 Python 环境下运行 `tools/realsense_bag_compare.py`，复核 metadata 与 image header 时间戳一致性。

## 假设

- RealSense metadata topic 对当前启用相机可用。
- 第一版主控自动对齐只生成对齐配置、索引和报告；materialized 训练数据集待数据集具体组织格式确认后另行规划。
- 传感器 flush 时间不固定，默认使用有限 `sensor_flush_timeout_s`，同时保留进度
  watchdog；只有显式配置 `none` / `unbounded` 时才允许无界等待。无界等待用于
  现场确实可能超长 flush 的传感器，是操作者接受等待风险后的预期配置，不应在
  follow-up audit 中单独视为 P0/P1 缺陷。
- mock / non-hardware tests cover formal-mode required topic enforcement and
  fail-closed behavior, but physical four RealSense availability remains a
  hardware acceptance item. Final acceptance requires a real formal capture with
  `cam1` to `cam4` color and aligned-depth image topics ready, recorded, and
  passing post-record metadata validation.
