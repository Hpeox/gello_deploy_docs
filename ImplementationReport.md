# Implementation Report

## Resume Instructions

If work is interrupted or context is compacted, resume by reading:

1. `FixPlan.md`
2. `ImplementationReport.md`
3. Relevant changed files for the current task
4. Latest recorded test output in this report

`FixPlan.md` remains the implementation source of truth. `review_report.md` is traceability only.

## Repository Boundaries

- `/home/robot/Desktop/gello-deploy` - outer planning/docs repository
- `/home/robot/Desktop/gello-deploy/MainController/src/main_controller` - MainController repository
- `/home/robot/Desktop/gello-deploy/FT300S` - FT300S repository
- `/home/robot/Desktop/gello-deploy/XenseTacSensor` - XenseTacSensor repository
- `/home/robot/Desktop/gello-deploy/RealSense` - RealSense repository

Initial relevant repository status: all relevant repositories were clean before implementation.

## Task Checklist

### Task 1: Xense UDS protocol magic differs from MainController client

- Status: done
- Affected repositories: MainController, FT300S, XenseTacSensor, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/uds_client.py`
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `ImplementationReport.md`
- Tests added or modified:
  - Updated `MockUdsSensor` to use `FT300S.protocol.messages` for FT300S and `XenseTacSensor.protocol.messages` for Xense.
  - Added assertions that FT300S-bound messages use `b"F3"` and Xense-bound messages use `b"XS"`.
- Tests run and results:
  - `python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py` failed before collection because the active Python environment has no `pytest` module. Environment failure, not a code failure.
  - `python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_mock_runtime.py` passed.
- Commit hashes:
  - MainController: `e36c6b29d4482c965b75a9562bb0292e6b14f64f`
  - FT300S: no file changes
  - XenseTacSensor: no file changes
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: `UdsClient` now supports per-client magic while preserving `b"F3"` defaults for existing helper calls. MainController instantiates FT300S with `b"F3"` and Xense with `b"XS"`.

### Task 2: PAUSED to finalizing/discarding has no real sensor ACK

- Status: done
- Affected repositories: MainController, FT300S, XenseTacSensor, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/uds_client.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `FT300S/core/service.py`
  - `FT300S/core/state.py`
  - `XenseTacSensor/core/service.py`
  - `XenseTacSensor/core/state.py`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added mock runtime coverage for `s -> p -> d` returning to `WAIT_START`.
  - Added mock runtime coverage for `s -> p -> x` returning to `WAIT_START`.
  - Added UDS client coverage for a relevant sensor `ERROR` waking a no-timeout ACK waiter.
- Tests run and results:
  - `python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py` failed before collection because the active Python environment has no `pytest` module. Environment failure, not a code failure.
  - `python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_mock_runtime.py FT300S/core XenseTacSensor/core` passed.
- Commit hashes:
  - MainController: `7ca9023fa0c9719c408516e0df8649750d97fb66`
  - FT300S: `b366fe25fff82373689b9e4a4258fe9451aa016b`
  - XenseTacSensor: `7e9c18da927f74020e468d727c9886f1f44b6f35`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Sensor services now accept `DEMO_DONE_REQ` and `DEMO_DISCARD_REQ` from `PAUSED`, transition to `WAIT_START`, and ACK. UDS ACK waits now return failure when a relevant sensor `ERROR` arrives instead of waiting indefinitely.

### Task 3: Startup failure does not perform ERROR to STOPPING cleanup

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added startup failure cleanup test covering `_wait_startup_ready()` failure after process/receiver setup.
  - Added managed-process startup rollback test covering failure while starting the second process.
- Tests run and results:
  - `python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py` failed before collection because the active Python environment has no `pytest` module. Environment failure, not a code failure.
  - `python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_mock_runtime.py` passed.
- Commit hashes:
  - MainController: `f2555f08db83bff70a375051ca7043e127421abb`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: `startup()` now logs `startup_failed`, transitions through `ERROR`, runs `stop_all()`, finishes in `STOPPED`, and re-raises. `_start_processes()` stops only processes that started successfully before propagating later startup failures.

### Task 4: START_REQ lacks a multi-sensor transaction boundary

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `MainController/src/main_controller/README.md`
  - `plan.md`
  - `implement_plan.md`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added start transaction rollback coverage for FT300S ACK followed by Xense `START_REQ` error.
  - Added paused resume transaction failure coverage that invalidates the paused demo and writes a failed manifest.
  - Added rosbag resume failure coverage that rolls back both started sensors and records failed rosbag state.
- Tests run and results:
  - `python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py` failed before collection because the active Python environment has no `pytest` module. Environment failure, not a code failure.
  - `python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_mock_runtime.py` passed.
- Commit hashes:
  - MainController: `2fd606fc4c9794816bfcd032c7693c38b27e123b`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Start/resume now uses an all-or-nothing transaction. Failures send `DEMO_DISCARD_REQ` to sensors that ACKed `START_REQ`, stop rosbag if recording had started, write a lightweight `status: "failed"` manifest, and clear the active demo context without saving high-frequency `.npz`.

### Task 5: ZMQ receiver fatal termination only logs a warning

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/zmq_telemetry.py`
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/test/test_maincontroller_core.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added receiver-level coverage that invalid ZMQ payloads call the warning path and the receiver continues to accept a later valid frame.
  - Added receiver-level coverage that an `on_frame` callback exception reports a fatal receiver failure.
  - Added controller coverage that ordinary ZMQ warning handling does not stop the controller.
  - Added controller coverage that a `zmq_fatal` command performs full cleanup and stops sensors, rosbag, receiver, RealSense monitor, and subprocesses.
- Tests run and results:
  - Previous interrupted validation used the active conda Python and `python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`, which failed with `No module named pytest`. That attempt is invalid for task validation because pytest was missing from that environment.
  - Valid MainController tests must be run from `/home/robot/Desktop/gello-deploy` after `conda deactivate`, using system Python:
    - `conda deactivate && python -m pytest MainController/src/main_controller/test/test_maincontroller_core.py -q`
    - `conda deactivate && python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q`
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_core.py MainController/src/main_controller/test/test_maincontroller_mock_runtime.py'` passed.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_core.py -q'` passed: `6 passed in 0.32s`.
  - The first sandboxed run of `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q'` failed with `PermissionError: [Errno 1] Operation not permitted` while binding the mock UDS Unix socket. This was an environment/sandbox failure, not a code behavior failure.
  - The same mock runtime command rerun outside the sandbox with approval passed: `15 passed in 14.02s`.
- Commit hashes:
  - MainController: `ad25c628a476ee5c3d0aea1ed7538909e8292264`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Current diff appears scoped to Task 5. Ordinary packet loss and invalid individual frames remain non-fatal; receiver-loop fatal termination is routed to MainController as a fatal event.

### Task 6: RealSense four-camera image-stream baseline and rosbag post-check

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/config.py`
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/main_controller/rosbag_control.py`
  - `MainController/src/main_controller/main_controller/realsense_image_guard.py`
  - `MainController/src/main_controller/test/test_maincontroller_core.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `MainController/src/main_controller/README.md`
  - `plan.md`
  - `implement_plan.md`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added formal required image-topic config coverage for 4 cameras / 8 image topics.
  - Added explicit `debug_degraded` subset config coverage.
  - Added rosbag metadata missing-topic/count-skew helper coverage.
  - Added mock runtime coverage that missing formal image readiness blocks recording and writes a failed manifest.
  - Added mock runtime coverage that rosbag image post-check failure marks the demo `failed`.
  - Added mock runtime coverage that `debug_degraded` mode uses and records its configured subset.
- Tests run and results:
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_core.py MainController/src/main_controller/test/test_maincontroller_mock_runtime.py'` passed.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_core.py -q'` passed: `9 passed in 0.33s`.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `18 passed in 18.65s`.
- Commit hashes:
  - MainController: `fe31d52bc7026e4f808576f08d86d475393b2a8c`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Formal mode fails closed on missing/mismatched required image topics. `debug_degraded` mode is explicit and uses only the configured subset. Demo manifests now include RealSense image readiness and rosbag image post-check results; post-check failure records `status: "failed"`.

### Task 7: Document drop-warning behavior for non-contiguous stream keys

- Status: done
- Affected repositories: outer docs repository
- Files changed:
  - `plan.md`
  - `implement_plan.md`
  - `ImplementationReport.md`
- Tests added or modified: none required
- Tests run and results: not run; documentation-only task.
- Commit hashes:
  - outer docs repository: `9e9e562e5f097bed38f2dbce43b5de93467f1906`
- Notes: Documented one `drop_warning` per detected non-contiguous key event, terminal and `controller_events.jsonl` emission, `missing_frame_count` / `warning_count` accumulation, large interval as a separate warning reason, manifest monitor summary fields, and no automatic pause/abort policy.

### Task 8: Manifest frame counts and discarded-demo status record

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/buffers.py`
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `MainController/src/main_controller/README.md`
  - `plan.md`
  - `implement_plan.md`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added completed-demo `frame_counts` assertions against saved `.npz` primary field lengths.
  - Updated discard flow coverage to assert a lightweight `status: "discarded"` manifest, empty `npz`, frame counts, and no high-frequency `.npz` artifacts.
- Tests run and results:
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_mock_runtime.py'` passed.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_core.py -q'` passed: `9 passed in 0.32s`.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `21 passed in 23.20s`.
- Commit hashes:
  - MainController: `a8b29ad513855376725b4964bcc52911d244f122`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Discard writes lightweight `manifest.json`; no high-frequency `.npz`. `discarded` remains reserved for completed user discard, while failed discard transactions are recorded as `failed` by task 11.

### Task 9: Replace tautological ZMQ buffer isolation assertion

- Status: done
- Affected repositories: MainController
- Files changed:
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `ImplementationReport.md`
- Tests added or modified:
  - Replaced the tautological `or second_demo_dir.exists()` assertion with persisted ZMQ `.npz` checks.
  - Asserted both demos save ZMQ rows and the second demo's first `seq` is greater than the first demo's last `seq`.
- Tests run and results:
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/test/test_maincontroller_mock_runtime.py'` passed.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `21 passed in 23.29s`.
- Commit hashes:
  - MainController: `c31a4d9b5a42ecb76ed90b2554934be4ffbf1d48`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Compare persisted ZMQ `.npz` ranges, not directory existence.

### Task 10: Add npz field-length consistency assertions

- Status: done
- Affected repositories: MainController
- Files changed:
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added `assert_npz_fields_same_length(npz)` helper.
  - Applied it to `ft300_timestamps.npz`, `xense_timestamps.npz`, `realsense_metadata.npz`, and `zmq_telemetry.npz`.
- Tests run and results:
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/test/test_maincontroller_mock_runtime.py'` passed.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `21 passed in 23.29s`.
- Commit hashes:
  - MainController: `f50751520ce29cfe5323c1b22de8e523b4fb9bfc`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Checks field lengths within each `.npz`, not across streams.

### Task 11: Partial failures for pause, finish, and discard commands

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/uds_client.py`
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `MainController/src/main_controller/README.md`
  - `plan.md`
  - `implement_plan.md`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added pause partial failure coverage that writes a failed manifest and stops the controller.
  - Added finish partial failure coverage that avoids `status: "done"`, records per-sensor command results and saved files, and stops the controller.
  - Added discard partial failure coverage that writes `status: "failed"`, not `discarded`.
- Tests run and results:
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_mock_runtime.py'` passed.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_core.py -q'` passed: `9 passed in 0.32s`.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `21 passed in 23.37s`.
- Commit hashes:
  - MainController: `62bb0fc47bc4a75967f5427d24a02251cfb80a83`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Start/resume rollback remains task 4. This task covers non-start command transactions and keeps `done`, `discarded`, and `failed` semantics distinct.

### Task 12: Paused resume rollback must invalidate every paused demo sensor

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `MainController/src/main_controller/README.md`
  - `implement_plan.md`
  - `ImplementationReport.md`
- Tests added or modified:
  - Updated paused resume failure coverage so Xense receives `DEMO_DISCARD_REQ` after returning `ERROR` to resume `START_REQ`.
  - Added paused resume failure coverage where FT300S fails before Xense receives `START_REQ`; both paused-context sensors receive discard rollback.
  - Added unconfirmed rollback coverage where a discard rollback returns `ERROR`; manifest records `rollback_unconfirmed_sensors` and the controller ends in `STOPPED`.
  - Preserved fresh-start rollback coverage so a sensor that never owned the new demo is not discarded unnecessarily.
- Tests run and results:
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_mock_runtime.py'` passed.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q -k "start_transaction_rolls_back or resume_transaction_failure or rosbag_resume_failure or realsense_readiness_failure"'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `5 passed, 18 deselected in 7.72s`.
- Commit hashes:
  - MainController: `12da18df7da2e2188689299abe5d666c92323265`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Fresh-start rollback remains scoped to sensors that ACKed `START_REQ`; paused-resume rollback targets every required sensor that already held the paused demo context. Unconfirmed rollback cleanup routes through `ERROR -> STOPPING -> STOPPED`.

### Task 13: Rosbag pause and finish stop failures are transaction failures

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `MainController/src/main_controller/README.md`
  - `implement_plan.md`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added rosbag pause failure coverage after both sensors ACK `PAUSE_REQ`; manifest records `rosbag_pause.ok == False` and final state is `STOPPED`.
  - Added finish-time rosbag stop failure coverage after both sensors ACK `DEMO_DONE_REQ`; manifest is `status: "failed"` and records `rosbag_stop.ok == False`.
  - Asserted finish stop failure preserves available sensor `saved_file` values, saves controller `.npz`, and skips RealSense rosbag post-check.
  - Existing pause, finish, and discard partial-failure tests now align with per-operation command result shape.
- Tests run and results:
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_mock_runtime.py'` passed.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q -k "pause_partial_failure or rosbag_pause_failure or finish_partial_failure or finish_rosbag_stop_failure or discard_partial_failure"'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `5 passed, 20 deselected in 7.78s`.
- Commit hashes:
  - MainController: `f7a2fa84f6963693627cd05a9f32192b251a2c5b`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: `done` now requires sensor finish, rosbag stop, and required post-check success. Rosbag pause/stop failures write failed manifests with detailed command results and route through fatal cleanup.

### Task 14: UDS finalization wait must not hang forever

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/main_controller/uds_client.py`
  - `MainController/src/main_controller/main_controller/main.py`
  - `MainController/src/main_controller/main_controller/config.py`
  - `MainController/src/main_controller/test/test_maincontroller_mock_runtime.py`
  - `MainController/src/main_controller/README.md`
  - `implement_plan.md`
  - `ImplementationReport.md`
- Tests added or modified:
  - Added mock sensor mode that receives `DEMO_DONE_REQ` and sends neither ACK nor ERROR while keeping the socket open.
  - Added mock sensor mode that closes the UDS connection after `DEMO_DONE_REQ`.
  - Added finish coverage for bounded flush timeout (`ack_timeout`) and peer disconnect (`uds_disconnected`), both ending in `STOPPED` with failed manifests.
- Tests run and results:
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_mock_runtime.py'` passed.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q -k "no_ack_times_out or peer_disconnect_wakes_ack_waiter or finish_partial_failure"'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `3 passed, 24 deselected in 4.88s`.
- Commit hashes:
  - MainController: `3b88ac50b87dd236193682ac57e74feec54f9105`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: `RuntimeConfig.sensor_flush_timeout_s` defaults to a finite `300.0` seconds and can be set to `None` via CLI `--sensor-flush-timeout-s none` / `unbounded`. Peer disconnect and send/ACK timeout now populate structured command errors for manifest reporting.

### Task 15: Close follow-up coverage gaps

- Status: done
- Affected repositories: MainController, outer docs repository
- Files changed:
  - `MainController/src/main_controller/README.md`
  - `implement_plan.md`
  - `ImplementationReport.md`
- Tests added or modified:
  - Task 12 regression coverage was added with paused resume rollback tests in task 12.
  - Task 13 regression coverage was added with rosbag pause/stop failure tests in task 13.
  - Task 14 regression coverage was added with no-ACK flush timeout and UDS peer disconnect tests in task 14.
  - README and implementation plan now state that physical four RealSense availability remains a hardware acceptance boundary.
- Tests run and results:
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_core.py -q'` passed: `9 passed in 0.32s`.
  - `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `27 passed in 32.52s`.
- Commit hashes:
  - MainController: `27f7499530fd18dbfb78520f91cce7b20513c6ce`
  - outer docs repository: report update is committed separately because a commit cannot contain its own hash.
- Notes: Mock/non-hardware tests validate configured formal-mode requirements and fail-closed behavior, not physical camera availability. Real four-camera acceptance remains open.

## Current Work

- Current task: none
- Current state: FixPlan tasks 1-15 are implemented, validated, and committed.

## Final Validation

- `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m compileall MainController/src/main_controller/main_controller MainController/src/main_controller/test/test_maincontroller_core.py MainController/src/main_controller/test/test_maincontroller_mock_runtime.py FT300S/core XenseTacSensor/core'` passed.
- `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_core.py -q'` passed: `9 passed in 0.32s`.
- `bash -lc 'source /home/robot/miniconda3/etc/profile.d/conda.sh; conda deactivate; python -m pytest MainController/src/main_controller/test/test_maincontroller_mock_runtime.py -q'` passed outside the sandbox with approval because the mock UDS server needs Unix socket bind: `27 passed in 32.52s`.

## Unresolved Risks and Follow-up Notes

- Broad integration or hardware acceptance tests may not be runnable in this environment.
- Real four-camera RealSense acceptance remains open until a formal capture proves all configured color and aligned-depth image topics are present and recorded.
- Network access is restricted; dependency installation is not assumed.
