# PICO direct-joint intent mapper

Status: implemented (2026-08-28)

## What

- Problem / goal: Translate the existing robot-independent `XRFrame` stream into one-joint `JointJogIntent` values without IK, while preventing the PICO `X/A` buttons from simultaneously controlling recording or headset input switching.
- User-visible behavior: In explicit `JOINT_JOG` mode, `X` selects the previous commandable joint, `A` selects the next joint, and holding the right Grip while moving the right joystick up/down produces one `+0.01/-0.01 rad` intent. The PC observer prints the selected joint and intent but never creates or sends a robot command.
- Success criteria:
  - The mapper obtains ordered selectable joints from an injected `JointSchema`, so the same logic works with different robot adapters.
  - `X/A` use rising-edge detection; holding a button cannot skip repeatedly through the joint list, and wrap-around is deterministic.
  - A jog requires explicit `JOINT_JOG` mode, a fresh XR frame, a neutral-to-arm transition, right Grip at or above the configured threshold, and a new right-stick deflection outside the dead zone.
  - Leaving `JOINT_JOG`, stale input, simultaneous `X+A`, released Grip, or a held joystick produces no intent. Recording and observe modes never consume `X/A` as joint controls.
  - A read-only app defaults to `OBSERVE`; `--mode joint-jog` is required to enable mapping and warns that the old recorder must not run and the headset's `Switch with A` toggle must be off.
  - Unit tests cover multiple joint schemas, button conflicts, edge detection, dead-man behavior, stale/mode transitions, and exact `JointJogIntent` output; the complete offline suite remains green.
- Out of scope: recording implementation or changes, Unity/PICO changes, robot state/current-angle display, `ControlRouter`, `DirectJointBackend`, `SafetyGateway`, `FakeRobot`, `RobotAdapter.send_joint_command`, IK, GDK/Leju SDK calls, and physical robot motion.

## How

- Approach: Add a small stateful `JointJogMapper` under `teleop.retargeting`. It accepts only `XRFrame`, `JointSchema`, and an explicit `TeleopInputMode`; it owns button/joystick edge state and emits an immutable status containing an optional `JointJogIntent`. Add a thin `observe_joint_jog.py` app using the existing `XRoboToolkitInputAdapter` and G2's static dry-run schema only as a realistic joint menu.
- Components / files touched: `src/teleop/retargeting/`, focused tests, `apps/observe_joint_jog.py`, this plan, and the runtime README. Old `test-teleop/` and Unity files are read-only references and remain unchanged.
- Phases: Convert criteria into failing pure-logic tests; implement the mapper; add and test the read-only observer; run the full suite and a time-bounded PICO observation if the external XR stream is available.
- Risks / unknowns: Old `test-teleop/send.py` defaults to `A=record start, B=record stop`, and Unity exposes `Switch with A`. Process-level mutual exclusion with the legacy recorder is not yet centrally supervised, so the observer must make ownership explicit and cannot guarantee another independently launched legacy process is absent.
- Safety boundary: Code-only changes under `teleop-runtime`; no edits to old code, no recorder actions, no robot SDK imports, no robot connection, no command generation, and no hardware motion. A stale or non-neutral stream always disarms mapping.
- Verification plan: Follow RED/GREEN `unittest`; run all offline tests in both Python environments; compile source/apps/tests; verify importing and running the mapper does not load `agibot_gdk`; run the observer in default observe mode and, only if PICO timestamps are available, confirm `X/A`, Grip, and right-stick gestures print intents with `hardware_command_sent=False`.

## Result

- Confirmed the legacy conflicts from source: `test-teleop/send.py` defaults to `A=record start, B=record stop`, and the Unity UI exposes `Switch with A`. Neither legacy component was modified.
- Added the mode-aware `JointJogMapper`, neutral-to-arm handshake, `X/A` edge detection, Grip dead-man gate, joystick rearming, and one-step `JointJogIntent` output using an injected `JointSchema`.
- Added a read-only observer that defaults to `OBSERVE`, prints the X/A ownership warning before explicit `joint-jog` mode, reports unavailable robot state honestly, and always reports `hardware_command_sent=False`.
- RED failed because the mapper and observer did not exist; later review-driven RED tests reproduced unsafe close-state and false-success error-reporting gaps. GREEN now passes 40/40 tests in both Python environments, 36 Python files compile, and import isolation loads neither `xrobottoolkit_sdk` nor `agibot_gdk`.
- Live debugging found two simultaneous `RoboticsServiceProcess` instances sharing the SDK port. After stopping both and starting one clean instance, the observer received 5,905 fresh frames in 120 seconds. A second process then reconnected without restarting the service and received 123 fresh frames, confirming normal SDK close/reconnect behavior with a single service instance.
- The repaired joint-jog observer received 123 fresh frames in three seconds, reported `0 intents`, emitted the new no-intent diagnostic, and always reported `hardware_command_sent=False`. PICO logs showed head tracking advancing while controller IMU/button values remained inactive, so live A/X/Grip/stick gesture verification still requires the user to wake and operate the controllers; no robot connection is needed.
- Independent follow-up review approved the fixes with no remaining HIGH, MEDIUM, or LOW findings. Close-state, error-summary, exit-code, error-path, and threshold-boundary behavior is covered by regression tests.
- Final live controller verification superseded the earlier pending note: `X` wrapped from the first to the last joint, `A` returned to the first joint, one upward deflection emitted one `+0.010 rad` intent, and the user's two downward deflections emitted exactly two `-0.010 rad` intents. The run received 5,894 frames, emitted three intended intents, and reported `hardware_command_sent=False` throughout.
