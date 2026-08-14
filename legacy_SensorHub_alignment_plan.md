# FR3 SensorHub camera-bundle 触发重构计划

## 检查结论

- 当前根因位于 [cache.py](/home/robot/Desktop/gello-deploy/LeRobotFR3/src/lerobot/robots/fr3/sensorhub/cache.py)：`CausalAligner.select()` 以各 camera 最新样本的最小 `ingest_monotonic_ns` 为截面，再执行 `latest_at_or_before()`。staggered arrivals 会产生逐 camera 变化的混合 sequence tuple；tuple 只要不同就发布，因此四路约 30 Hz camera 可能形成接近聚合更新率的 clustered publishes。
- camera skew 当前也基于 ingest time，而不是 `CameraSample.source_timestamp_ns`，与离线 bundler 的语义不一致。
- 同一 `CausalAligner` 还用 camera 截面作为 Xense/FT/robot/gripper 的人工 `T0`，选择 `latest_at_or_before()`；这不符合在线 latest-available 目标。
- [runtime.py](/home/robot/Desktop/gello-deploy/LeRobotFR3/src/lerobot/robots/fr3/sensorhub/runtime.py) 每 1 ms 轮询并立即发布每个新 tuple；[aligned_shm.py](/home/robot/Desktop/gello-deploy/LeRobotFR3/src/lerobot/robots/fr3/sensorhub/aligned_shm.py) 的 sequence parity 决定两个约 6 MB slot。clustered publishes 会缩短 `N → N+2` 同槽重写间隔，可能增加 reader 复制期间 slot 被改写、最终出现 `no coherent AlignedObservation snapshot` 的概率；这是代码路径支持的机制推断，仍需新增 timing/retry diagnostics 验证。

## 实现变更

- 在 [config_fr3.py](/home/robot/Desktop/gello-deploy/LeRobotFR3/src/lerobot/robots/fr3/config_fr3.py) 和 `SensorHubConfig` 中保留/新增三个独立参数：
  - `camera_bundle_span_warn_ms=20`：nominal coherence/degraded 分界。
  - `camera_max_skew_ms=50`：hard source-span publication gate。
  - `camera_bundle_wait_ms=25`：provisional arrival/scheduling grace period。
  - 校验三者均为正数且 `camera_bundle_span_warn_ms <= camera_max_skew_ms`，并由 `sensorhub_dict()` 传入子进程；不提高 `snapshot_read_timeout_ms`。

- 在 [cache.py](/home/robot/Desktop/gello-deploy/LeRobotFR3/src/lerobot/robots/fr3/sensorhub/cache.py) 拆分职责，但保留 `CausalAligner.select()` 作为兼容 façade：
  - 为 `SampleCache` 增加只读、有锁的 ordered snapshot API。camera caches 仅作为 rolling search windows，不作为等待逐个消费的历史 bundle queues。
  - 新增内部 `CameraBundle`/diagnostics 结构和 `CausalCameraBundler`，负责一次性 bootstrap、steady-state source-timestamp search 和 per-camera committed frontiers。
  - 不引入长期 `STARTUP → STEADY` FSM、logical backlog/replay/coalescing、reuse frontier 或 reuse count。
  - 新增 `LatestObservationAssembler`，仅在 bundler commit 新 bundle 后读取一次 `xense_cache.latest()`、`ft_cache.latest()`、`robot_cache.latest()`、`gripper_cache.latest()`，以 assembly 时的 `monotonic_ns - ingest_monotonic_ns` 执行现有 max-age 策略。
  - 非 camera source timestamp 不参与相互对齐，不设置 shared `T0`，也不调用 `latest_at_or_before()`。
  - camera bundle 在交给 assembler 时即消费；若 required modality missing/stale，该 bundle 不发布，也不会因随后某个 non-camera 更新而重试。camera frontier 仍继续推进，后续新 camera bundle 才能恢复 FR3OBS2 publication。
  - aligned sequence 只在成功组装时递增；background reader threads 和 `AlignmentPublisher` 均不直接或同步等待上游 SHM/ZMQ。

## SensorHub bootstrap 与 steady-state camera rounds

- 在 [runtime.py](/home/robot/Desktop/gello-deploy/LeRobotFR3/src/lerobot/robots/fr3/sensorhub/runtime.py) 将 bootstrap 纳入现有一次性 startup/READY 流程：
  1. 按现状启动 UDS、attach writer/readers，并启动所有 background reader threads。
  2. 使用同一个 startup deadline 等待每个 camera cache 至少出现两个 advancing sequences。
  3. `SensorHubRuntime` 显式调用一次 `CausalCameraBundler.initialize()`；重复初始化应被拒绝。
  4. 对每路 camera 取 `{previous, latest}`，对四 camera 搜索 `[-1,0]^4` 的 16 个 tuple；动态 `N` camera 情况使用相同的 `2^N` bounded search。
  5. 以最小 source-timestamp span 为第一评分项、更新的 `bundle_time_ns=max(source timestamps)` 为 tie-break，commit bootstrap tuple 并建立四个 per-camera frontiers。
  6. 启动/放行 alignment publisher；继续要求 Xense、FT、robot、gripper 等 required sources 均至少推进两次，并成功发布至少一个 aligned observation 后才报告 `READY`。
- bootstrap 在一个 SensorHub process 生命周期中只执行一次。rollout 边界、episode 切换以及 READY 后有意暂停 Xense/FT 均不得 reset/reinitialize bundler。
- 若 bootstrap 或后续 camera bundle 因 non-camera missing/stale 未形成 FR3OBS2 publication，camera bundler 仍独立 commit 和推进 frontiers。

- steady-state 不重建离线完整 timeline：
  - Bundle T commit 后，其每路 selected sample 成为对应 camera frontier。
  - cache ordering 中晚于 frontier 的样本均为 post-frontier；sequence 可以跳号，不要求连续。
  - 任意 camera 首次出现 post-frontier sample 即打开下一 round。
  - `round_start_ingest_ns` 是当前所有已缓存 post-frontier samples 中最早的原始 `ingest_monotonic_ns`；等待时间属于数据，不属于某次 alignment-loop 调用。
  - round 在所有 cameras 均已有 post-frontier sample，或 `camera_bundle_wait_ms` 到期时进行评估；没有 privileged/master camera。

- 每次评估按以下固定顺序执行：
  1. 首先检查四路 current-latest tuple。
  2. 若 source span `<=20 ms`，立即 commit。
  3. 否则执行 bounded `{previous, latest}` search。每路候选最多为当前 rolling window 中 latest 及其紧邻 predecessor，并以 committed frontier 为下界；无 post-frontier sample 时允许 frontier 本身参与 drop fallback。
  4. 评分为最小 source span，其次更新的 bundle time。
  5. searched candidate `<=20 ms` 时立即 commit。
  6. 最佳 candidate 为 `20–50 ms` 且 wait 尚未到期时保持 pending，允许更好的后续到达参与下一次评估。
  7. wait 到期后允许 commit `20–50 ms` candidate，并标记 degraded。
  8. 最佳 candidate `>50 ms` 时不 commit、不发布，保持当前 round pending，直到后续 samples 使 bounded search resync。
  9. 最终 tuple 必须至少有一路相对于前一 committed tuple 推进，且不得重复 commit identical tuple。

- non-latest selection 的 frontier/timer 语义：
  - 若 search commit 某路较旧 candidate，而同一 camera 的更新 sample 已在 cache 中，该更新 sample 会立即成为下一 round 的 post-frontier sample。
  - 下一 round 的等待年龄从该 sample 原始 `ingest_monotonic_ns` 计算，不在 commit 时重置。
  - 不增加“同一 frame 最多 reuse 一次”规则。reuse 仅作为当前 bundle 的 diagnostic 属性；由于候选窗口持续向 latest 移动且 50 ms hard gate 生效，约 30 Hz 下可自然容纳一次 drop fallback，同时 camera 持续停机后最终停止 commit。

- 与离线 bundler 的关系限定为：沿用 camera source timestamp、bounded tuple search、span-first score、resync/degraded/reuse 语义；不沿用 `locked_plus_one` timeline progression，也不在在线端补放或 coalesce 历史 logical bundles。两个仓库之间不建立 import 或运行时依赖。

## Xense、FR3OBS2 与 calibration diagnostics

- 在 [readers.py](/home/robot/Desktop/gello-deploy/LeRobotFR3/src/lerobot/robots/fr3/sensorhub/readers.py) 的 `XenseReader.read()` 将 `XenseSample.source_timestamp_ns` 改为 `max(timestamp0_ns, timestamp1_ns)`；两个 tactile arrays 仍是同一 SHM row、同一个 `XenseSample`，`ingest_monotonic_ns` 保持读取完成时的在线可用时间。
- 在 `runtime.py` 保留现有 reader/process/READY/recovery 生命周期，只接入 bootstrap、三项 camera 参数及事件型 diagnostics。每次 bundle decision 记录 mode、source span、round wait age、selected sequences、resync/degraded 状态及本次自然发生的 reused camera IDs；不维护复用次数或复用 frontier。
- 为 calibration 增加默认关闭的可选 `camera_timing_trace_path`：
  - 每次成功完成 coherent `RealSenseReader.read()` 后，以 JSONL 持久化 `camera_id`、`sequence`、`source_timestamp_ns`、`ingest_monotonic_ns`。
  - camera sample 先进入 cache，再通过非阻塞、有界诊断队列交给单独 trace writer，避免文件 I/O 成为 camera reader 的同步路径；队列溢出只增加 dropped-trace-record diagnostic，不影响 SensorHub。
  - trace 使用 `CameraSample` 已有数据，不改变 RealSense SHM ABI。
- 在 `aligned_shm.py` 添加无 ABI 影响的诊断：
  - writer 记录实际 publish interval、同 parity slot 的 `N → N+2` rewrite interval 和一次 slot copy duration。
  - client 在 coherence timeout 中附带 bounded retry 分类，例如 latest 未就绪、slot 正在写、复制前后 sequence 改变、header mismatch 及最后观察到的 sequence。
  - 不改变 `FR3OBS2` magic/version、layout、slot count、offset、header 或 payload，也不改变 reader timeout 默认值。

- inference chain 工作后另行执行代表性 calibration，启用 RealSense、SensorHub intra-process/SHM、rosbag recording、observation construction、policy inference、Reverse ZMQ/NUC telemetry，仅在可行时禁用最终 physical actuator execution。
- rosbag recording 必须保留，因为它属于真实 rollout 负载；历史 rosbag `Recorded span` 仅作为 recorder-path/stress reference，不作为 SensorHub arrival clock。
- 使用实际 ingest trace 做严格 causal replay 和 `camera_bundle_wait_ms` threshold sweep，再确定最终值。25 ms 仅是低于约 33.3 ms camera period 的 bring-up placeholder。
- 历史 91 reports / 69,654 bundles 支持 20 ms nominal reference 和 50 ms conservative hard gate：cross-demo Header-p95 p50 为 20.457 ms，最大 Header max 为 30.877 ms，91/91 demo 的 Header maxima 均低于 50 ms；这些数据不用于确定 25 ms wait。

## 测试计划

- 更新 [test_alignment_and_shm.py](/home/robot/Desktop/gello-deploy/LeRobotFR3/tests/robots/fr3/test_alignment_and_shm.py)：
  - 四 camera bootstrap 必须各有两个 advancing samples，并验证 `[-1,0]^4` 的 16-tuple span/tie-break selection。
  - 参数化 camera arrival order，证明没有 privileged camera。
  - first post-frontier sample 打开 round，并以其原始 ingest time 开始 wait。
  - all-cameras-arrived 与 25 ms timeout 两条 evaluation path。
  - current-latest `<=20 ms` fast path和 bounded fallback `<=20 ms` immediate commit。
  - `20–50 ms` 在 timeout 前保持 pending、timeout 后 commit degraded；`>50 ms` 始终 hard reject。
  - 单帧和多帧 dropped sequence jumps，无连续 sequence 假设。
  - bounded reuse 不依赖 reuse counter；identical tuple 不得重复 commit。
  - stalled camera 可短暂参与有界 degraded reuse，但其他 cameras 推进后 source span 超过 50 ms 时自然停止 commit。
  - non-latest candidate commit 后，已缓存 newer sample 立即成为 post-frontier，并沿用其原始 ingest wait age。
  - Xense 两种 scalar layout 均断言 pair timestamp 为 `max(t0,t1)`。
  - writer timing diagnostics 验证正常约 30 Hz 下 publish interval 约 33 ms、同槽间隔约 66 ms；client 的 deterministic incoherent-slot 测试断言 retry diagnostics。

- 更新 [test_sensorhub_runtime.py](/home/robot/Desktop/gello-deploy/LeRobotFR3/tests/robots/fr3/test_sensorhub_runtime.py)：
  - startup 在每个 camera 两帧以前不得 initialize bundler 或报告 READY；bootstrap 恰好一次，且首个有效 aligned publication 后才 READY。
  - 模拟多个 rollout/episode 边界，证明不重新 bootstrap。
  - 使用 recording fake writer 驱动四 camera staggered arrival，验证每轮最多一次 publish，而非约四次 clustered publishes。
  - 高频更新 Xense/FT/robot/gripper 时不触发 publish；下一 camera bundle 使用各 cache 当时的 latest sequence/value。
  - Xense/FT 暂停或 stale 时 FR3OBS2 publication 被抑制，但 committed camera frontiers 继续推进；恢复 non-camera 本身不发布，后续 camera bundle 使用当前 latest values 恢复。
  - 保留 telemetry outage、persistent SUB、parent exit、fatal handling、READY required-source coverage。
  - optional camera timing trace 写出四个精确字段；trace queue overflow/关闭不影响 camera cache 与 SensorHub health。

- 更新 `test_fr3_robot.py`/现有 config tests，覆盖 20/50/25 ms 默认值、JSON 传递、正数校验、nominal threshold 不得大于 hard gate，以及 trace 默认关闭。
- 实现后仅运行 FR3 focused pytest（使用项目规定的 `lerobot-fr3-312` 环境和 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`）及非改写型语法/diff 检查；不运行未授权 lint、无关 policy 或全仓测试。代表性硬件 calibration 是后续单独授权的 bring-up 阶段，不作为离线单元测试的一部分。

## 兼容性与边界

- 保持 `RSRGBD1`、`FGT1`、`FRCMD1`、FR3OBS2 ABI、dynamic ordered camera mapping、SensorHub ownership、persistent telemetry SUB、UDS/reset handshake 不变；`samples.py`、generic `Robot` API 和 MainController 仓库无需修改。
- `camera_max_skew_ms` 的比较域将从 camera ingest-time skew 改为 camera source-timestamp span，这是有意的行为变化；20 ms、50 ms、25 ms 分别承担 nominal quality、hard gate、arrival grace 三种不同语义。
- 假设各 RealSense `source_timestamp_ns` 位于可比较且通常单调的 clock domain。此次不引入 clock correction、cache epoch、writer restart protocol 或 camera-stall fatal timer。
- degraded/reuse 可能在个别 drop/skew 周期重复图像，这是显式且可诊断的 bounded-search 结果；rolling `{previous, latest}` window 和 50 ms hard gate负责阻止 stalled camera 无限推进 publication。
- `camera_bundle_wait_ms=25` 不是由历史 rosbag source/recorded spans 推导的最终值；在真实 inference workload trace 完成 causal sweep 后，应以独立配置变更调整。
- 本次仍为计划修订，不修改代码、不运行测试。
