# NUC 端 FRCMD1 / `RESET_JOINT` 最终实施计划

## 保持的架构

- 新增独立FRCMD1-only reverse torque server。
- Legacy GELLO JSON server不变。
- 单一持久FCI连接和`start_torque_control()` session。
- Reset复用现有`MotionGenerator`。
- Workstation提供具体`q_reset[7]`；NUC不做randomization、IK或rollout管理。
- FGT1 `RESETTING` bit是唯一reset状态路径。
- 不增加ACK replay、额外ACK socket、supervisor、automatic recovery或异步reset准备。
- 保持`SensorHub_plan.md`定义的：

```text
INITIALIZE
-> RESET_JOINT
-> RESETTING 0 -> 1 -> 0
-> rollout稍后开始
```

## 协议与入口

### FRCMD1

新增stdlib-only的`franka_gello_zmq/frcmd_protocol.py`：

- 使用`struct`、`math`、`dataclasses`和tuple。
- 不依赖numpy、pyzmq或pylibfranka。
- 定义固定112-byte `<8sIIIIQqq7dB7x` ABI。
- 提供不可变`FrankaCommand`和严格`parse_command()`。
- 验证magic、version、header/total size、known flags和7个finite targets。
- Malformed、unknown flags或same-sequence conflicting payload均log/drop。

### Reverse server

新增`franka_gello_zmq/reverse_torque_server.py`及console entry：

- 默认bind `tcp://*:6001`。
- `SUBSCRIBE=b""`、`RCVHWM=1`、`CONFLATE=1`、`LINGER=0`。
- 每个有效frame附加NUC-local `received_at`和递增`receive_index`。
- Legacy `torque_server.py`保持不变。
- 不抽取legacy limiter helpers；reverse server保留小型等价实现并用数值测试锁定行为。

## Startup与本地goal

Reverse server启动顺序：

```text
connect FR3
-> configure collision behavior
-> load model
-> start_torque_control() once
-> read initial q
-> commanded_goal = measured q
-> publish RESETTING=0
-> fixed-goal impedance HOLD
```

始终维护权威NUC-local：

```python
commanded_goal
last_live_goal
normal_stream_active
```

网络静默不得：

- 清除或恢复其他target；
- 因输入停止而输出zero torque；
- 退出或重启torque session；
- 重新运行startup interpolation。

首条command是normal时，保留现有`max_startup_error`和startup `MotionGenerator`。

首条command是reset，或reset在startup中到达时：

- reset优先；
- 丢弃当前startup generator；
- 从startup当前已生成的`commanded_goal.copy()`开始reset；
- 不重启FCI；
- reset完成后视为startup baseline已经建立，首个normal不再运行startup MotionGenerator。

## Reset准备与pre-HOLD

### MotionGenerator只构建一次

首次接受新reset时，一次性执行：

```python
reset_q_start = commanded_goal.copy()

build_started_at = monotonic()
reset_generator = MotionGenerator(
    startup_speed_factor,
    reset_q_start,
    q_reset,
)
reset_generator_build_duration = monotonic() - build_started_at
```

`reset_generator_build_duration`仅为diagnostic measurement：

- 不参与pre-HOLD、trajectory或completion计算。
- 不影响控制分支或错误策略。
- 不在1 kHz critical path执行同步逐reset日志。
- 如需保留测量值，只写入简单的进程内diagnostic字段；主要通过NUC专项benchmark收集。
- 只有真实NUC测量显示同步构造影响FCI deadline时，才另行考虑异步准备。

在`MotionGenerator`增加只读属性：

```python
@property
def duration_s(self) -> float:
    return float(np.max(self.t_f_sync))
```

该属性返回构造阶段已经计算好的synchronized final time，不重新估算轨迹。

定义：

```python
RESET_VISIBILITY_WINDOW_S = 0.10
```

它只用于一次性推导optional pre-HOLD：

```python
reset_pre_hold_duration = max(
    0.0,
    RESET_VISIBILITY_WINDOW_S - reset_generator.duration_s,
)
```

当前robot telemetry默认50 Hz，100 ms提供约5次正常`RESETTING=1`发布机会。

`RESET_VISIBILITY_WINDOW_S`：

- 不是reset completion condition；
- 不在recurring path重复读取或计算；
- 不要求MotionGenerator完成后继续HOLD `RESETTING=1`。

MotionGenerator成功构建并计算pre-HOLD后，提交accepted reset：

```python
reset_started_at = monotonic()
reset_active = True
reset_pre_hold_elapsed = 0.0
reset_trajectory_elapsed = 0.0
normal_stream_active = False
```

此后FGT1直接编码`reset_active=True`。

MotionGenerator构造失败或产生non-finite内部状态属于内部控制错误，reverse server fail-fast退出；不得发布虚假的ACK。

## 1 kHz控制顺序

每个control iteration固定采用：

```text
read robot state
-> poll/consume already-available command input
-> apply command semantics using current reset_active
-> advance pre-HOLD or reset MotionGenerator
-> if MotionGenerator finished, commit reset completion
-> compute/write torque
-> publish telemetry when due
```

关键约束：

- Reverse server在reset pre-HOLD和MotionGenerator执行期间都持续poll/consume command SUB。
- Command语义必须在本iteration原有`reset_active`仍具权威性时处理。
- 已经available的normal frame会在reset completion提交前被消费并丢弃。
- 本轮command处理完成后，才允许MotionGenerator完成并提交`reset_active=False`。
- 若packet确实在reset completion之后才到达NUC，则允许作为post-reset input；上层协议负责在`INITIALIZE`期间不发送normal policy command。

`receive_index`只是NUC-local接收/消费顺序：

- 不代表workstation send time。
- 不参与跨主机时钟比较。
- 不证明packet在rollout层面的生成阶段。
- 只用于保证一个已消费frame不会被latest-value receiver再次解释。

## 1 kHz reset路径

### Pre-HOLD

若：

```python
reset_pre_hold_elapsed < reset_pre_hold_duration
```

则每周期仅执行：

```python
q_goal = reset_q_start
commanded_goal = reset_q_start
reset_pre_hold_elapsed += period
```

这是简单counter/branch和fixed-goal阻抗控制：

- 不调用`MotionGenerator.get_desired_joint_positions()`；
- 不重新读取`duration_s`；
- 不做reset-distance/duration估算；
- 不运行ordinary watchdog；
- normal command仍被poll、消费并丢弃。

允许最后一个control period略微越过pre-HOLD截止时间；无需拆分单周期或补偿亚周期余量。

### Reset trajectory

Pre-HOLD结束后：

```python
reset_trajectory_elapsed += period
q_goal, motion_finished = reset_generator.get_desired_joint_positions(
    reset_trajectory_elapsed
)
commanded_goal = q_goal.copy()
```

- `duration_s >= RESET_VISIBILITY_WINDOW_S`时，pre-HOLD为0，trajectory立即开始。
- 短trajectory先HOLD `reset_q_start`，再运行完整MotionGenerator。
- Zero-distance reset先HOLD约100 ms，再调用generator并完成。
- 不在recurring path构建或重新规划trajectory。

### Completion

Reset completion condition只有：

```python
motion_finished
```

不包含`RESET_VISIBILITY_WINDOW_S`或wall-clock条件。

Pre-HOLD已经保证：

```text
pre_hold_duration + synchronized_trajectory_duration
>= RESET_VISIBILITY_WINDOW_S
```

完成时立即：

```python
commanded_goal = q_reset.copy()
last_live_goal = q_reset.copy()
reset_active = False
normal_stream_active = False
```

- 正常多秒reset没有附加completion delay。
- Short/zero reset的可观察时间全部位于trajectory之前。
- MotionGenerator完成后不再额外保持`RESETTING=1`。
- Post-reset固定HOLD `q_reset`并发布`RESETTING=0`。
- Completed duplicate保持no-op，不重新置1，不replay ACK。

提交completion时，将当前已消费的`receive_index`作为post-reset watermark。由于本轮command poll/dispatch先于completion，reset期间已经available的normal frame不会跨过该watermark。

## Commands与sequence处理

维护最小状态：

```python
last_reset_sequence
last_reset_frame
last_reset_status  # active/completed
last_consumed_receive_index
post_reset_receive_index
```

规则：

- 相同sequence、相同bytes：
  - active：不重建generator，不重置pre-HOLD counter、trajectory counter或timer。
  - completed：no-op，保持`RESETTING=0`。
- 相同sequence、不同bytes：protocol conflict，log/drop。
- 较旧sequence：stale，log/drop。
- 新sequence：
  - 无active reset时接受。
  - active期间拒绝且不更新`last_reset_sequence`。
- Sequence在一个NUC process生命周期内不重用。

整个`reset_active=True`阶段，包括pre-HOLD：

- normal commands被poll、消费并永久丢弃；
- 不更新`last_live_goal`或normal watchdog timestamp；
- 另一reset不能preempt；
- 每个frame的`receive_index`只处理一次。

Reset完成后仅接受watermark之后实际到达的新normal frame。

## Post-reset与ordinary watchdog

Reset完成后：

```text
q_goal = commanded_goal = q_reset
RESETTING=0
```

该HOLD可无限持续，即使远大于`input_timeout`。

第一条新normal target `q_0`：

1. 与`last_live_goal == q_reset`执行现有jump gate。
2. 合法时从`commanded_goal == q_reset`执行per-cycle rate limiting。
3. 设置`normal_stream_active=True`并启动normal watchdog timing。
4. 不运行startup MotionGenerator。

Ordinary watchdog仅在normal stream已启动后生效：

```text
normal tracking
-> normal input stale
-> HOLD当前commanded_goal
```

Timeout HOLD和post-reset HOLD复用相同fixed-goal impedance/coriolis/torque-rate控制。Fresh normal恢复继续沿用现有timeout-recovery语义。

## FGT1 telemetry

修改`telemetry.py`：

- 定义`ROBOT_TELEMETRY_FLAG_RESETTING = 1 << 0`。
- `pack_robot_frame(..., resetting=False)`将当前`reset_active`写入现有16-bit flags。
- 保持FGT1 v1、struct layout和504-byte frame size。
- Decode时保留flags，并在robot诊断输出中暴露`resetting`。
- Gripper不使用该bit；unknown bits继续忽略。
- Relay保持byte-transparent。
- 不修改`TelemetryPublisher.send()`。
- 不根据send结果确认ACK，不增加reset-specific transport state。

## Workstation测试

全部使用`/usr/bin/python3`；不创建环境，不安装缺失依赖。`frcmd_protocol.py`的golden tests必须可在纯stdlib环境运行。

### Protocol、telemetry和ZMQ

- FRCMD1 normal/reset golden vectors及malformed cases。
- FGT1 `RESETTING` true/false、504-byte ABI和diagnostic decode。
- PUB/SUB slow joiner、CONFLATE、stale、disappearance/reconnect。
- Duplicate、conflict、stale reset sequence。
- 验证`receive_index`仅表示NUC-local顺序。
- 验证reset iteration先消费available command，再提交completion。
- 验证reset completion后才实际到达的packet可以成为post-reset input。

### MotionGenerator与pre-HOLD

- `duration_s == max(t_f_sync)`且属性只读。
- 每次accepted reset只构建一个MotionGenerator。
- `duration_s`只在acceptance读取一次。
- `reset_generator_build_duration`不影响任何control state或duration计算。
- Recurring path不重新估算distance/duration。
- Duration大于100 ms：
  - pre-HOLD为0；
  - trajectory立即开始；
  - generator完成即`RESETTING=0`，无额外延迟。
- Duration小于100 ms：
  - pre-HOLD为`RESET_VISIBILITY_WINDOW_S - duration_s`；
  - pre-HOLD期间固定`reset_q_start`；
  - generator不在pre-HOLD期间调用。
- Zero-distance：
  - 约100 ms fixed-goal pre-HOLD；
  - telemetry保持`RESETTING=1`；
  - generator随后完成并立即切换为0。
- Duplicate不重建generator，也不重置pre-HOLD/trajectory counter。

### 控制语义

使用fake clock、period、receiver和轻量active-control接口：

- Reset总时间长于`input_timeout`时watchdog不干预。
- Post-reset长时间静默时无限HOLD `q_reset`且`RESETTING=0`。
- Pre-HOLD和trajectory期间normal永久丢弃。
- 与completion同iteration已经available的normal先被丢弃。
- Completion之后才到达的normal允许作为post-reset frame。
- 首个post-reset normal从`q_reset`执行jump/rate checks。
- Ordinary timeout保持当前local `commanded_goal`。
- Reset打断startup时从当前`commanded_goal`开始，FCI session对象不变。
- Fake read/write/model异常和non-finite内部结果导致非零退出。

运行：

```text
/usr/bin/python3 -m unittest discover -s tests -v
```

若任何非Franka依赖缺失，只跳过受影响测试并记录；不得安装依赖或修改production语义。

## NUC-only验证

专项测量一次性MotionGenerator construction latency：

1. 在NUC相同Python/runtime环境中，对zero、tiny和代表性较大reset target benchmark构造延迟。
2. 多次构建并报告p50、p99和max。
3. 在安全真实reset路径确认同步构造未造成FCI deadline/control exception。
4. Timing只作为diagnostic，不改变pre-HOLD或completion。
5. 当前不增加异步准备；只有真实测量证明必要时才另行设计。

真实FR3继续验证：

- 无command startup HOLD和telemetry-first READY。
- Tiny、zero、长于`input_timeout`的reset。
- `RESETTING 0 -> 1 -> 0`。
- Pre-HOLD后完整trajectory。
- Reset期间持续command polling。
- 长post-reset silence。
- 第一条policy target continuity。
- Target-jump recovery及ordinary timeout/reconnect。
- 单一FCI session和fail-fast异常边界。

最终报告分为：

1. 已在workstation实现并验证；
2. 已实现但需NUC/pylibfranka验证；
3. 因workstation缺失依赖而跳过的明确检查。

## 仓库差异与本次修订

- `SensorHub_plan.md`中“目标仓库没有NUC receiver/torque-loop”是原任务范围描述；本任务只补NUC endpoint，不改变其wire/handshake契约。
- Legacy GELLO server等待首条消息后才连接FCI，不适用于reverse拓扑；仅新增reverse server采用telemetry-first startup。
- 当前FGT1 flags固定为0且解码丢弃；必须启用bit 0，但ABI和默认false行为不变。
- 当前telemetry默认50 Hz，因此`RESET_VISIBILITY_WINDOW_S=0.10`提供约5次正常发布机会。
- 相对上一版，常量已从`MIN_RESET_ACTIVE_DURATION`更名为`RESET_VISIBILITY_WINDOW_S`，且只参与pre-HOLD推导。
- 明确command polling/consumption先于reset completion提交，避免同轮available normal跨越post-reset watermark。
- MotionGenerator construction timing仅为diagnostic，不参与控制语义，也不在critical path同步记录日志。

没有阻止实现的未决决定。
