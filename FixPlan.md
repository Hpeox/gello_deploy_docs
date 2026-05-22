# FixPlan

## Fix tasks

### 1. Xense UDS protocol magic differs from MainController client

- Merged review findings: 1 and 6
- Strategy: Fix code and tests. Make MainController's UDS protocol magic
  configurable per client while preserving the current FT300S default. Create
  FT300S clients with `b"F3"` and Xense clients with `b"XS"`. Also replace the
  runtime UDS mock's shared MainController protocol helpers with real sensor
  protocol helpers so Xense compatibility is actually tested.
- Involved files:
  - `MainController/src/MainController/MainController/uds_client.py`
  - `MainController/src/MainController/MainController/main.py`
  - `MainController/src/MainController/test/test_maincontroller_mock_runtime.py`
    or a focused UDS client test file
  - `FT300S/protocol/messages.py`
  - `XenseTacSensor/protocol/messages.py`
- Main code changes:
  - Add a per-client `magic` option to `UdsClient`.
  - Allow `pack_message()` and `unpack_header()` to use an explicit magic or
    expected magic, with backward-compatible defaults where practical.
  - Instantiate `ft_client` with `b"F3"` and `xense_client` with `b"XS"`.
- Test plan:
  - Update mock FT300S to use `FT300S.protocol.messages`.
  - Update mock Xense to use `XenseTacSensor.protocol.messages`.
  - Assert Xense-bound messages start with `b"XS"`.
  - Assert FT300S-bound messages start with `b"F3"`.
  - Avoid using `MainController.uds_client` protocol helpers inside sensor-side
    runtime mocks except where the test is specifically targeting the client
    helper itself.
- Risks and notes:
  - Existing tests import `pack_message()` and `unpack_header()` from
    `MainController.uds_client`, so API changes should be compatible or updated
    deliberately.
  - Finding 6 is merged into this task because it is the test-fidelity half of
    the same protocol compatibility problem.
  - `MsgType` values are currently aligned across MainController, FT300S, and
    Xense, but mock code should convert by integer value when crossing protocol
    modules to avoid enum type coupling.
- Suggested execution order: 1

### 2. PAUSED to finalizing/discarding has no real sensor ACK

- Merged review findings: 2 and 7
- Strategy: Fix code and tests. Keep the documented MainController behavior that
  allows finishing or discarding from `PAUSED`, and bring both sensor services
  into sync with that behavior.
- Involved files:
  - `FT300S/core/service.py`
  - `XenseTacSensor/core/service.py`
  - `FT300S/core/state.py`
  - `XenseTacSensor/core/state.py`
  - `MainController/src/MainController/MainController/uds_client.py`
  - `MainController/src/MainController/test/test_maincontroller_mock_runtime.py`
- Main code changes:
  - In both sensor services, accept `DEMO_DONE_REQ` from `PAUSED`, flush the
    current demo cache, transition to `WAIT_START`, and ACK with `saved_file`.
  - In both sensor services, accept `DEMO_DISCARD_REQ` from `PAUSED`, discard
    the current demo cache, transition to `WAIT_START`, and ACK.
  - Preserve the intended pause semantics: pause stops sensor acquisition but
    does not clear the current demo cache. MainController and sensor modules
    should therefore remain synchronized after `p -> d` and `p -> x`.
  - Update MainController UDS ACK waiting so a relevant sensor `ERROR` can wake
    the waiter and return failure instead of waiting indefinitely.
- Test plan:
  - Add `s -> p -> d` coverage with a timeout assertion; expected result is no
    hang and a clear return to `WAIT_START`.
  - Add `s -> p -> x` coverage with a timeout assertion; expected result is no
    hang and a clear return to `WAIT_START`.
  - Treat these paused finalization/discard paths as mechanical coverage for
    the documented state machine, not only as regression tests for the hang.
  - Add focused UDS client coverage for relevant `ERROR` response waking an ACK
    wait.
- Risks and notes:
  - `core/state.py` in both sensor modules already allows `PAUSED -> STOPPED`,
    but `core/service.py` does not currently implement the corresponding
    command behavior for paused finalization/discard. The service layer is the
    source of the deadlock.
  - Finding 7 is merged into this task as the required mechanical test coverage
    for the newly supported paused finish/discard paths.
- Suggested execution order: 2

### 3. Startup failure does not perform ERROR to STOPPING cleanup

- Strategy: Fix code and tests. Treat any required startup component failure as
  an unrecoverable startup failure: log the failure, enter `ERROR`, clean up all
  already-started resources, and end in `STOPPED`.
- Involved files:
  - `MainController/src/MainController/MainController/main.py`
  - `MainController/src/MainController/MainController/processes.py`
  - `MainController/src/MainController/test/test_maincontroller_mock_runtime.py`
    or a focused controller startup test file
- Main code changes:
  - Wrap startup so exceptions from process startup, receiver startup, ZMQ first
    frame wait, UDS connection, `INIT_READY`, or rosbag readiness are caught at
    the controller boundary.
  - On startup failure, log a `startup_failed` style event with the failing
    stage/error, set state to `ERROR`, call the common cleanup path, and finish
    in `STOPPED`.
  - Preserve failure visibility to the CLI by re-raising the exception or
    otherwise returning a non-zero process result.
  - In `_start_processes()`, if a later process fails to start, stop only the
    processes that already started successfully before propagating the failure.
- Test plan:
  - Simulate `_wait_startup_ready()` failure after process startup and assert
    cleanup runs and final state is `STOPPED` after passing through `ERROR`.
  - Simulate failure while starting the Nth managed process and assert earlier
    started processes are stopped.
  - Include a half-initialized case where UDS clients are not connected, because
    cleanup must not hang while sending `STOP_REQ`.
- Risks and notes:
  - `stop_all()` currently sends `STOP_REQ` to both sensor clients; startup
    cleanup must tolerate disconnected clients and unavailable receivers.
  - Startup is intentionally fail-fast: if the system has not reached
    `WAIT_START`, any required module failure should shut down all previously
    started resources instead of leaving a partially running stack.
- Suggested execution order: 3

### 4. START_REQ lacks a multi-sensor transaction boundary

- Merged review findings: 4 and 8
- Strategy: Fix code and tests. Treat demo start/resume as an all-or-nothing
  multi-sensor transaction. If any required sensor fails to start, log the
  failure, roll back sensors that already ACKed, and clear the MainController
  demo context. Also document MainController as the owner of multi-sensor
  transaction rollback policy. Start/resume transaction failure rolls back by
  sending `DEMO_DISCARD_REQ` to sensors that already ACKed `START_REQ`, clears
  the active demo buffers/context, and does not save high-frequency `.npz`;
  however, its manifest status is `failed`, not `discarded`.
- Involved files:
  - `MainController/src/MainController/MainController/main.py`
  - `MainController/src/MainController/test/test_maincontroller_mock_runtime.py`
  - `plan.md`
  - `implement_plan.md`
  - `MainController/src/MainController/README.md`
- Main code changes:
  - Track which sensors have ACKed `START_REQ`.
  - If a later sensor fails or times out, send `DEMO_DISCARD_REQ` to every
    sensor that already ACKed `START_REQ`.
  - Apply the same rollback behavior for both new demo start from `WAIT_START`
    and resume from `PAUSED`: record a failure log, discard the partially
    resumed/started sensor state, write a lightweight failed manifest, clear
    MainController's current demo context, and return to `WAIT_START`.
  - If rosbag record/resume fails after sensors ACKed start, also discard the
    started sensor state, write a lightweight failed manifest, and clear the
    demo context.
  - The failed manifest should use `status: "failed"` and record
    `failure_stage`, `failure_reason`, sensors that ACKed `START_REQ`, rollback
    action/results, and rosbag record/resume state if available.
  - Do not save high-frequency `.npz` files for start/resume transaction
    failures.
- Documentation changes:
  - State that MainController owns multi-sensor transaction coordination.
  - Define the start/resume rollback command as `DEMO_DISCARD_REQ` for sensors
    that already ACKed `START_REQ`.
  - Define start/resume transaction failure as failed-demo status, not
    user-discarded status.
  - Define rosbag record/resume failure as part of the same start/resume
    transaction failure path.
  - Explicitly state whether partial failures for `PAUSE_REQ`, `DEMO_DONE_REQ`,
    and `DEMO_DISCARD_REQ` are handled in this task or deferred to a later
    policy, so the documentation no longer relies on "both sensors always
    succeed or fail together".
- Test plan:
  - Simulate FT300S start ACK followed by Xense start timeout/ERROR. Assert
    FT300S receives `DEMO_DISCARD_REQ`, MainController does not enter
    `COLLECTING`, a lightweight failed manifest is written, and the demo
    context is cleared.
  - Simulate the same failure while resuming from `PAUSED`; expected behavior is
    still failure log plus `DEMO_DISCARD_REQ`, a failed manifest, and
    MainController returning to `WAIT_START`.
  - Simulate rosbag record/resume failure after both sensors start; assert both
    sensors receive discard rollback and the demo is recorded as failed rather
    than discarded.
- Risks and notes:
  - This intentionally chooses discard-on-resume-failure rather than preserving
    a paused demo. MainController behavior should be consistent across fresh
    start and paused resume failures, while the manifest status remains
    `failed`.
  - Transaction failure must not silently clear demo context and leave an
    ambiguous demo directory behind.
  - `discarded` is reserved for user-initiated discard. Start/resume transaction
    failure is a system failure even though it uses `DEMO_DISCARD_REQ` for
    rollback.
  - `DEMO_DISCARD_REQ` is already valid from `COLLECTING`; after finding 2 it
    will also be valid from `PAUSED`, which makes the rollback path simpler and
    more robust.
  - Finding 8 is merged into this task as the documentation and ownership
    policy for multi-sensor transaction rollback.
- Suggested execution order: 4

### 5. ZMQ receiver fatal termination only logs a warning

- Strategy: Fix code and tests. Treat ZMQ receiver fatal failure or disconnect as
  an unrecoverable controller error because the receiver is required to drain
  continuously for the lifetime of the system.
- Involved files:
  - `MainController/src/MainController/MainController/zmq_telemetry.py`
  - `MainController/src/MainController/MainController/main.py`
  - `MainController/src/MainController/test/test_maincontroller_mock_runtime.py`
    or a focused ZMQ receiver/controller test file
- Main code changes:
  - Keep invalid individual telemetry frames as non-fatal: log the invalid frame
    and continue draining.
  - Distinguish receiver-level fatal errors from invalid frames. Fatal examples
    include poll/recv failures, unexpected callback failures, or endpoint
    disconnect conditions that end the receiver loop.
  - Report receiver fatal errors to MainController as fatal events, not ordinary
    warnings.
  - MainController should log the fatal event, enter `ERROR`, run `stop_all()`,
    and stop the whole system.
- Test plan:
  - Verify invalid ZMQ payloads are logged but the receiver thread continues.
  - Simulate receiver fatal failure, for example by making `on_frame` raise, and
    assert MainController performs `ERROR -> STOPPING -> STOPPED`.
  - Verify the fatal path does not leave ZMQ receiver, sensor clients, rosbag, or
    subprocesses running.
- Risks and notes:
  - This intentionally upgrades receiver termination from warning-only behavior
    to whole-system shutdown. If ZMQ cannot be drained, the acquisition quality
    guarantee is already broken.
  - Automatic receiver restart is deferred unless a later data-quality policy
    explicitly defines how to handle the resulting telemetry gap.
- Suggested execution order: 5

### 6. RealSense four-camera image-stream baseline and rosbag post-check

- Strategy: Add documentation TODOs for formal four-camera acquisition quality
  gates based on a recorded normal-topic baseline.
- Documentation TODOs:
  - During development, capture one representative message from each of the 8
    expected RealSense image topics and store the stable schema fields in a
    config file.
  - The baseline config should include topic name, message type, width, height,
    encoding, step, and stream role such as color or depth.
  - Treat the baseline config / required image topic list as the single
    authoritative source for formal four-camera acquisition.
  - Formal runs require all 4 cameras / 8 image topics by default.
  - Add an explicit debug/degraded capture mode for partial camera/topic capture;
    this mode must be enabled by an explicit parameter, and its configured
    subset becomes the required topic list for that run.
  - Before starting rosbag recording in formal runs, receive at least one frame
    from each required image topic and compare its stable fields against the
    baseline config.
  - The readiness check should use the same required image topic list that
    rosbag2 will record.
  - After each recording ends, inspect rosbag metadata and verify that all 8
    required image topics are present.
  - Compare per-topic frame/message counts against expected duration/fps and
    against each other.
  - Define acceptable count skew threshold and record the readiness/post-check
    result in controller logs / manifest.
- Involved files:
  - `plan.md`
  - `implement_plan.md`
  - future implementation may add a RealSense topic baseline config and touch
    MainController rosbag/startup code
- Test plan:
  - No immediate test change.
  - Future tests should mock image topic readiness, baseline comparison, and
    rosbag metadata.
- Risks and notes:
  - Check image topics for rosbag readiness; metadata topics remain the realtime
    monitoring source.
  - The baseline should store stable schema fields, not raw full echo output or
    volatile timestamps.
  - Passing the pre-recording baseline check proves the image streams are alive
    and structurally correct before recording, but rosbag metadata remains the
    authoritative post-recording check.
  - Current code does not yet block recording startup on missing RealSense image
    topics; this task defines the future readiness gate.
  - Formal mode should fail closed on missing required image topics; subset
    capture is allowed only through explicit debug/degraded mode configuration.
  - The "frame counts are comparable" rule needs a concrete threshold before
    implementation.
- Suggested execution order: deferred after runtime correctness fixes

### 7. Document drop-warning behavior for non-contiguous stream keys

- Strategy: Documentation-only clarification. Explicitly document the existing
  drop-monitor behavior for frame id / seq / frame_number discontinuity.
- Documentation changes:
  - In `plan.md`, state that every detected non-contiguous key event emits one
    `drop_warning`.
  - The warning is printed to the terminal and written to
    `controller_events.jsonl`.
  - The monitor increments `missing_frame_count` by the positive key gap and
    increments `warning_count` by the emitted warning count.
  - The same behavior applies to FT300S `frame_id`, Xense `frame_id`, ZMQ `seq`
    per source, and RealSense metadata `frame_number` per topic.
  - Large timestamp intervals remain a separate `drop_warning` reason.
  - Demo manifest records each stream monitor summary, including
    `warning_count`, `missing_frame_count`, and `max_interval_ns`.
  - These warning/statistics fields are available for post-collection operator
    review.
- Involved files:
  - `plan.md`
  - optionally `implement_plan.md` if both docs should say the same thing
- Test plan:
  - No code test change required.
  - Existing/future `DropMonitor` tests should continue covering continuous
    frames, skipped keys, large intervals, and pause/resume baseline reset.
- Risks and notes:
  - This task does not introduce a drop budget, quality status, or automatic
    pause/abort behavior.
  - It only documents the current warning-and-recording behavior for
    discontinuities.
- Suggested execution order: documentation pass after runtime correctness fixes

### 8. Manifest frame counts and discarded-demo status record

- Merged review findings: 11 and 14
- Strategy: Fix code, tests, and docs. Add explicit frame counts to demo
  manifests and make the discard path write a lightweight discarded-demo
  manifest before the in-memory demo buffers are cleared.
- Involved files:
  - `MainController/src/MainController/MainController/main.py`
  - `MainController/src/MainController/MainController/buffers.py`
  - `MainController/src/MainController/test/test_maincontroller_mock_runtime.py`
  - `plan.md`
  - `implement_plan.md`
- Main code changes:
  - Add a frame-count helper for the current `DemoStore`.
  - Include `frame_counts` in the manifest written by `_save_current_demo()`.
  - Frame counts should cover at least `ft300`, `xense`, `realsense`, and
    `zmq`.
  - For discarded demos, write `manifest.json` with `status: "discarded"` before
    `discard_demo()` clears `demo_store`.
  - User-initiated discard writes `status: "discarded"`.
  - Do not use `status: "discarded"` for start/resume transaction failures;
    those are handled by task 4 as failed demos.
  - Do not save high-frequency `.npz` files for discarded demos; record `npz` as
    `{}` or an equivalent explicit empty value in the discard manifest.
  - For discarded demos, compute `frame_counts` from in-memory buffers before
    clearing `demo_store`.
- Documentation changes:
  - Remove statements that discard keeps only controller log or does not save a
    manifest.
  - State that discard saves a lightweight manifest with discarded status and
    summary fields, but does not save high-frequency `.npz` artifacts.
  - Define `discarded` as user-initiated discard via the `x` command.
  - Define `failed` as system/controller transaction failure, such as
    start/resume partial sensor failure or rosbag record/resume failure.
  - State that both discarded and failed demos may avoid high-frequency `.npz`
    artifacts, but their manifest status and reason fields have different
    semantics.
- Test plan:
  - After a completed demo, assert `manifest["frame_counts"]` exists.
  - Assert each count matches the corresponding saved `.npz` primary field
    length, for example `ft300.frame_id`, `xense.frame_id`,
    `realsense.topic`, and `zmq.seq`.
  - For discard, assert the manifest is written before buffers are cleared and
    contains `status: "discarded"`.
  - For discard, assert high-frequency `.npz` files are not saved and the
    manifest records an empty `npz` value.
- Risks and notes:
  - Compute completed-demo frame counts before clearing `demo_store`.
  - For discard, compute `frame_counts` before `discard_demo()` clears
    `demo_store`.
  - The current `discard_demo()` flow has a viable insertion point after sensor
    discard / rosbag stop and before `self.demo_store = None`.
  - Avoid reading `.npz` files just to compute counts during normal runtime; use
    in-memory buffer lengths before save.
  - Keep `discarded` and `failed` separate: `discarded` expresses user intent,
    while `failed` expresses an unsuccessful system transaction.
  - Start/resume transaction failure is owned by task 4 and should not be routed
    through the user-discard manifest path except for shared helper code.
  - Finding 14 is merged into this task as the document-policy clarification for
    discarded demo manifests.
- Suggested execution order: after runtime state-machine fixes, before final
  manifest/documentation cleanup

### 9. Replace tautological ZMQ buffer isolation assertion

- Strategy: Fix tests. Replace the tautological assertion in the consecutive
  demo mock runtime test with a real check that the second demo has an
  independent ZMQ buffer and persisted `.npz`.
- Involved files:
  - `MainController/src/MainController/test/test_maincontroller_mock_runtime.py`
- Test changes:
  - Remove `or second_demo_dir.exists()` from the assertion.
  - Prefer checking persisted data after the second demo is finished:
    - load `first_demo_dir / "zmq_telemetry.npz"`
    - load `second_demo_dir / "zmq_telemetry.npz"`
    - assert both contain rows
    - assert the second demo's first `seq` is greater than the first demo's last
      `seq`, or otherwise assert the saved row sets are not identical.
  - Optionally assert in-memory second demo buffer starts from an empty buffer
    before accumulating new rows.
- Test plan:
  - Run the mock runtime test file.
- Risks and notes:
  - The row count between two demos may coincidentally match, so row-count
    inequality alone is not a reliable isolation check.
  - Since the mock ZMQ publisher uses a monotonically increasing `seq`, comparing
    first/last saved `seq` ranges is a stronger assertion.
- Suggested execution order: with test hardening tasks

### 10. Add npz field-length consistency assertions

- Strategy: Fix tests. Add a helper that checks every array inside a saved
  `.npz` file has the same row count, and apply it to all MainController saved
  `.npz` outputs.
- Involved files:
  - `MainController/src/MainController/test/test_maincontroller_mock_runtime.py`
  - optionally `MainController/src/MainController/test/test_maincontroller_core.py`
- Test changes:
  - Add a helper such as `assert_npz_fields_same_length(npz)`.
  - The helper should:
    - assert `npz.files` is non-empty
    - take the first field length as the expected row count
    - assert every field has that same length
    - return the row count so existing minimum-count assertions can reuse it
  - Apply the helper to:
    - `ft300_timestamps.npz`
    - `xense_timestamps.npz`
    - `realsense_metadata.npz`
    - `zmq_telemetry.npz`
- Test plan:
  - Run the mock runtime test file.
- Risks and notes:
  - This task checks field lengths within each single `.npz` file. It must not
    assert equal row counts across FT300S, Xense, RealSense, and ZMQ files,
    because those streams have different rates and start timing.
  - Some arrays may have dtype `object` because fields can contain `None`; the
    check should compare `len(array)`, not dtype.
  - `TableBuffer.append()` already makes equal in-memory field lengths likely,
    but the saved artifact should be asserted directly.
- Suggested execution order: with test hardening tasks
