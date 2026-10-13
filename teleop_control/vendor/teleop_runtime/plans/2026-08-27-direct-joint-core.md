# Direct joint control core

Status: implemented (2026-08-27)

## What

- Problem / goal: Build the first offline slice of the cross-robot teleoperation architecture: direct control of one, some, or all independently commandable joints without inverse kinematics.
- User-visible behavior: A fake robot accepts validated named-joint targets; invalid, stale, expired, excessive-jump, and out-of-limit commands are rejected with a clear reason.
- Success criteria: The same DirectJointBackend, SafetyGateway, and ControlRouter work with two FakeRobot joint schemas; single, partial, and all-joint targets pass; unspecified joints stay unchanged; common unsafe commands fail; the generic package imports no G2 or vendor SDK.
- Out of scope: PICO/XR input, physical G2 connection or motion, GDK, URDF IK, vendor IK, Leju SDK, video, recording, and changing the existing test-teleop baseline.

## How

- Approach: Define small standard contracts, an abstract RobotAdapter, an in-memory FakeRobot, a direct-joint backend, a router, and a single safety gateway. Use named joints and SI units. Build tests first, then run a small CLI demo through the complete offline path.
- Components / files touched: New files only under teleop-runtime/: pyproject.toml, README.md, src/teleop/, apps/, tests/, and this plan.
- Phases: Contracts and tests; minimal implementation; offline test and demo verification.
- Risks / unknowns: Real G2 joint capabilities and vendor limits are not inferred in this phase. FakeRobot proves software boundaries, not physical safety or timing.
- Safety boundary: No XR service, network robot connection, vendor SDK import, hardware command, existing dataset change, or edit under test-teleop.
- Verification plan: Run unittest discovery; run the offline demo and observe a valid joint jog update plus rejection of an unsafe target; search generic source for G2/GDK/vendor imports; confirm test-teleop remains untouched.
- Verification result: Seven tests passed. The demo changed `left_arm.j1` from `0.00` to `0.01` and rejected an out-of-limit target. All Python files compiled, no vendor term was found under `src/`, `tests/`, or `apps/`, and no file under `test-teleop/` was modified.
