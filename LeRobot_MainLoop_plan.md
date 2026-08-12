# FR3 Controlled Rollout 实施计划

## 1. 当前架构、核心保证与授权边界

### 当前 LeRobot 架构

- `lerobot_rollout.py` 每个进程只构建一次 `RolloutContext`；policy、processor、robot、SensorHub 和 inference engine 可跨多轮 rollout 持续存活。
- `BaseStrategy` 提供可复用的 policy control loop；`EpisodicStrategy` 已有 episode 级 inference/interpolator reset 和标准 dataset frame 操作。本次不改变二者语义。
- `ThreadSafeRobot` 只代理 observation/action。本次不修改它；Controlled 通过 `ctx.hardware.robot_wrapper.inner` 调用 FR3-only capability。
- FR3 已有同步 `_reset_joints()`：重复发送相同的 `RESET_JOINT` frame，直至观察到 `RESETTING=1`，随后等待 `RESETTING=0`。
- `RESET_JOINT` 的正式语义包括：
  - arm 移动到指定 joint target；
  - gripper 同时执行 OPEN。
- MainController 使用新的 Controlled rollout UDS；FR3↔SensorHub 内部 UDS、aligned SHM 和 FRCMD1/FGT1 ABI 保持不变。
- `FR3.get_observation()` 的 stale/max-age exception 必须原样传播，不能转换为 ABORT。

### Controlled UDS 核心原则

```text
No deferred command execution.
```

LeRobot 对实际从 UDS 读取并作为当前 phase 输入处理的 command：

- 按当前 worker phase 立即 validation。
- 当前 phase 合法则立即接受并执行。
- 当前 phase 不合法则立即 reject/drop。
- ordinary lifecycle command 永远不会保存到未来 phase 执行。

LeRobot 的 phase validation 是 defensive layer，不是 command scheduler。同步 phase 或 phase 切换边界中残留在 kernel socket receive buffer 的 packet只是 transport backlog，不是 application-level command queue。

所有进入新 worker phase 的路径统一遵循：

```text
finish old-phase work
-> blind drain old transport backlog
-> commit new phase
-> publish completion STATUS for the new phase
-> only then accept fresh input
```

Command acceptance ACK 在 command被当前 phase解析和验证后立即发送；它不表示 phase transition或 lifecycle operation已经完成。新 phase的完成由后续 STATUS表示。

计划只规定 LeRobot 子模块自身的 protocol/lifecycle guarantees，不展开 MainController 的状态机、输入过滤、重试间隔或 ACK/STATUS 等待方式。

### 本地 mock 与授权边界

已确认本地 `/home/robot/Desktop/gello-deploy/zmq_franka_gello` fork 提供若干安全 mock、dry-run 和 component-test 能力，但没有现成的完整 command→control→telemetry localhost E2E mock runtime。

当前只允许 workstation-local 测试。明确禁止：

- 通过 SSH、ZMQ、TCP 或其他方式连接 NUC。
- 使用任何 NUC endpoint、默认 robot IP 或跨主机配置。
- 调用真实 Franka、真实 gripper、FCI 或 torque-control session。
- 发送任何可能最终到达实体机械臂或 gripper 的命令。

本次不建设新的 NUC simulator，不修改 production protocol 来适配测试。

## 2. 最小实现范围与正式产物

### 新增

- `src/lerobot/rollout/control_uds.py`
  - 单 client、单线程 `AF_UNIX/SOCK_SEQPACKET` server。
  - 负责 accept、blocking recv、nonblocking poll、blind drain、ACK/STATUS 发送和 cleanup。
  - 不包含 receiver thread、应用层 command queue 或 scheduler。
- `src/lerobot/rollout/strategies/controlled.py`
  - 实现线性 outer loop、Base-like inner loop、phase-local UDS、termination 和 optional dataset。
- `tests/test_controlled_rollout.py`
  - 覆盖 Controlled config、protocol、phase、dataset、termination 和 mocked lifecycle。
- 仓库根目录 `FR3_controlled_hardware_integration_test.md`
  - coding 及当前允许的 A/B 测试完成后生成。
  - 文件内容使用英文。
  - 其中 NUC/硬件步骤当前只编写、不执行。

### 修改

- `rollout/configs.py`、strategy factory 和 exports：
  - 注册 `ControlledStrategyConfig(type="controlled")`。
  - 增加 `control_socket_path`，默认 `/run/user/<uid>/lerobot_controlled.sock`。
- `rollout/context.py`：
  - Controlled 允许 `dataset=None`；提供 dataset 时校验有效 `repo_id`。
  - concrete robot 创建后、connect 前检查 required controlled capability。
  - Controlled 跳过 startup-pose observation，令 `initial_position=None`。
  - 不做通用 context transaction/rollback 重构。
- `robots/fr3/config_fr3.py`、`robots/fr3/fr3.py`：
  - 增加正式 reset/home 配置、FR3-only capability 和 reset target本地日志。
- `rollout/inference/rtc.py`：
  - 仅在定向测试证实 previous-episode inference 会污染 next START 后，增加最小 RTC-local synchronization。
- `lerobot_rollout.py` 及相关 Controlled、FR3、inference、reverse-ZMQ 文档：
  - 补充 CLI、UDS contract、reset/home、gripper OPEN、logging 和 teardown 语义。
  - 将原有 protocol response术语统一由 RESULT改为 ACK。
  - 将任何把 reset→gripper OPEN描述为 side effect、风险或待决定事项的相关表述修正为正式预期行为。

### 明确不修改

- generic `Robot` ABC。
- `ThreadSafeRobot`。
- `InferenceEngine` ABC。
- `BaseStrategy`、`EpisodicStrategy` 的现有语义。
- SensorHub UDS、aligned SHM、FRCMD1/FGT1 ABI。
- MainController。
- `zmq_franka_gello` production code。
- 通用 state machine、event bus、receiver thread、command latch、reset cancellation、generic interpolation fallback 或 standalone keyboard mode。

## 3. Controlled UDS、ACK/STATUS 与 blind drain

### Protocol operations

- `INITIALIZE`
- `START`
- `STOP`
- `ABORT`
- `SHUTDOWN`
- `FAIL_STOP`

Command schema：

```text
protocol_version
type = COMMAND
sequence
operation
```

ACK schema：

```text
protocol_version
type = ACK
sequence
operation
accepted
code
phase
message
```

ACK 是 application-level command acknowledgement：

- 表示 command已经成功解析，并根据收到时的当前 worker phase接受或拒绝。
- `accepted=true` 表示当前 phase接受该 command。
- `accepted=false` 表示 application-level rejection。
- ACK 不表示 lifecycle operation或 phase transition已经完成。
- 不增加独立 NACK message type。

STATUS 报告后续 worker lifecycle transition或 operation outcome：

```text
READY
INITIALIZING
INITIALIZED
STARTED
STOPPED
ABORTED
COMPLETED
SHUTTING_DOWN
FAIL_STOPPING
ERROR
```

其他语义：

- blind drain 中被丢弃的 stale packet不进行 command解析或 dispatch，因此不要求逐包产生 ACK/STATUS。
- MainController 不提供也不会收到 `q_reset`。
- 本计划不规定 MainController 如何关联或消费 ACK/STATUS。

### Sequence validation 与 FAIL_STOP

正常 sequence validation适用于所有 operations，包括 `FAIL_STOP`：

- 新 command必须使用正常递增 sequence。
- duplicate或倒退 sequence按既有 duplicate/stale sequence规则拒绝。
- `FAIL_STOP` 不增加 sequence特例。

重复发送 `FAIL_STOP` 时，每次是新的 command，例如：

```text
FAIL_STOP seq=100
FAIL_STOP seq=101
FAIL_STOP seq=102
```

`FAIL_STOP` 是 idempotent、可安全重复发送的 fatal termination request：

- LeRobot 实际读取并接受 `FAIL_STOP` 时，立即进入 no-new-motion fatal teardown。
- 多次合法 delivery不得产生新的 robot/gripper motion。
- 协议不要求 exactly-once delivery。
- 本计划不规定上层如何重试、等待或调度 `FAIL_STOP`。

### 单线程 phase-local I/O

```text
WAIT_INITIALIZE
    -> blocking recv()

INITIALIZING
    -> synchronous initialize_rollout()
    -> _reset_joints()
    -> 不读取 Controlled UDS

WAIT_START
    -> blocking recv()

RUNNING
    -> 每个 control-loop iteration nonblocking poll()
```

- WAIT phase 中读取到的 command按当前 phase立即处理；无效 command立即以 `ACK accepted=false` 拒绝，随后继续等待。
- malformed或 oversize packet按可用信息 reject/drop。
- duplicate或倒退 sequence按正常 validation拒绝，不保存到后续 phase。
- UDS EOF、disconnect 或 socket failure属于 internal fatal，不等待重连。
- INITIALIZING 是同步、不可软件抢占区间。

### INITIALIZING 后的 blind phase-boundary drain

INITIALIZING 期间到达的 packet可能物理存在于 kernel socket receive buffer；这只是 transport backlog。

`initialize_rollout()` 成功返回后、进入 `WAIT_START` 前：

```text
finish INITIALIZING work
-> nonblocking recv/discard until EAGAIN
-> commit WAIT_START
-> publish INITIALIZED with phase=WAIT_START
-> only then blocking recv fresh input
```

该 drain 的唯一职责是防止旧 phase中积累的 transport input在下一 phase获得新的合法含义。

明确不在 drain 中进行：

- lifecycle command dispatch；
- SHUTDOWN/FAIL_STOP特殊识别；
- command arbitration；
- pending intent管理；
- deferred scheduling。

因此：

- stale `INITIALIZE/START/STOP/ABORT` packet被直接丢弃。
- 恰好在同步阻塞期间进入 backlog的 `SHUTDOWN` packet也可被丢弃，不具有跨 phase delivery guarantee。
- 恰好进入 backlog的 `FAIL_STOP` packet也可被丢弃；其 idempotent/retriable property消除了必须保存该 packet的要求。
- drain 中若 `recv` 返回 EOF或 socket error，而不是正常 packet，则仍视为 internal fatal。

reset 自身失败直接成为 internal fatal，不尝试 return-to-home motion。

### RUNNING 中的 polling和 phase-exit cleanup

- 每个 control tick nonblocking poll UDS。
- 对实际读取到的 command立即按 RUNNING phase validation并发送 ACK。
- 合法 command立即执行；不合法 command立即 reject/drop。
- 实际读取并接受 `FAIL_STOP` 时立即进入 no-new-motion fatal teardown。
- 实际读取并接受 `SHUTDOWN` 时进入 graceful shutdown。
- 没有 packet时立即继续 control loop，不阻塞 FPS。

如果 STOP、ABORT、duration completion或其他事件结束 RUNNING，统一遵循：

```text
finish RUNNING work
-> nonblocking recv/discard until EAGAIN
-> commit destination phase
-> publish completion STATUS with destination phase
-> only then accept fresh input
```

- 已缓冲的旧-phase packet不解析、不分类、不执行。
- EOF/socket failure仍成为 internal fatal。
- 不进行 batch arbitration、termination scanning或跨 phase delivery。
- 不能先发布新 phase的 STATUS再执行 drain，以免丢弃收到 STATUS后发送的合法新 command。

## 4. Controlled outer loop、inner loop与 inference lifecycle

### Setup

- concrete robot创建后、connect前检查以下 callable：
  - `initialize_rollout()`
  - `return_to_home()`
- capability缺失时抛 `NotImplementedError`，不连接 robot、不产生 motion。
- inference engine在进程生命周期内只启动一次；Controlled idle phase保持 paused。
- Controlled UDS绑定后，`run()`接受一个长期 client，并发布 `READY`、`phase=WAIT_INITIALIZE`。
- 不启动 keyboard listener。

### 线性 outer loop

```text
while session active:
    WAIT_INITIALIZE:
        blocking recv
        accept INITIALIZE / SHUTDOWN / FAIL_STOP
        reject other operations

    INITIALIZING:
        keep inference paused
        concrete_robot.initialize_rollout()
        blindly discard stale transport backlog
        commit WAIT_START
        publish INITIALIZED
        then accept fresh input

    WAIT_START:
        blocking recv
        accept START / SHUTDOWN / FAIL_STOP
        reject other operations

    RUNNING:
        prepare a new inference episode
        resume inference
        execute Base-like loop
        poll UDS each control tick
        always pause inference when leaving
```

使用函数返回值、异常和 `continue`组织控制流，不引入通用 state machine或 `goto`。

### 统一 phase transition 顺序

每个 command先在收到时的当前 phase完成解析和 validation，并发送对应 ACK。若 command导致 phase transition，则后续严格执行：

```text
finish old-phase work
-> blind drain old transport backlog
-> commit new phase
-> publish completion STATUS for the new phase
-> only then accept fresh input
```

具体路径：

- `WAIT_INITIALIZE -> INITIALIZING`
  - 接受 INITIALIZE并发送 ACK。
  - 完成 WAIT_INITIALIZE退出工作。
  - blind drain WAIT_INITIALIZE backlog。
  - commit INITIALIZING。
  - publish INITIALIZING。
  - 开始同步 reset；INITIALIZING不读取 UDS。
- `INITIALIZING -> WAIT_START`
  - reset成功返回。
  - blind drain INITIALIZING期间形成的 backlog。
  - commit WAIT_START。
  - publish INITIALIZED with phase=WAIT_START。
  - 开始 blocking recv。
- `WAIT_START -> RUNNING`
  - 接受 START并发送 ACK。
  - 完成 RTC/reset/interpolator/cache等 START boundary preparation。
  - blind drain WAIT_START backlog。
  - commit RUNNING。
  - publish STARTED with phase=RUNNING。
  - 开始 RUNNING tick-level UDS polling。
- `RUNNING -> WAIT_INITIALIZE`
  - 完成 pause和 episode save/clear。
  - blind drain RUNNING backlog。
  - commit WAIT_INITIALIZE。
  - publish STOPPED、ABORTED或 COMPLETED with phase=WAIT_INITIALIZE。
  - 开始 blocking recv。
- terminal transition同样先完成旧 phase工作并清理旧 backlog，再 commit/publish terminal phase；随后不再接受 lifecycle input。

若 disconnect/socket failure使 drain无法正常完成，则直接进入 internal fatal，不再 commit或发布正常目标 phase的 completion STATUS。

### START 边界与 RTC synchronization

RTC background inference不直接调用 `robot.send_action`。因此 previous inference与 FR3 INITIALIZE/reset motion同时存在，本身不定义为 robot-command race，也不要求在 INITIALIZE前等待 RTC quiescent。

需要验证的是：

```text
previous rollout inference still running
-> next START
-> engine.reset()
-> old inference finishes or merges stale result
-> next episode contaminated
```

定向测试先确认当前 RTC implementation是否允许该污染：

- 若不存在实际 race，不增加同步机制。
- 若存在 race，仅在 next START的 episode reset边界增加最小 RTC-local synchronization：

```text
ensure previous inference quiescent
-> engine.reset()
-> interpolator.reset()
-> clear cached observation
-> engine.resume()
```

具体内部 API根据 coding时的 RTC结构选择；不预先固定 `wait_until_idle()`或新增公共接口。

保持以下边界：

- 不修改 `InferenceEngine` ABC。
- 不全局重定义 `pause()`。
- 不改变 Base/Episodic的 pause semantics。
- Sync backend不增加无必要同步。
- 不因 RTC synchronization改变 INITIALIZING的 FR3 reset流程。

### RUNNING lifecycle

每次 START：

1. 如定向测试证实需要，先完成 RTC-local previous-episode quiescence。
2. `engine.reset()`。
3. `interpolator.reset()`。
4. 清除 cached processed observation。
5. `engine.resume()`。
6. blind drain WAIT_START transport backlog。
7. commit RUNNING并发布 STARTED。
8. 执行 observation → inference → interpolation → action loop。
9. 每个 tick nonblocking poll UDS。
10. 离开 RUNNING时始终在 `finally`中 pause inference。

结束规则：

- `STOP`：
  - pause inference并保存 non-empty optional dataset episode。
  - blind drain RUNNING backlog。
  - commit `WAIT_INITIALIZE`。
  - publish `STOPPED` with phase=`WAIT_INITIALIZE`。
  - only then blocking recv fresh input。
- 正值 `--duration` 到期：
  - pause inference并保存 non-empty optional dataset episode。
  - blind drain RUNNING backlog。
  - commit `WAIT_INITIALIZE`。
  - publish `COMPLETED` with phase=`WAIT_INITIALIZE`。
  - only then blocking recv fresh input。
- `ABORT`：
  - pause inference并清空 optional dataset episode。
  - blind drain RUNNING backlog。
  - commit `WAIT_INITIALIZE`。
  - publish `ABORTED` with phase=`WAIT_INITIALIZE`。
  - only then blocking recv fresh input。
- 实际读取到 `SHUTDOWN`：
  - 发送 accepted ACK。
  - 清空 unfinished episode并完成 RUNNING退出工作。
  - blind drain旧 backlog。
  - commit graceful terminal phase并发布 `SHUTTING_DOWN`。
  - 进入 graceful teardown。
- 实际读取到 `FAIL_STOP`：
  - 发送 accepted ACK。
  - 清空 unfinished episode并完成 RUNNING退出工作。
  - blind drain旧 backlog。
  - commit fatal terminal phase并发布 `FAIL_STOPPING`。
  - 进入 no-new-motion fatal teardown。

任何一轮 RUNNING结束后都不能直接再次 START；下一轮必须重新：

```text
INITIALIZE -> WAIT_START -> START
```

## 5. Optional Dataset 与 finalization

Dataset 是 Controlled 主路径中的 optional recording sink：

- 默认 `dataset=None`。
- dataset presence不改变任何 phase、rollout或 termination行为。
- 无 dataset时使用 `nullcontext()`或等效结构：
  - 不创建 `VideoEncodingManager`；
  - 不构建 recording frame；
  - 不执行 dataset finalization。
- 有 dataset时沿用标准 create/resume、feature schema、`rollout_` naming和 `LeRobotDataset`格式。
- 仅在 `action_dict is not None`时添加标准 observation/action/task frame。
- `dataset.num_episodes`、`episode_time_s`和 `reset_time_s`不控制 Controlled lifecycle。

Episode ownership：

```text
ControlledStrategy
    -> add_frame()
    -> save_episode()
    -> clear_episode_buffer()
```

- STOP/completion：保存 non-empty episode，不创建空 episode。
- ABORT、FAIL_STOP、RUNNING中 SHUTDOWN：清空当前 episode buffer。
- internal fatal：best-effort clear，绝不保存 partial episode。

Video/session ownership：

```text
VideoEncodingManager / LeRobotDataset
    -> 按当前 repository lifecycle完成 video/session finalization
```

实现保持最小：

- 每个 dataset session只有一次清晰的 finalization ownership。
- 不依赖无依据的 double-finalize幂等性。
- `VideoEncodingManager`不替代 episode save/discard。
- Controlled不重新实现 video finalization。
- setup、正常退出和异常退出路径都必须只 finalize一次。
- 不预先锁死 `manager_entered` flag、新 API或额外 lifecycle abstraction。
- 不改变 Episodic的现有行为。

测试使用 spy/counter验证 episode save/clear和 session finalize次数。

## 6. FR3 正式配置、capability 与本地 logging

`FR3Config` 增加三个非 Optional 的正式配置：

```python
rollout_home_joint_positions = (
    0.1416057646,
    0.3408541381,
    -0.0186031274,
    -1.5938080549,
    0.0486696586,
    1.8890386820,
    0.0432172865,
)

rollout_init_delta_lower = (-0.01,) * 7
rollout_init_delta_upper = (0.01,) * 7
```

`q_home` 来源：

```text
zmq_franka_gello/test_artifacts/
reverse_startup_drift_20260812_194933/
cycle_01/robot_fgt1.jsonl
```

`FR3Config.__post_init__()` 无条件验证：

- 每个 vector长度严格为 7。
- 每项是 finite real value。
- 不接受 bool。
- 每个 joint满足 `lower[i] <= upper[i]`。
- 不使用 `None`、`NaN`或 placeholder。
- 不将这些字段加入 `sensorhub_dict()`。

FR3-only capability：

```text
FR3.initialize_rollout()
    delta[i] ~ Uniform(lower[i], upper[i])
    q_reset = q_home + delta
    log q_reset locally
    _reset_joints(q_reset)
```

其正式结果是：

- arm移动到本轮 sampled `q_reset`；
- gripper OPEN。

```text
FR3.return_to_home()
    log deterministic q_home locally
    _reset_joints(q_home)
```

其正式结果是：

- arm精确回到 deterministic `q_home`；
- gripper OPEN。

Logging 要求：

- 在调用 `_reset_joints()`前记录完整 concrete target。
- 日志明确区分 rollout initialization target与 graceful home target。
- 日志用于调试、测试证据和未来硬件验证。
- 不增加 UDS field、random seed protocol、跨进程 target handshake或 MainController依赖。

其他约束：

- 每次 INITIALIZE独立采样。
- MainController不提供、不接收也不需要知道 `q_reset`。
- achieved position由后续 observation telemetry自然呈现。
- Controlled直接通过 `robot_wrapper.inner`调用。
- 不修改 generic `Robot`或 `ThreadSafeRobot`。
- 不增加 generic interpolation fallback。
- 不修改 reset frame、FRCMD1 ABI或现有 router对 gripper command的解释。

## 7. Graceful 与 fatal teardown

Controlled 内部只需区分：

```text
CONTINUE
GRACEFUL_SHUTDOWN
FATAL_TEARDOWN
```

### SHUTDOWN + return-to-home success

实际读取到当前 phase合法的显式 UDS `SHUTDOWN` 后：

1. 发送 `ACK accepted=true`。
2. 停止 policy control并 pause inference。
3. 若存在 unfinished episode，执行 clear。
4. blind drain当前 phase的 transport backlog。
5. commit graceful terminal phase。
6. publish `SHUTTING_DOWN`。
7. 调用 `return_to_home()`：
   - 本地记录 deterministic `q_home`；
   - 发送 `RESET_JOINT(q_home)`；
   - arm回到 `q_home`；
   - gripper OPEN。
8. stop inference engine。
9. 按第 5 节唯一 owner结束 dataset/video session。
10. 关闭 Controlled UDS。
11. disconnect FR3，并按既有 ownership关闭 SensorHub。
12. session successful exit。

同步 phase形成的 stale backlog中的 SHUTDOWN不具有跨 phase delivery guarantee；blind drain不会识别或执行它。

### SHUTDOWN + return-to-home failure

如果 `return_to_home()`抛出或报告失败：

1. 记录并尽可能通过现有 STATUS/error路径报告失败。
2. 不再尝试任何其他 recovery motion。
3. 继续 stop inference、dataset/video cleanup、UDS close和 robot/SensorHub disconnect。
4. cleanup failure不得覆盖原始 home failure。
5. session最终必须以 non-success结束，不能视为成功 graceful shutdown。

使用现有 top-level error/result机制实现；不增加通用 error framework。

### FAIL_STOP 和 internal fatal

LeRobot 实际读取到当前 phase合法、sequence有效的 `FAIL_STOP` 时：

1. 发送 `ACK accepted=true`。
2. 立即停止当前 lifecycle并完成必要的 old-phase cleanup。
3. pause/stop inference。
4. best-effort clear partial episode。
5. blind drain当前 phase的 transport backlog。
6. commit fatal terminal phase。
7. publish `FAIL_STOPPING`。
8. 不调用 `return_to_home()`，不发送任何新的 motion command。
9. 完成 dataset/video cleanup、UDS close和 robot/SensorHub disconnect。
10. session以 non-success/fatal result结束。

相同或倒退 sequence的 `FAIL_STOP`仍按正常 sequence规则拒绝。使用新递增 sequence的重复 delivery保持安全，不产生额外 motion。

同步 phase形成的 stale backlog中的 `FAIL_STOP`可以被 blind drain直接丢弃。LeRobot不解析或保存该 stale packet。

Internal fatal包括：

- UDS disconnect/EOF/socket failure。
- stale/max-age。
- SensorHub fatal。
- reset failure。
- policy、inference、runtime或 dataset exception。
- unexpected interruption。

Internal fatal使用相同 no-new-motion teardown：

- 不调用 `return_to_home()`。
- 保留原始异常。
- cleanup failure不覆盖根因。
- session以 non-success/fatal result结束。
- transport已经失效时，不要求完成 drain或发送 ACK/STATUS。

`FR3.get_observation()` stale/max-age exception不得由 Controlled捕获并转换为 ABORT，且最终不得触发 return-to-home。

## 8. 分层测试、硬件文档与最终汇报

### A. LeRobot local unit/component tests

本层不依赖 `zmq_franka_gello` runtime，使用 `lerobot-fr3`环境和 mock，覆盖：

- Controlled config、factory和 exports。
- FR3 home/lower/upper正式默认值及无条件 validation。
- 非 7 维、non-finite、bool、`lower > upper`的拒绝。
- `q_reset=q_home+Uniform(lower,upper)`范围及逐轮重新采样。
- mocked `_reset_joints()`：
  - INITIALIZE使用 sampled `q_reset`；
  - graceful shutdown精确使用 `q_home`。
- `q_reset`/`q_home` local log与实际 reset target一致。
- capability缺失时在 connect/motion前抛 `NotImplementedError`。
- generic `Robot`、`ThreadSafeRobot`未增加 reset API。
- WAIT phase blocking recv和 RUNNING tick-level nonblocking poll。
- ACK表示 command解析及当前 phase acceptance/rejection。
- STATUS表示后续 phase transition或 operation outcome。
- 不存在独立 NACK message type。
- ordinary lifecycle command不支持 deferred execution。
- INITIALIZING期间不读取 Controlled UDS。
- INITIALIZING后的 phase-boundary drain对已缓冲 packet执行 blind discard：
  - 不解析 lifecycle operation；
  - 不识别或保留 SHUTDOWN/FAIL_STOP；
  - stale packet不会在 `WAIT_START`执行。
- 所有 phase transition均验证以下顺序：

```text
finish old-phase work
-> blind drain old backlog
-> commit new phase
-> publish new-phase completion STATUS
-> accept fresh input
```

- STOP、ABORT和 duration completion不能在 blind drain前发布 `STOPPED`、`ABORTED`或 `COMPLETED`。
- STATUS发布后到达的合法新 command不得被前一 phase的 drain误删。
- RUNNING phase-exit cleanup使用 blind discard，旧-phase input不会穿越到 `WAIT_INITIALIZE`。
- drain遇到 EOF/socket failure时进入 internal fatal。
- 实际读取到 `FAIL_STOP`时立即 no-new-motion teardown。
- 使用递增 sequence重复发送 `FAIL_STOP`保持安全。
- 相同或倒退的 `FAIL_STOP` sequence按正常 duplicate/stale规则拒绝，不增加 sequence特例。
- 实际读取到合法 `SHUTDOWN`时执行既定 graceful teardown。
- STOP、ABORT、completion均回到 `WAIT_INITIALIZE`。
- 每次 START reset engine、interpolator和 observation cache。
- 所有 RUNNING出口始终 pause inference。
- RTC测试专门确认 previous-episode inference是否会在 next START的 reset后 merge stale result；若存在，验证最小 RTC-local synchronization。
- RTC测试不把 inference与 FR3 reset motion并发本身视为 robot-command race。
- dataset enabled/disabled、frame add、episode save/clear、标准 reload和单次 session finalize。
- stale/max-age、SensorHub fatal、reset/runtime failure原样传播且不回 home。
- graceful return-to-home success产生 successful session exit。
- graceful return-to-home failure：
  - 最终 non-success；
  - 完成资源 teardown；
  - 不产生第二次 recovery motion。
- FAIL_STOP/internal fatal保持 no-new-motion。

执行测试时使用：

```text
conda run -n lerobot-fr3 env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest ...
```

### B. Local cross-repo mock integration tests

记录实施时两个仓库的 SHA。仅利用本地 `zmq_franka_gello` 已确认安全的 mock、dry-run和 component-test infrastructure；不构建不存在的完整 E2E simulator。

在隔离的 `inproc://`或 `/tmp`临时 `ipc://` endpoint上，尽可能验证：

- LeRobot FRCMD1 serialization/send与实际 codec、router、receiver兼容。
- normal action frame。
- sampled `q_reset` local log、实际 FRCMD1 payload和 receiver capture一致。
- deterministic `q_home` local log、实际 FRCMD1 payload和 receiver capture一致。
- `RESET_JOINT`的 gripper byte被 router解释为 OPEN，dry-run/mock gripper path收到 OPEN command。
- INITIALIZE和 successful graceful return-to-home均符合 reset→gripper OPEN。
- 实际处理的 FAIL_STOP不产生新的 return-to-home FRCMD1或 gripper OPEN command。
- fake telemetry/codec与 LeRobot reader的 FGT1互操作。
- 已有安全 component tests中的 receiver/router、reset control state、`RESETTING 0 -> 1 -> 0`、timeout/watchdog/JUMP_HOLD、fake-control和 dry-run gripper路径。
- LeRobot编码的 FRCMD1可由 fork解析，fork编码的 FGT1可由 LeRobot解析。

`zmq_franka_gello`测试使用：

```text
conda run -n franka_zmq312 python -m unittest ...
```

本层不声称覆盖：

- LeRobot `_reset_joints()`通过完整 localhost NUC mock telemetry闭环完成 ACK。
- command-driven fake robot q与同一 runtime返回的 closed-loop telemetry。
- 完整 SensorHub + all sensor sources。
- NUC-specific runtime。
- 真实 FCI、torque control、arm motion或 gripper motion。

这些空白不通过新增 simulator补齐，直接进入 C。

### C. Future NUC / real-hardware integration

当前只编写测试方案，不执行，包括：

- 任何 NUC connection或 NUC-specific environment。
- 完整 routed reverse runtime。
- 真实 Franka、gripper、FCI和 torque control。
- 真实 randomized reset motion。
- 真实 graceful q_home return。
- 真实 reset→gripper OPEN motion。
- 真实 RESETTING handshake。
- 真实 disconnect、timeout、JUMP_HOLD和 failure response。

### `FR3_controlled_hardware_integration_test.md`

coding和 A/B测试后生成：

```text
/home/robot/Desktop/gello-deploy/LeRobotFR3/
FR3_controlled_hardware_integration_test.md
```

编写前读取：

- `/home/robot/Desktop/gello-deploy/zmq_franka_gello/docs/NUC_reverse_test_prompt.md`
- 当前 authoritative routed reverse-ZMQ documentation。

文档至少包含：

- LeRobot/NUC branch、SHA、环境和 prerequisites。
- A、B已验证内容及证据。
- 仍需 NUC runtime验证的内容。
- 必须真实 FR3/gripper才能验证的内容。
- workstation/NUC人工启动顺序。
- 每项测试的目的、operator steps、expected UDS/FRCMD1/FGT1/RESETTING behavior。
- ACK的 application-level acceptance/rejection语义与 STATUS的 transition/outcome语义。
- 所有新 phase在 publish completion STATUS前完成 old-phase blind drain。
- STATUS发布后才接受 fresh input。
- no deferred lifecycle command execution。
- phase-boundary drain只是 blind stale-input discard。
- stale SHUTDOWN/FAIL_STOP packet不具有跨 phase delivery guarantee，也不要求 drain识别或保留。
- FAIL_STOP的 idempotent/retriable property、正常递增 sequence要求及 no-new-motion结果。
- actual sampled `q_reset` local log与 FRCMD1/telemetry核对。
- actual deterministic `q_home` local log与 FRCMD1/telemetry核对。
- previous-rollout RTC state不得污染下一次 START。
- INITIALIZE：
  - arm移动到 sampled `q_reset`；
  - gripper OPEN。
- START、STOP、ABORT。
- successful graceful SHUTDOWN：
  - arm回到 deterministic `q_home`；
  - gripper OPEN；
  - successful session exit。
- graceful home failure：
  - 不再产生 recovery motion；
  - 完成 teardown；
  - 最终 non-success。
- FAIL_STOP/internal fatal：
  - no-new-motion；
  - 不发送 return-to-home或新 gripper command。
- disconnect、timeout、SensorHub/FCI/gripper failure。
- pass/fail criteria、日志和 artifact要求。
- 明确标注会产生真实 arm motion、gripper motion或二者同时 motion的步骤。
- 将 reset→gripper OPEN描述为正式 expected behavior，而不是异常或待决定事项。
- 对意外 motion、方向、torque、non-finite command、telemetry loss、missing RESETTING transition、FCI/process failure及 operator concern定义立即停止条件。
- 明确声明文档创建时未执行任何 NUC/硬件步骤。

无需在文档中设计 MainController重试策略、ACK等待策略或详细 UDS message sequence。

### Implementation 完成后的最终汇报

最终报告明确列出：

1. 实际运行的 A测试命令、范围和结果。
2. 本地 `zmq_franka_gello` mock/dry-run/component capability的检查结论和代码依据。
3. 实际运行的 B测试、真实覆盖范围和结果。
4. 因现有 mock无法组成完整闭环而转入 C的项目。
5. 明确声明未连接任何 NUC、未调用真实 Franka/gripper、未建立 FCI session、未发送可达实体硬件的命令。
6. `FR3_controlled_hardware_integration_test.md`的绝对路径。
7. 尚未经过真实 NUC/FR3/gripper验证的风险点，包括真实时序、reset handshake、控制稳定性、failure response和 teardown行为；reset→gripper OPEN作为已确定的正式语义报告，不列为待决定问题。
