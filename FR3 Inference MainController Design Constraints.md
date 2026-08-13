# FR3 Inference MainController Design Constraints

This document defines **implementation-independent constraints** for the FR3 inference MainController.

It is the source of truth for implementation planning. Existing code must be evaluated against these constraints; existing implementation details do not override them.

---

## 1. Scope and ownership

### MainController MUST

- own the inference **session lifecycle**;
- own the **rollout lifecycle**;
- own recording lifecycle and rollout/demo bookkeeping;
- act as the **primary lifecycle scheduler** for persistent LeRobot;
- launch and supervise its required workstation-side subprocesses:
  - RealSense runtime;
  - Xense runtime;
  - FT300S runtime;
  - rosbag recorder;
  - persistent LeRobot worker;
- classify faults as rollout-level recoverable or session-level fatal;
- decide whether session termination uses `SHUTDOWN` or `FAIL_STOP`.

### MainController MUST NOT

- own or directly supervise the SensorHub process;
- own or restart NUC runtime processes;
- implement automatic NUC recovery/supervision in v1;
- own policy inference state;
- choose or generate FR3 `q_reset`.

---

## 2. Persistent LeRobot worker

The LeRobot worker MUST remain alive across multiple rollouts within one inference session.

Policy/model loading, `Robot::FR3`, and SensorHub SHOULD remain persistent across rollouts.

Normal rollout completion or rollout-level abort MUST NOT require restarting LeRobot.

---

## 3. Lifecycle scheduling

MainController is the **primary scheduler**.

Ordinary lifecycle commands include at least:

```text
INITIALIZE
START
ABORT / STOP rollout
```

At most **one ordinary lifecycle transaction** may be outstanding at any time.

Normal transaction semantics MUST be:

```text
send REQUEST(seq)
→ receive ACK(seq, accepted/rejected)

if ACK = accepted:
    wait for completion STATUS
    → update MainController state
    → only then schedule the next ordinary lifecycle transaction

if ACK = rejected:
    transaction ends immediately
    → no completion STATUS is expected
```

### ACK semantics

`ACK` is the immediate response sent by LeRobot after receiving a UDS command.

It indicates whether the request was accepted in the current lifecycle phase.

`ACK(accepted)` means the operation has been accepted and MainController MUST wait for its completion `STATUS`.

`ACK(rejected)` ends the transaction immediately; no completion `STATUS` is expected.

`ACK` MUST NOT be interpreted as command completion.

### STATUS semantics

`STATUS` reports lifecycle state transition/completion after the accepted operation has actually completed.

For an accepted ordinary lifecycle transaction:

```text
REQUEST → ACK(accepted) → operation → completion STATUS
```

MainController MUST use the completion `STATUS` as the authoritative signal that the accepted lifecycle operation has finished.

This design does NOT add new ACK/STATUS liveness timeout requirements in v1; existing internal operation timeouts and operator supervision are sufficient.

---

## 4. Input handling and deferred execution

MainController MUST validate user/input actions against its **current state at the time the input is received**.

An invalid command MUST be rejected immediately and MUST NOT be queued for execution in a future state.

Example:

```text
INITIALIZING
+ user requests START
→ reject locally
→ do not send START to LeRobot
```

There MUST NOT be:

```text
START requested during INITIALIZING
→ remember START
→ automatically execute after INITIALIZE completes
```

MainController may use implementation-level synchronization primitives or a thin input-thread handoff mechanism, but these MUST NOT create a deferred lifecycle-command scheduler.

---

## 5. LeRobot command handling

LeRobot MUST validate every received command against its current lifecycle phase.

For ordinary lifecycle commands:

```text
new request valid in current phase
→ ACK(accepted)
→ execute now

new request invalid in current phase
→ ACK(rejected)

stale / duplicate packet
→ may be silently discarded
```

A newly received request that is invalid in the current lifecycle phase MUST receive a negative `ACK`.

Only stale/duplicate packets, or packets removed by defensive phase-boundary drain, may be silently discarded.

LeRobot MUST NOT:

- maintain an ordinary lifecycle command queue;
- defer an ordinary command until a future lifecycle phase;
- act as a secondary lifecycle scheduler.

Transport-buffer drain MAY be used at phase boundaries to discard stale or duplicate packets.

Such drain MUST be treated only as **defensive phase-boundary cleanup**, not as normal command scheduling or ordering logic.

---

## 6. INITIALIZE semantics

`INITIALIZE` is a preparation step for the **next rollout**, not cleanup of the previous rollout.

Normal lifecycle:

```text
WAIT_START
→ send INITIALIZE REQUEST
→ ACK(accepted)
→ INITIALIZING / initialization operation
→ completion STATUS
→ READY
```

MainController owns **when** `INITIALIZE` occurs.

LeRobot owns:

- `q_home`;
- concrete `q_reset`;
- reset/randomization configuration;
- reset randomization state/seed.

MainController MUST NOT generate or pass `q_reset`.

The lower FR3 execution path may map:

```text
INITIALIZE
→ LeRobot selects q_reset
→ FR3-specific initialize/reset
→ FRCMD1 RESET_JOINT(q_reset)
→ NUC executes q_reset
```

NUC MUST execute the concrete `q_reset` deterministically and MUST NOT perform reset randomization itself.

---

## 7. RESETTING ownership and INITIALIZE completion

`RESETTING` is part of the lower FR3 execution/telemetry contract.

LeRobot / `Robot::FR3` is responsible for using the `RESETTING` telemetry state as needed to determine completion of the lower-level reset operation.

Conceptually:

```text
RESET_JOINT
→ RESETTING = 1
→ reset motion executes
→ RESETTING = 0
→ FR3 initialize operation completes
→ LeRobot emits lifecycle STATUS
```

MainController MUST NOT use the raw `RESETTING` transition as the authoritative completion signal for `INITIALIZE`.

MainController MUST use the **LeRobot lifecycle completion `STATUS`**.

This preserves the abstraction boundary:

```text
RESETTING
= FR3 execution detail

STATUS
= MainController lifecycle contract
```

---

## 8. Rollout lifecycle

The controller MUST support repeated explicit rollout cycles:

```text
WAIT_START
→ INITIALIZE
→ READY
→ START
→ RUNNING
→ WAIT_START
```

There MUST NOT be an implicit fixed sequence such as:

```text
rollout end
→ sleep
→ automatic reset
→ next rollout
```

A future convenience operation such as:

```text
NEXT = INITIALIZE + START
```

MAY exist, but internally MUST preserve the same two lifecycle transactions.

---

## 9. WAIT_START semantics

`WAIT_START` means that the current rollout has ended and MainController is ready to accept the next valid operator lifecycle action.

Entering `WAIT_START` MUST NOT imply that NUC runtime is healthy or ready for `INITIALIZE`.

After a NUC-side failure:

```text
rollout abort
→ WAIT_START
```

the operator is responsible for confirming that any required manual NUC recovery has completed before requesting `INITIALIZE`.

MainController is NOT required to implement an automatic NUC-readiness gate in v1.

Therefore:

```text
WAIT_START
≠ NUC_READY
```

and v1 MUST NOT require additional states such as:

```text
WAIT_NUC
NUC_READY
GROUP_READY
```

---

## 10. SensorHub ownership and persistence

SensorHub MUST remain owned by `Robot::FR3`.

MainController MUST NOT:

- launch SensorHub directly;
- supervise SensorHub PID directly;
- restart SensorHub independently.

SensorHub SHOULD remain session-persistent and continue to own:

- local sensor SHM readers;
- NUC FGT1 telemetry subscription;
- alignment caches;
- aligned-observation SHM publication.

Temporary NUC robot/gripper telemetry loss MUST NOT by itself make SensorHub fatal.

During such an outage:

```text
NUC telemetry becomes stale
→ SensorHub remains alive
→ aligned publication stops
→ latest_sequence stops advancing
```

When telemetry returns, normal cache update and alignment SHOULD resume without:

- cache epoch reset;
- SHM recreation;
- SensorHub restart;
- runtime generation IDs.

---

## 11. SensorHub fatal propagation

SensorHub-originated fatal conditions MUST be surfaced through **LeRobot's existing fatal handling path**.

Conceptually:

```text
SensorHub fatal
→ Robot::FR3 / LeRobot observes fatal
→ LeRobot fatal handling
→ LeRobot session terminates
→ MainController observes LeRobot/session failure
```

MainController is NOT required to directly supervise SensorHub or independently reproduce SensorHub fault classification.

MainController MAY read aligned-SHM fatal information where useful for health observation, but this does not change SensorHub process ownership or fatal propagation ownership.

---

## 12. MainController aligned-SHM contract

MainController MUST use the aligned-observation SHM as the final observation-stream health gate during rollout execution.

It only requires header-level information, at minimum:

```text
ready
latest_sequence
fatal
```

MainController SHOULD use a formal header-only/read-only API rather than duplicating SHM ABI offsets.

MainController MUST NOT need to copy the full aligned observation payload solely for watchdog operation or raw recording.

---

## 13. NUC telemetry

NUC FGT1 telemetry MUST support fan-out to both:

```text
SensorHub
MainController
```

MainController's direct FGT1 subscription is used for:

- raw low-dimensional telemetry recording;
- explicit runtime/control flags.

Robot telemetry flags include:

```text
bit 0 = RESETTING
bit 1 = JUMP_HOLD
```

MainController MUST NOT turn the FGT1 stream into a general automatic NUC recovery protocol.

---

## 14. Rollout-level recoverable faults

The following events are rollout-level recoverable and MUST NOT by themselves terminate the inference session:

- user rollout abort;
- `JUMP_HOLD`;
- aligned `latest_sequence` stall;
- temporary NUC robot/gripper telemetry loss;
- NUC runtime failure that can be manually repaired before the next rollout.

Temporary NUC telemetry loss is not itself an automatic abort trigger. If it causes the aligned-observation health gate/watchdog to fail while `RUNNING`, the current rollout is aborted. Temporary NUC telemetry loss MUST NOT become a session fatal solely because the telemetry is temporarily unavailable.

Normal recovery semantics:

```text
RUNNING
→ ABORT current rollout
→ stop/fail current recording as appropriate
→ WAIT_START
```

No `SHUTDOWN` or `FAIL_STOP` is sent solely because of these events.

For NUC runtime failure:

```text
ABORT
→ WAIT_START
→ operator manually restores NUC runtime
→ operator confirms recovery
→ INITIALIZE
→ START
```

MainController MUST NOT introduce mandatory automatic NUC-readiness or restart logic in v1.

---

## 15. Aligned-observation watchdogs

### MainController rollout watchdog

MainController MUST monitor aligned `latest_sequence` while `RUNNING`.

If it stops advancing beyond the configured MainController timeout:

```text
ABORT current rollout
→ WAIT_START
```

The MainController stall threshold MUST be shorter than LeRobot's internal aligned-observation max-age fatal threshold.

Intended ordering:

```text
ordinary recoverable stall
→ MainController rollout ABORT first

persistent/unhandled stale observation
→ LeRobot internal max-age fatal fallback later
```

### LeRobot max-age watchdog arming

LeRobot's aligned-observation max-age fatal watchdog MUST only be armed in lifecycle phases that actively require fresh observations, at minimum:

```text
RUNNING
```

After successful rollout completion or successful `ABORT`, the RUNNING max-age watchdog MUST be disarmed.

Therefore ordinary observation inactivity while waiting between rollouts MUST NOT independently cause a stale-observation session fatal.

The exact set of additional phases requiring fresh observations MAY be determined by the LeRobot implementation, but the watchdog MUST NOT remain implicitly armed merely because the worker process is alive.

---

## 16. JUMP_HOLD

If MainController observes:

```text
JUMP_HOLD = 1
```

while `RUNNING`, it MUST abort the rollout immediately rather than wait for the aligned-stream timeout.

`JUMP_HOLD` MUST remain session-recoverable.

The next `INITIALIZE` may clear the latched hold through `RESET_JOINT`.

---

## 17. Session-level fatal faults

The following are session-level fatal in v1:

- RealSense fatal or unexpected required-process exit;
- Xense fatal or unexpected required-process exit;
- FT300S fatal or unexpected required-process exit;
- rosbag recorder fatal or unexpected required-process exit;
- SensorHub-originated fatal surfaced through LeRobot;
- MainController unrecoverable internal error;
- persistent LeRobot worker fatal/exit;
- LeRobot policy/runtime fatal;
- LeRobot reset / `INITIALIZE` failure;
- LeRobot aligned-observation max-age fatal fallback.

RealSense, Xense, FT300S, and rosbag recorder are required MainController-controlled subprocesses.

They MUST NOT be transparently **process-restarted after failure** inside the same inference session in v1.

---

## 18. Rosbag lifecycle clarification

The prohibition on rosbag restart applies specifically to **process recovery after rosbag recorder failure**.

It does NOT prohibit normal recording lifecycle operations between rollouts.

Normal rollout recording control through the existing recorder services (for example `~/record` and `~/stop`) is permitted between rollouts.

Therefore:

```text
normal rollout recording control
≠ recorder-process restart after failure
```

A rosbag recorder process fatal or unexpected process exit remains a session-level fatal condition.

---

## 19. Session termination contract

Two distinct session termination modes MUST exist.

### SHUTDOWN

`SHUTDOWN` MUST mean **user-requested normal session termination only**.

`SHUTDOWN` is only accepted where the existing lifecycle state permits it. It MUST NOT introduce deferred/latching shutdown handling.

While a synchronous blocking ordinary lifecycle operation such as `INITIALIZE` is in progress, MainController MUST reject a user `SHUTDOWN` request in the current state and MUST NOT send that rejected `SHUTDOWN` to LeRobot.

The operator MAY request `SHUTDOWN` again after the blocking operation settles.

LeRobot MUST NOT be modified to add concurrent message handling solely to make `SHUTDOWN` interrupt such an operation.

LeRobot receiving an accepted `SHUTDOWN` MAY:

```text
stop inference
→ return FR3 to configured q_home
→ graceful teardown
```

`SHUTDOWN` is the only termination mode that permits generation of a new return-to-home robot motion.

### FAIL_STOP

`FAIL_STOP` means:

> MainController has determined that the inference session must terminate because of a system fault.

On accepted `FAIL_STOP`, LeRobot MUST:

```text
stop inference
→ clean unfinished rollout/runtime state
→ perform fatal-style teardown
```

and MUST NOT generate a new return-to-home robot motion.

All MainController-detected session-level faults MUST map to `FAIL_STOP`.

A system failure MUST NOT be converted to graceful `SHUTDOWN` based on LeRobot's own judgement of telemetry or robot condition.

---

## 20. FAIL_STOP delivery semantics

`FAIL_STOP` is a session-termination intent, not an ordinary lifecycle transaction.

After MainController enters `FAIL_STOP` termination, it MUST transmit `FAIL_STOP` periodically until any of:

1. delivery is confirmed by a positive `ACK`;
2. LeRobot is confirmed to have already entered fatal teardown; or
3. the LeRobot process is confirmed to have exited.

A valid `FAIL_STOP` MUST NOT be rejected solely because of the current ordinary lifecycle phase.

Each retransmission MUST use the normal monotonically increasing UDS message sequence number:

```text
FAIL_STOP(seq=N)
FAIL_STOP(seq=N+1)
FAIL_STOP(seq=N+2)
...
```

The same sequence number MUST NOT be reused solely for retransmission.

### ACK behavior

A positive ACK for any `FAIL_STOP` transmission means:

```text
FAIL_STOP was received and accepted
```

and MainController SHOULD stop further retransmission.

However:

```text
FAIL_STOP ACK
≠ LeRobot teardown complete
```

After ACK, MainController MUST still wait for LeRobot process termination.

### Fatal-teardown / process-exit behavior

If LeRobot is confirmed to have already entered fatal teardown, or exits before MainController receives an ACK:

```text
fatal teardown confirmed OR LeRobot process exit
→ stop FAIL_STOP retransmission
```

If LeRobot is still alive after either a positive `ACK` or fatal-teardown confirmation, MainController MUST wait for process exit using the existing bounded timeout / force-terminate behavior.

The desired terminal intent is already established once `FAIL_STOP` is accepted or LeRobot is known to be in fatal teardown, so further retransmission is unnecessary.

Repeated `FAIL_STOP` packets MUST be safe/idempotent and MUST NOT be interpreted as queued lifecycle operations.

---

## 21. INITIALIZE and FAIL_STOP concurrency

`INITIALIZE` / reset is currently a synchronous blocking operation on the LeRobot side.

v1 MUST NOT require:

- a separate UDS receiver thread solely to interrupt reset;
- software preemption of an in-progress reset by `FAIL_STOP`.

If MainController enters `FAIL_STOP` while LeRobot is blocked in reset, it may continue periodically transmitting `FAIL_STOP`.

Conceptually:

```text
LeRobot executing synchronous reset
→ MainController enters FAIL_STOP
→ MainController retransmits FAIL_STOP(seq++)
→ reset returns or fails
→ LeRobot resumes control-message handling
→ accepts FAIL_STOP
→ ACK
→ fatal teardown
```

Immediate physical interruption of reset belongs to robot/entity safety mechanisms rather than this lifecycle protocol.

---

## 22. MainController / LeRobot failure interaction

If LeRobot itself has already entered fatal teardown or exited, MainController does not need to successfully deliver another `FAIL_STOP`.

It MUST classify the session as failed and clean up remaining workstation resources.

If MainController disappears unexpectedly before it can send `FAIL_STOP`:

```text
MainController control UDS disconnect
→ LeRobot treats controller loss as fatal
→ no-new-motion fatal teardown
```

No additional session-termination heartbeat protocol is required in v1.

---

## 23. Recording requirements

MainController owns rollout recording.

Raw NUC telemetry SHOULD continue to be recorded from MainController's direct FGT1 subscription.

RealSense, Xense, and FT300S recording remain independent of the aligned-observation payload.

A rollout-level abort MUST stop/fail the current recording consistently.

A recording subsystem fatal, including rosbag recorder process fatal, MUST escalate to session-level `FAIL_STOP`.

---

## 24. Teardown ordering

Session teardown MUST preserve:

```text
termination_mode = SHUTDOWN | FAIL_STOP
termination_reason
```

### SHUTDOWN ordering

```text
settle current rollout/recording
→ send SHUTDOWN to LeRobot
→ wait for graceful return-home + teardown
→ stop remaining workstation runtime
```

MainController MUST NOT destroy LeRobot/SensorHub dependencies before LeRobot completes graceful shutdown.

### FAIL_STOP ordering

```text
fail/stop current rollout
→ periodically send FAIL_STOP(seq++)
→ positive ACK OR fatal-teardown confirmation OR LeRobot exit stops retransmission
→ if LeRobot is still alive, wait for process exit
→ force terminate on bounded timeout if necessary
→ stop remaining workstation runtime
```

---

## 25. Explicit v1 non-goals

The implementation MUST NOT add the following unless the design is explicitly revised:

- ordinary lifecycle command queues;
- deferred execution of invalid lifecycle commands;
- deferred/latching `SHUTDOWN` handling;
- multiple simultaneous ordinary lifecycle transactions;
- MainController-owned `q_reset` generation;
- automatic NUC restart;
- automatic NUC-readiness gate;
- NUC supervisor/recovery handshake;
- `WAIT_NUC` / `NUC_READY` / `GROUP_READY` states;
- direct MainController supervision of SensorHub;
- SensorHub restart or generation/epoch mechanisms;
- local required-subprocess process restart after failure;
- rosbag recorder process restart after failure;
- separate UDS receiver thread solely for reset or `SHUTDOWN` preemption;
- software `FAIL_STOP` preemption of synchronous reset;
- MainController use of raw `RESETTING` as authoritative `INITIALIZE` completion;
- MainController full aligned-observation payload consumption solely for watchdog/recording.

Normal rollout recording control through the existing recorder services is NOT prohibited by the process-restart restriction.

---

## 26. Core invariants

```text
MainController
= primary lifecycle scheduler
= session / rollout / recording owner
= fault classifier
= SHUTDOWN vs FAIL_STOP authority
```

```text
Ordinary lifecycle transaction
= single outstanding
= REQUEST → ACK(accepted) → completion STATUS
= REQUEST → ACK(rejected) → transaction ends; no STATUS expected
= no deferred execution
```

```text
Invalid command handling
= invalid user/input action detected by MainController → reject locally, do not send
= new REQUEST received by LeRobot but invalid in current lifecycle phase → ACK(rejected)
```

```text
FAIL_STOP
= repeated REQUEST(seq++)
= retry until positive ACK, fatal-teardown confirmation, or process exit
= valid FAIL_STOP is not rejected solely due to ordinary lifecycle phase
= ACK confirms acceptance, not teardown completion
```

```text
SHUTDOWN
= user-requested normal termination only
= accepted only where current lifecycle state permits
= no deferred/latching handling
= no new concurrent receiver mechanism
```

```text
LeRobot
= persistent inference worker
= lifecycle-phase validator
= q_home / q_reset owner
= FR3 initialization executor
```

```text
RESETTING
= lower FR3 execution detail

INITIALIZE STATUS
= authoritative MainController completion signal
```

```text
LeRobot aligned max-age watchdog
= armed only when fresh observations are required
= disarmed after rollout completion / ABORT
```

```text
WAIT_START
= controller ready for operator action
≠ guarantee that NUC is ready
```

```text
SensorHub
= persistent aligned-observation service
= owned/supervised through Robot::FR3 / LeRobot
= fatal conditions surface through LeRobot
```

```text
NUC telemetry loss
= not an automatic abort by itself
= may cause rollout abort through aligned-observation health-gate failure
= not session-fatal solely because telemetry is temporarily unavailable
```

```text
NUC
= deterministic robot-command executor
= manually recovered when runtime fails
```

```text
rollout-level fault
→ ABORT → WAIT_START

session-level fault
→ FAIL_STOP

user normal session exit
→ SHUTDOWN
```