# `camera_bundle_wait_ms` 独立 calibration implementation plan

## 概要

- 以已实现并通过 correctness tests 的 `CausalCameraBundler` 和 provisional `camera_bundle_wait_ms=25` 为前置条件。
- `CameraSample.source_timestamp_ns` 是 camera tuple selection、score 和 coherence validation 的唯一 timestamp：`20 ms` nominal threshold 与 `50 ms` hard gate 均只作用于 source-timestamp span。`ingest_monotonic_ns` 不参与 tuple score 或 coherence filtering，不引入 camera ingest-span threshold；它只用于 causal availability、识别已到达的 post-frontier samples、定义 `round_start_ingest_ns` 及判断 `camera_bundle_wait_ms` 到期。
- Candidate progression 只遵循既有 per-camera frontier：selected sample 不得退回各自 frontier 之前、至少一路 camera 推进、identical tuple 不得重复 commit；不增加严格的 `bundle_time_ns > previous_bundle_time_ns` 要求。
- `CausalCameraBundler.initialize()` commit initial frontiers，并且只返回一次 bootstrap `CameraBundle` 供 assembly attempt。若 non-camera missing/stale 导致 assembly 失败，只抑制 FR3OBS2 publication，不回滚或冻结已 commit 的 camera frontiers。
- 本计划独立负责 timing trace、严格 causal replay、threshold sweep、证据评审及最终配置替换。
- 历史 91 reports / 69,654 bundles **只能支持** `20 ms` nominal source-span threshold 和 `50 ms` hard gate；不能确定最终 `camera_bundle_wait_ms`。历史 rosbag `Recorded span` 仅作为 recorder-path/stress reference，不能充当 SensorHub arrival clock。

## 实现变更

- 在 `FR3Config`、`SensorHubConfig` 和 `sensorhub_dict()` 增加默认关闭的 `camera_timing_trace_path: str | None = None`；不增加 RealSense SHM 字段，不修改 `RSRGBD1` ABI。
- 新增独立 timing-trace writer：
  - 每次 coherent `RealSenseReader.read()` 成功后，先把完整 `CameraSample` 写入对应 cache，再将仅含四个标量字段的 record 通过 `put_nowait()` 放入有界队列。
  - JSONL 每行严格包含 `camera_id`、`sequence`、`source_timestamp_ns`、`ingest_monotonic_ns`；`camera_id` 使用配置中的 RealSense SHM name。
  - writer 使用独立线程，正常关闭时排空队列并 flush；队列默认容量固定为 4096，测试时允许注入更小容量。
  - overflow 只累计并记录 `dropped_trace_records` diagnostic，不阻塞 reader、不撤回 cache sample、不设置 SensorHub `FATAL` 或改变 health。writer I/O 错误同样标记 trace 不可用于 calibration，但不改变正常 SensorHub health。
- 新增离线 calibration 工具：
  - 每个 trace 文件作为独立 monotonic clock domain 校验和回放，不跨进程拼接 `ingest_monotonic_ns`。
  - 按 `ingest_monotonic_ns` 排序，以文件顺序处理同 timestamp tie；只向生产 `CausalCameraBundler` 暴露当时已经到达的 sample。所有 tuple selection、score、20/50 ms filtering 仍只使用 `source_timestamp_ns`。
  - 同时调度 sample arrivals 与 pending round deadline，使用 simulated monotonic time、无 `sleep`，确保 timeout decision 也满足严格 causal 语义。
  - 允许 exceptional late-arrival/drop recovery 在 degraded commit 后短时间内产生额外 camera commit；它是新的 latest-state observation，不替换或修订此前 FR3OBS2 frame。分析只检查正常 staggered arrivals 下是否消除系统性的 per-camera clustered publication，不增加 round-revision flags、source-time watermarks 或 global rate limiting。
  - 默认 sweep 整数 `0–50 ms`，必须包含 provisional `25 ms`；输出每个 threshold 的 commit coverage、normal/degraded 数量、source-span 分布、round-wait 分布、reuse/resync/hard-gate 状态及相对 25 ms 的变化，生成 machine-readable CSV/JSON 和简洁摘要。

## Calibration 与配置替换

- inference chain 可用后，采集覆盖 startup warm-up 和完整 steady-state rollout 的 representative trace。保持真实 workload：RealSense、SensorHub intra-process/SHM、rosbag recording、observation construction、policy inference、Reverse ZMQ/NUC telemetry。
- 仅在现有控制面能安全做到时禁用最终 physical actuator execution；不得为 calibration 削减上述 workload。任何实际 FR3/FCI 或 actuator 执行仍需单独授权。
- calibration trace 必须证明 `dropped_trace_records=0`；否则该 run 作废并重新采集。
- 根据 sweep 的 latency/coherence/coverage Pareto 表进行人工证据评审，不由工具自动选值。确认具体 threshold 后，将 `camera_bundle_wait_ms` 默认值从 25 替换为批准值，并更新 config/runtime focused tests；保留 calibration 报告和选择理由作为变更证据。

## Focused tests

- Config：默认 `None`、显式 path 的 JSON-safe 传递、无效值校验，以及 tracing 关闭时不启动 writer。
- Reader/runtime：验证 coherent read 后严格执行“cache append → non-blocking trace enqueue”；writer 慢或队列满时 cache、reader thread、READY 和 SensorHub health 不受影响。
- Writer：验证 JSONL 每行只有四个指定字段、字段值来自同一 `CameraSample`、dynamic camera identity 正确、正常 shutdown drain/flush，以及 overflow counter 和 I/O failure diagnostics。
- Replay：用 synthetic staggered arrivals 验证不读取未来 sample、source-only tuple scoring/filtering、arrival/deadline 调度、bootstrap one-shot return、frontier-only progression、per-file clock isolation、threshold sweep 包含 25 ms，并与生产 bundler 对同一事件流的 decision 结果一致；late recovery burst 不判为 replacement 或系统性 clustered publication。
- 使用 `conda run -n lerobot-fr3-312` 和 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 运行 FR3 focused pytest；不运行未授权 lint、policy tests 或全仓测试。

## 从原 alignment plan 移出的边界

- 移出：timing trace 配置/采集/writer/overflow、真实 workload calibration、causal replay/sweep、历史证据限制、trace focused tests、25 ms 最终替换及独立授权说明。
- 继续留在原 plan：`camera_bundle_wait_ms` 的 runtime round/timer correctness、bootstrap/frontier/bounded-search 语义、20/50 ms source-timestamp gates、正常 bundle-decision diagnostics、FR3OBS2 contention/timing/retry diagnostics、Xense 修正及其 correctness tests。
- 不修改 `FR3OBS2`、`RSRGBD1` 或其他 SHM/wire ABI，不把 calibration trace 变成常规 runtime telemetry，也不改 generic `Robot` API、MainController 或 Reverse ZMQ ABI。
