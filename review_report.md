1. **【维度 1/2：Xense UDS 协议魔数与主控客户端不一致】**
  - **位置证据**：[MainController/uds_client.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/uds_client.py:16) 固定 `MAGIC = b'F3'`，[main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:105) 用同一个 `UdsClient` 创建 `xense_client`；真实 Xense 协议在 [XenseTacSensor/protocol/messages.py](/home/robot/Desktop/gello-deploy/XenseTacSensor/protocol/messages.py:9) 定义 `MAGIC = b"XS"`，并在 [同文件](/home/robot/Desktop/gello-deploy/XenseTacSensor/protocol/messages.py:67) 拒绝非 `XS` header。
  - **确定性推导**：主控向 Xense 发送的 `INIT_REQ/START_REQ` header 必然带 `F3`，Xense 服务端必然判定 `invalid magic`，因此主控无法完成真实 Xense `INIT_READY/ACK` 流程。
  - **修正指令**：让 `UdsClient` 支持按传感器配置 magic，FT300S 使用 `F3`，Xense 使用 `XS`；或抽出共享协议定义，禁止 MainController 内硬编码单一 magic。
  - **report status**: accepted
  - **FixPlan task**: task 1
  - **Resolution note**: 修复为 per-sensor UDS magic；FT300S 使用 `F3`，Xense 使用 `XS`。第 6 条测试 mock 协议问题并入本任务。

2. **【维度 1/2：`PAUSED -> FINALIZING/DISCARDING` 在真实传感器路径无 ACK，`p -> d` 卡死】**
  - **位置证据**：[plan.md](/home/robot/Desktop/gello-deploy/plan.md:77) 和 [plan.md](/home/robot/Desktop/gello-deploy/plan.md:78) 允许 `PAUSED -> FINALIZING/DISCARDING`；[main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:218) 允许 `finish_demo()` 从 `PAUSED` 执行，并在 [main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:224) 无硬超时等待 `DEMO_DONE_REQ` ACK；FT300S 只在 `COLLECTING` ACK `DEMO_DONE_REQ`，见 [FT300S/core/service.py](/home/robot/Desktop/gello-deploy/FT300S/core/service.py:193)，Xense 同样只在 `COLLECTING` ACK，见 [XenseTacSensor/core/service.py](/home/robot/Desktop/gello-deploy/XenseTacSensor/core/service.py:163)。
  - **确定性推导**：用户执行 `p` 后，两个传感器进入 `PAUSED`。随后执行 `d`，两个服务端走 `INVALID_STATE` 分支并发送 `ERROR`，不会发送 `ACK(cmd=DEMO_DONE_REQ)`。主控在 [uds_client.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/uds_client.py:158) 的无超时循环中等待 ACK，主线程不再处理后续 `q`。
  - **修正指令**：统一状态协议。方案一：传感器服务端显式支持 `PAUSED` 下 `DEMO_DONE_REQ/DEMO_DISCARD_REQ` 并 ACK。方案二：主控在 `PAUSED` 下拒绝 `d/x`，要求先 `s` 恢复。无论选择哪种，都要让 `ERROR` 唤醒 ACK 等待并进入可清理状态。
  - **report status**: accepted
  - **FixPlan task**: task 2
  - **Resolution note**: 保留主控允许 paused 下 finish/discard 的状态机；补齐两个传感器 service 在 `PAUSED` 下处理 `DEMO_DONE_REQ` / `DEMO_DISCARD_REQ` 并 ACK。第 7 条测试覆盖并入本任务。

3. **【维度 2：启动失败不执行 `ERROR -> STOPPING`，已启动子进程留存】**
  - **位置证据**：[main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:107) 的 `run()` 只捕获 `KeyboardInterrupt`；[main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:127) 先启动子进程，[main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:359) 到 [main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:371) 在 ZMQ、UDS、rosbag 未就绪时直接 `raise RuntimeError`；文档要求 [plan.md](/home/robot/Desktop/gello-deploy/plan.md:84) `ERROR -> STOPPING`。
  - **确定性推导**：当 ZMQ 首帧超时或 UDS 初始化失败时，异常越过 `run()`，`finally` 只关闭 logger。此前由 [processes.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/processes.py:43) 启动的子进程没有经过 `stop_all()`。
  - **修正指令**：在 `startup()` 外层捕获 `Exception`，立即 `set_state(ERROR)`，执行 `stop_all()`，再以非零退出；`_start_processes()` 中任一 `Popen` 失败时也要停止已启动进程。
  - **report status**: accepted
  - **FixPlan task**: task 3
  - **Resolution note**: 启动阶段关键模块失败视为不可恢复；记录错误，进入 `ERROR`，统一清理已启动资源，最终 `STOPPED`。

4. **【维度 2：`START_REQ` 不是事务，第二个传感器失败后第一个传感器继续采集】**
  - **位置证据**：[main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:176) 先向 FT300S 发送 `START_REQ`，再在 [main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:179) 向 Xense 发送；Xense 失败时 [main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:180) 直接 `return`。FT300S 收到 START 后在 [FT300S/core/service.py](/home/robot/Desktop/gello-deploy/FT300S/core/service.py:157) 到 [FT300S/core/service.py](/home/robot/Desktop/gello-deploy/FT300S/core/service.py:163) 进入 `COLLECTING` 并 ACK。
  - **确定性推导**：当 FT300S ACK 而 Xense 超时或拒绝时，FT300S 已经采集，主控仍未进入 `COLLECTING`，rosbag 未启动，当前 `demo_store` 已创建但不会写入 FT300S 帧。
  - **修正指令**：把 start/pause/done/discard 做成事务。第二个传感器失败时，主控必须向已 ACK 的传感器发送 `PAUSE_REQ` 或 `STOP_REQ` 回滚，并把 demo 标记为 failed 或删除临时目录。
  - **report status**: accepted
  - **FixPlan task**: task 4
  - **Resolution note**: 将 start/resume 视为 all-or-nothing 多传感器事务；任一失败则对已 ACK start 的传感器发送 `DEMO_DISCARD_REQ` 并清理主控 demo 上下文。第 8 条事务文档策略并入本任务。

5. **【维度 2：ZMQ receiver 终止异常后只记录日志，持续 drain 要求失效】**
  - **位置证据**：文档要求 ZMQ 从启动到退出持续 drain，见 [plan.md](/home/robot/Desktop/gello-deploy/plan.md:17)；但 [zmq_telemetry.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/zmq_telemetry.py:118) 到 [zmq_telemetry.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/zmq_telemetry.py:126) 捕获 receiver 外层异常后关闭 socket；[main.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/main.py:400) 只记录 WARN。
  - **确定性推导**：`poll/recv/on_frame` 外层异常发生后，receiver 线程结束，SUB socket 被关闭，主控状态不变，后续不再 drain telemetry。
  - **修正指令**：区分非法帧与 receiver 终止。终止错误必须投递 fatal command，触发 receiver 重启或 `ERROR -> STOPPING`。
  - **report status**: accepted
  - **FixPlan task**: task 5
  - **Resolution note**: ZMQ receiver fatal/断连视为不可恢复错误；MainController 进入 `ERROR` 并停止整个系统。非法单帧仍只告警并继续 drain。

6. **【维度 3：UDS mock 与被测代码共享错误协议，Xense 兼容性断言为空】**
  - **位置证据**：mock 测试在 [test_maincontroller_mock_runtime.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/test/test_maincontroller_mock_runtime.py:22) 从 `main_controller.uds_client` 导入 `pack_message/unpack_header`；mock 服务端在 [同文件](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/test/test_maincontroller_mock_runtime.py:111) 用该 `unpack_header` 解码。
  - **确定性推导**：mock Xense 与 MainController 使用同一套 `F3` 协议，因此测试只证明主控能和自身协议通信，不能验证真实 Xense 的 `XS` 协议。
  - **修正指令**：mock FT300S 使用 `FT300S.protocol.messages`，mock Xense 使用 `XenseTacSensor.protocol.messages`；新增断言：MainController 发往 Xense 的首两个字节等于 `XS`。
  - **report status**: merged
  - **FixPlan task**: merged into task 1
  - **Resolution note**: 作为第 1 条真实协议兼容性的测试防线处理；mock FT300S/Xense 改用各自真实协议 helper，并增加 `F3` / `XS` header 断言。

7. **【维度 3：已声明的 `PAUSED -> FINALIZING/DISCARDING` 未被机械覆盖】**
  - **位置证据**：测试流程在 [test_maincontroller_mock_runtime.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/test/test_maincontroller_mock_runtime.py:407) pause 后，[test_maincontroller_mock_runtime.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/test/test_maincontroller_mock_runtime.py:412) 先 resume，再在 [test_maincontroller_mock_runtime.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/test/test_maincontroller_mock_runtime.py:417) finish；实现计划测试也只列 `s -> p -> s -> d -> q`，见 [implement_plan.md](/home/robot/Desktop/gello-deploy/implement_plan.md:247)。
  - **确定性推导**：`p -> d` 和 `p -> x` 两条文档允许路径没有任何断言执行，所以上述无 ACK 卡死路径不会被当前测试发现。
  - **修正指令**：增加 `s -> p -> d`、`s -> p -> x` 测试，并让 mock 在 `PAUSED` 下按真实服务端返回 `ERROR`；测试必须带超时断言，验证主控不挂死且进入明确终态。
  - **report status**: merged
  - **FixPlan task**: merged into task 2
  - **Resolution note**: 作为第 2 条的机械测试覆盖处理；新增 `s -> p -> d` 与 `s -> p -> x` 超时保护测试。

8. **【维度 4：多传感器命令缺少事务边界】**
  - **缺失的系统要素**：文档只写了顺序发送 `START_REQ/PAUSE_REQ/DEMO_DONE_REQ`，见 [plan.md](/home/robot/Desktop/gello-deploy/plan.md:149) 到 [plan.md](/home/robot/Desktop/gello-deploy/plan.md:170)，没有定义单个传感器 ACK、另一个传感器 ERROR/超时时的回滚所有权。
  - **脑补的常识**：作者暗中依赖“两个物理传感器总是一起成功或一起失败”。
  - **补全建议**：文档必须回答：哪个模块负责回滚已 ACK 的传感器；回滚失败时 demo 状态写成 failed、discarded 还是 error；rosbag 已 record 但传感器未全量 start 时如何处理。
  - **report status**: merged
  - **FixPlan task**: merged into task 4
  - **Resolution note**: 作为第 4 条的事务 ownership 和 rollback 文档策略处理；MainController 作为多传感器事务 owner。

9. **【维度 4：RealSense 启用相机与 metadata topic 的权威来源缺失】**
  - **缺失的系统要素**：MainController 默认订阅四路相机 metadata，见 [config.py](/home/robot/Desktop/gello-deploy/MainController/src/main_controller/main_controller/config.py:43)；RealSense launch 当前只启用 `cam3`，见 [four_realsense_640x480_30.launch.py](/home/robot/Desktop/gello-deploy/RealSense/launch/four_realsense_640x480_30.launch.py:8)。
  - **脑补的常识**：作者暗中依赖“配置里的相机集合与 launch 实际启动集合一致”。
  - **补全建议**：文档必须指定相机清单唯一来源、缺失 topic 的启动超时、manifest 中 disabled/missing camera 的记录方式，以及是否允许只采集子集相机。
  - **report status**: accepted
  - **FixPlan task**: task 6
  - **Resolution note**: 转为正式四相机 image stream baseline 和 rosbag post-check 文档 TODO；不修改当前运行代码。

10. **【维度 4：ZMQ 丢包后的数据质量决策未定义】**
  - **缺失的系统要素**：ZMQ 参考文档明确队列满会丢帧，见 [Zmq_Ref/Readme.md](/home/robot/Desktop/gello-deploy/Zmq_Ref/Readme.md:121) 和 [Zmq_Ref/Readme.md](/home/robot/Desktop/gello-deploy/Zmq_Ref/Readme.md:142)；主计划只定义告警阈值，见 [plan.md](/home/robot/Desktop/gello-deploy/plan.md:96) 到 [plan.md](/home/robot/Desktop/gello-deploy/plan.md:108)。
  - **脑补的常识**：作者暗中依赖“告警足够表达数据质量”。
  - **补全建议**：文档必须定义每个 demo 的 ZMQ 丢包预算、超过预算时 pause/abort/继续的策略、seq reset 边界，以及 relay HWM 与主控接收 HWM 的验收值。
  - **report status**: reframed
  - **FixPlan task**: task 7
  - **Resolution note**: 不采纳丢包预算、quality status 或自动 pause/abort 方向；仅作为文档优化处理，明确不连续 key 会产生 `drop_warning`、打印终端、写入 `controller_events.jsonl`，并累计到 manifest。

11. **【维度 1：manifest 缺少帧数，且放弃 demo 无 manifest】**
  - **位置证据**：`plan.md:45` 要求 manifest 记录帧数和完成/放弃状态；`implement_plan.md:226-230` 要求记录各 `.npz` 路径和帧数；`main.py:445-452` 的 manifest 没有帧数字段；`main.py:246-264` 的 `discard_demo()` 不写 manifest。
  - **逻辑推导**：完成 demo 的 manifest 不包含任何 `frame_counts`；放弃 demo 后没有 `manifest.json`。这两点与 root plan 的文字要求同时冲突。
  - **修正指令**：在 `_save_current_demo()` 中加入 `frame_counts`；`discard_demo()` 写入 `status: "discarded"` 的 manifest，或把 root plan 改为“放弃 demo 不生成 manifest”。
  - **report status**: accepted
  - **FixPlan task**: task 8
  - **Resolution note**: 接受 manifest 缺少 `frame_counts` 的问题；完成 demo manifest 应记录 `ft300`、`xense`、`realsense`、`zmq` 等帧数。discard 路径应写 lightweight manifest，并在清空 `demo_store` 前读取内存 buffer 统计必要帧数。

12. **【维度 3：存在恒真断言】**
  - **位置证据**：`test_maincontroller_mock_runtime.py:460-463` 中 `assert len(controller.demo_store.zmq) != first_demo_rows or second_demo_dir.exists()`；`main.py:168` 创建 demo 目录；`test_maincontroller_mock_runtime.py:353` 返回该目录。
  - **逻辑推导**：`second_demo_dir` 在返回前已由 `mkdir()` 创建，所以 `second_demo_dir.exists()` 必然为真。该断言整体为 `A or True`，不验证 ZMQ buffer 是否独立。
  - **修正指令**：删除 `or second_demo_dir.exists()`，改为显式检查第二段 `.npz` 内容、seq 范围或 buffer 起始状态。
  - **report status**: accepted
  - **FixPlan task**: task 9
  - **Resolution note**: 接受恒真断言问题；删除 `or second_demo_dir.exists()`，改为检查连续 demo 的 ZMQ `.npz` 数据范围或 buffer 起始状态，确保第二段数据独立。

13. **【维度 3：.npz 字段长度一致性测试未实现】**
  - **位置证据**：`plan.md:198` 和 `implement_plan.md:241` 要求验证 `.npz` 字段长度一致；`test_maincontroller_mock_runtime.py:436-443` 只检查每个 npz 的一个字段长度；`buffers.py:80-83` 每个 buffer 都定义多个字段。
  - **逻辑推导**：测试执行了 `.npz` 生成与读取，但没有断言同一文件内所有字段长度相等，因此该计划项没有机械验证。
  - **修正指令**：添加 helper：遍历 `npz.files`，断言所有数组 `len()` 等于首字段长度，并对四个 `.npz` 全部调用。
  - **report status**: accepted
  - **FixPlan task**: task 10
  - **Resolution note**: 接受 `.npz` 字段长度一致性测试缺口；新增 helper 遍历单个 `npz.files`，断言同一 `.npz` 内所有字段长度一致，并应用到四个主控 `.npz` 文件。不要求不同传感器/数据流文件之间行数一致。

14. **【维度 4：discard manifest 规则自相矛盾】**
  - **位置证据**：`plan.md:45` 写明 manifest 记录“完成/放弃状态”；`implement_plan.md:245` 写明 discard 不保存 manifest。
  - **逻辑推导**：对同一个被放弃 demo，文档 A 要求存在 manifest 记录放弃状态，文档 B 要求不存在 manifest。两条规则不能同时成立。
  - **修正指令**：统一文本。推荐改为：`discarded demo 写入 manifest.json，status 为 "discarded"，不保存高频 .npz；只保留 controller log 和 rosbag 停止记录。`
  - **report status**: merged
  - **FixPlan task**: merged into task 8
  - **Resolution note**: 作为 task 8 的 discard manifest 文档规则统一处理；discarded demo 写 lightweight `manifest.json`，`status: "discarded"`，不保存高频 `.npz`，并在清空 buffer 前记录必要统计。
