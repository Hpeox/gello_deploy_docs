# FR3 SensorHub 运行期容错与关节复位握手计划

## 概要

保持单一持久 SensorHub 子进程、持久 ZMQ telemetry `SUB`、连续 bounded caches 和 session-level aligned SHM。运行期任何数据缺失或无法对齐都统一执行：

`CausalAligner.select() -> None -> retry`

SensorHub 不判断缺失原因，也不通过 stall/alignment timeout 将其升级为 fatal。启动阶段仍严格要求所有数据源推进并产生首个有效 aligned snapshot。

新增 FR3 内部 `_reset_joints(q_reset)`，通过 FRCMD1 `RESET_JOINT` 和 FGT1 `RobotTelemetry.RESETTING` 完成同步 reset 握手；`RESETTING` 仅属于 telemetry/runtime control state，不进入 `RobotSample` 或 aligned data。

## 按文件实施

### `sensorhub/runtime.py`

- `_alignment_loop()`：
  - 每轮始终调用 `CausalAligner.select()`。
  - 返回 sample 时正常 publish；返回 `None` 时短暂 sleep 后重试。
  - 删除 `last_success_ns`、连续 alignment-failure 计时及任何 pre-alignment freshness gate。
  - 捕获 aligner/publisher 的未预期异常并调用 `_fatal()`。

- `_supervise()`：
  - 删除 camera、Xense、FT300S、robot、gripper 的 runtime stale 检查。
  - 只保留当前 parent lifecycle 行为：父进程消失时设置 `stop_event` 并结束 SensorHub，不改为 `FATAL`。
  - 不尝试解释 alignment miss，也不增加替代 stall timeout。

- Reader threads：
  - `TimeoutError` 表示暂时没有新数据，只重试。
  - parser/ABI/SHM read、ZMQ、内部 invariant 等非 timeout 异常继续调用 `_fatal()`。
  - robot telemetry 到达时，q/dq/tau 仍转换为原有 `RobotSample` 并进入 alignment cache；`RESETTING` 单独更新 runtime control state，并通过 UDS channel发布。
  - 不清 cache、不重建 reader、不重启 SensorHub；`required_sample_max_age_ms` 继续由 aligner排除旧数据。

- `_fatal()`：
  - 将 `_fatal_lock: Event` 改为真正的 `Lock`，保证并发错误只发布一次 fatal。
  - 即使写 fatal SHM 状态或发送 UDS fatal 失败，也在 `finally` 中设置 `stop_event`。
  - parent disappearance沿用独立的非 fatal shutdown 路径。

### `config_fr3.py` 与 `SensorHubConfig`

- 删除：
  - `alignment_failure_timeout_ms`
  - `camera_xense_stall_timeout_ms`
  - `ft_robot_gripper_stall_timeout_ms`
- 不增加替代 source-stall 配置。
- 保留 `required_sample_max_age_ms` 和 `cache_horizon_s`。
- 新增 workstation reset 配置：
  - `reset_ack_timeout_s: float = 2.0`
  - `reset_completion_timeout_s: float = 30.0`
  - `reset_retry_interval_s: float = 0.1`
- 三项 reset 配置必须为有限正数；只供 `FR3` 使用，不传给 SensorHub CLI。

### `protocols.py`、`readers.py`

- FRCMD1：
  - 定义 `COMMAND_FLAG_RESET_JOINT = 1 << 0`。
  - `pack_command()` 增加 keyword-only `flags=0`，仅允许已知 bit。
  - 保持 ABI version、header、112-byte frame、7-joint payload 和普通 action格式不变。
  - reset retransmission复用第一次 pack 得到的完全相同 frame，因此 command sequence和 timestamps均不变化。

- FGT1：
  - 定义 robot telemetry bit 0 为 `ROBOT_TELEMETRY_FLAG_RESETTING`。
  - `parse_telemetry()` 从现有 16-bit flags字段提取该 bit，不改变 504-byte frame或 telemetry ABI version。
  - 仅 `RobotTelemetry` 增加 `resetting: bool`。
  - `RobotSample`、`AlignedSample`、aligned slot/header/payload均不增加该字段。
  - gripper telemetry不使用该 flag；未知 telemetry bits继续忽略。

- `TelemetryReader`：
  - 将 `RobotTelemetry` 转换为原有 `RobotSample` 时，单独保存刚解析出的 `resetting` 状态，供同一 SensorHub telemetry thread读取。
  - runtime在 append `RobotSample` 后同步更新 control-only reset state。
  - 不通过 observation cache或 aligned SHM传递 `RESETTING`。

### `sensorhub/uds.py`

- 将 UDS `PROTOCOL_VERSION` 从 1 bump 到 2；v1 packet不再作为兼容消息接受。
- 增加内部消息：
  - `GET_ROBOT_RESETTING`
  - `ROBOT_RESETTING`
- `ROBOT_RESETTING.status_code`：
  - `0`：当前为 `RESETTING=0`
  - `1`：当前为 `RESETTING=1`
  - `2`：尚无可用 robot telemetry state
- SensorHub在状态变化时主动发布 `ROBOT_RESETTING`，同时响应 FR3 的显式查询。
- 明确 packet `sequence` 为方向内独立序列：
  - FR3→SensorHub request使用 FR3 的 `_uds_sequence`。
  - SensorHub→FR3 status/response继续使用 `UDSControlServer` 自己的发送序列。
  - 两者均与 FRCMD1 command sequence无关。
- 使 `UDSControlServer.publish()` 的 sequence分配和发送受同一 lock保护，保证 query response、transition、READY/FATAL有序。
- 不扩展 aligned SHM，也不创建第二个 telemetry `SUB`。

### `fr3.py`

- 在 `__init__()` 中新增：
  - `_uds_sequence = 0`
  - reset-state event queue
  - 私有 command lock
- 增加统一的 `_send_uds_request(...)`：
  - 每发一个 `GET_ROBOT_RESETTING`、`SHUTDOWN` 或未来的 FR3内部 control request时，将 `_uds_sequence` 加一。
  - 使用该值构造 UDS packet。
  - 不读取、修改或预留 FRCMD1 `_command_sequence`。
- 修改 `disconnect()`，不再使用 `_command_sequence + 1` 构造 `SHUTDOWN`；改由 `_send_uds_request("SHUTDOWN", ...)` 分配独立 UDS request sequence。

- 新增单下划线内部 API：

  ```python
  FR3._reset_joints(q_reset) -> None
  ```

- `q_reset` 按 joint1…joint7顺序接受恰好 7 个有限、非 bool数值。
- 调用必须位于 rollout `INITIALIZE` phase boundary；普通 policy action publication应已停止。私有 command lock使整段 handshake与 `send_action()` 互斥。
- 开始前：
  - 调用 `_check_health()` 并分派已有 UDS v2 packets。
  - 通过 `_send_uds_request("GET_ROBOT_RESETTING")` 查询当前状态；必须确认 `0`。若已为 `1`，拒绝开始新的 reset。
- 请求与 acknowledgement：
  - FRCMD1 `_command_sequence` 只递增一次。
  - 只 pack 一次带 `RESET_JOINT` flag的 frame；joint payload为 `q_reset`，`gripper_gPO=0` 并由 NUC忽略。
  - 立即发送，并按 `reset_retry_interval_s` 重发同一 bytes和同一 FRCMD1 sequence。
  - 每次 `GET_ROBOT_RESETTING` 都使用递增且独立的 `_uds_sequence`。
  - 收到 `RESETTING=1` 后停止重发；超过 `reset_ack_timeout_s` 抛出 `TimeoutError`。
- Completion：
  - acknowledgement后只等待 `RESETTING=0`，期间继续使用新 UDS request sequence查询并检查 SensorHub process、FATAL和 UDS health。
  - 不再发送 reset frame。
  - 超过 `reset_completion_timeout_s` 抛出 `TimeoutError`。
  - 只有观察到 `1 -> 0` 后才成功返回。
- 重构 `_check_health()` 的 packet dispatch，使 `ROBOT_RESETTING` transition进入内部有序队列；UDS EOF/断开视为内部 IPC failure。
- 普通 `send_action()` 保持绝对 7-joint + gripper语义、校验和返回值不变。
- 不修改 `Robot`、`ThreadSafeRobot`、`get_observation()` 或当前 rollout strategy。

### NUC 协议约束

目标仓库没有 NUC receiver/torque-loop实现，因此只实现并测试 workstation侧行为。远端必须满足：

- 相同 `RESET_JOINT` FRCMD1 command sequence的重复 frame属于同一逻辑请求。
- 重复 frame不得重新开始或重新计时 reset trajectory。
- 接受请求并进入 reset motion后设置 robot FGT1 `RESETTING=1`。
- reset motion完成后设置 `RESETTING=0`。
- UDS request sequence只属于 FR3↔SensorHub control channel，不参与 NUC幂等判断。
- acknowledgement仍由现有 telemetry stream表达，不新增独立 NUC ACK transport。

## SHM 与兼容性

- `sensorhub/aligned_shm.py`、`samples.py` 的 alignment structures以及 `sensorhub/__init__.py` 不作相关修改。
- 不新增 SHM status API。
- 保持 `FR3OBS1` ABI、magic/version、header/slot大小、字段语义和生命周期不变。
- `RESETTING` 不写入 `RobotSample`、`AlignedSample`、observation dictionary或 SHM。
- `CausalAligner._publish_sequence` 在同一 SensorHub session持续递增；source outage时停止推进，恢复后继续递增。
- 未来 MainController自行读取所需 global-header字段。
- UDS protocol独立 bump至 version 2，不影响 FRCMD1、FGT1或 aligned SHM ABI。
- 删除三个旧 timeout字段后，旧配置中的对应键需要同步清理。

## 测试计划

### Protocol 与 reset handshake

- `test_protocols.py`
  - 保留普通 FRCMD1 golden frame，确认 flags为 0、ABI大小不变。
  - 验证 `RESET_JOINT` flag、未知 flags拒绝和完全相同 frame可重复发送。
  - 验证 `RobotTelemetry.resetting` 的 true/false解析；frame size/version不变。
  - 验证 `RobotSample` 没有 `resetting` 字段，alignment数据结构不受影响。
  - 验证 gripper telemetry和未知 telemetry bits不受影响。

- `test_fr3_robot.py`
  - 验证 `_reset_joints()` 输入校验和 connected/health前置条件。
  - 模拟 `RESETTING: 0 -> 1 -> 0`，验证同步返回。
  - acknowledgement前捕获多次 FRCMD1 frame，断言 bytes、command sequence和 timestamps完全相同；收到 `1` 后不再重发。
  - 验证重复的 `GET_ROBOT_RESETTING` 使用连续递增的 `_uds_sequence`，同时 FRCMD1 reset sequence保持不变。
  - 验证 `disconnect()` 的 `SHUTDOWN` 延续 `_uds_sequence`，不会占用或猜测 `_command_sequence + 1`。
  - 分别验证 acknowledgement timeout、completion timeout、SensorHub FATAL、process exit和 UDS断开。
  - 验证普通 action与 reset共用 FRCMD1 command sequence，而 UDS request sequence独立。
  - 验证 `_reset_joints()` 不进入 generic `Robot` API，以及进入时已为 `RESETTING=1` 会被拒绝。

- UDS tests
  - 更新 packet golden assertions到 `protocol_version=2`，验证 v1被拒绝。
  - 验证 `GET_ROBOT_RESETTING`/`ROBOT_RESETTING` schema及 unknown/false/true状态。
  - 分别验证 client request sequence和 server response sequence在各自方向单调，且无需相等或关联。
  - 验证快速 `0 -> 1 -> 0` transition不会因 packet drain只保留最终状态而丢失 acknowledgement。
  - 验证并发 READY/FATAL/reset-state publish仍具有唯一、单调的 server sequence。

### SensorHub runtime

- 使用 fake readers启动同一个 `SensorHubRuntime` 和同一个 aligned SHM：
  - 严格启动并等待 READY，记录 `latest_sequence`。
  - 暂停 robot/gripper telemetry，确认 runtime不 fatal、同一进程仍存活且 aligned sequence停住。
  - 恢复 telemetry，包括 source sequence从较小值重新开始，确认无需 clear cache、重建 SHM或重启 SensorHub即可继续发布。
- 参数化暂停 camera、Xense、FT300S：
  - reader仅产生 `TimeoutError` 时 runtime持续重试且不 fatal。
  - source恢复后 alignment自动恢复。
- 验证 aligner连续返回 `None` 不触发 fatal；随后返回有效 sample时恢复 publish。
- 验证 `RESETTING` transition只更新 runtime control state/UDS，不改变 cached `RobotSample` 或 aligned payload。
- 回归验证 malformed telemetry、reader/parser exception、invalid input/ABI、publisher/aligner exception和 UDS internal failure仍进入 fatal。
- 验证 parent disappearance沿用当前行为：设置 stop并终止 SensorHub，不发布 `FATAL`。
- 保留现有 aligned SHM roundtrip、stale snapshot、fatal metadata和 ABI golden tests。
- 实施后运行全部 FR3 protocol、robot、alignment/SHM、UDS和新增 runtime tests；正式验证需使用安装完整依赖的项目环境。

## 语义边界

- rollout/MainController语义：`INITIALIZE`
- FR3内部操作：`FR3._reset_joints(q_reset)`
- FRCMD1 wire flag：`RESET_JOINT`
- FGT1 robot control state：`RESETTING`
- SensorHub UDS protocol：version 2
- FRCMD1 sequence：NUC command身份及 reset幂等键
- FR3 `_uds_sequence`：FR3→SensorHub control request顺序
- UDS server sequence：SensorHub→FR3 status/response顺序
- `get_observation()` 不决定 rollout/session policy。
- 不实现 MainController、SensorHub restart、generation、cache epoch、cache clearing、source supervisor、恢复命令或新的 episode state machine。
