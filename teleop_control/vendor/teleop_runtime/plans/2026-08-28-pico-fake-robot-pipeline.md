# PICO direct-joint FakeRobot pipeline

Status: complete（离线基础已验证；后续真实 G2 单关节路线已冻结）

## What

- Problem / goal: Connect the verified PICO `JointJogIntent` output to the existing robot-independent direct-joint core in the required order: `ControlRouter -> DirectJointBackend -> SafetyGateway -> RobotAdapter`, using only `FakeRobot`.
- User-visible behavior: In explicit joint-jog mode, an accepted PICO jog updates exactly one simulated joint by `0.01 rad` and prints its before/target/after values. Selection-only or dead-man-released frames do not change simulated state. Rejected commands print a safety reason without changing state.
- Success criteria:
  - One reusable `DirectJointPipeline` reads state, routes the intent, validates the command, and only then calls `RobotAdapter.send_joint_command()`.
  - The pipeline uses the adapter's injected `JointSchema`; no robot name or vendor SDK appears in the generic core.
  - The PICO/FakeRobot app reuses `XRoboToolkitInputAdapter` and `JointJogMapper`, requires explicit `--mode joint-jog`, and reports both fake state updates and `physical_robot_command_sent=False`.
  - Offline tests prove an accepted single-joint jog updates only its named joint and a safety rejection leaves FakeRobot unchanged.
  - Full tests and a real PICO-to-FakeRobot run pass without importing GDK or connecting to physical hardware.
- Out of scope: G2/Leju live adapters, vendor SDK calls, physical motion, IK, video, recording changes, `SessionSupervisor`, and changing the Unity/PICO application.

## How

- Approach: Add a small orchestration class under `teleop.control` that owns a `RobotAdapter`, `ControlRouter`, and `SafetyGateway`, returning an immutable execution record. Add a separate finite-duration app that maps XR frames to intents and submits only emitted intents to a `FakeRobot` built from the current adapter schema.
- Components / files touched: `src/teleop/control/`, one new app, focused tests, README, and this plan. Existing PICO observers remain read-only and unchanged.
- Phases: Write failing pipeline tests; implement the minimal orchestrator; add/test the fake runtime entry point; run full offline and real PICO dry-run verification; reconcile this document.
- Risks / unknowns: A simulated state update proves software ordering and safety checks, not physical timing or hardware safety. Button ownership with the legacy recorder remains an operational precondition.
- Safety boundary: FakeRobot only; no `agibot_gdk`, network robot address, hardware command, physical G2 motion, or edits to the old `test-teleop`/Unity projects.
- Verification plan: Run focused and full `unittest` suites in both Python environments; compile all Python files; verify import isolation from robot SDKs; run the new app with real PICO gestures and observe named FakeRobot state changes while every line reports `physical_robot_command_sent=False`.
