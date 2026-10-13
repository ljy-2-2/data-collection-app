# G2 direct-joint dry-run adapter

Status: implemented (2026-08-27)

## What

- Problem / goal: Extend the existing robot-independent direct-joint core with a G2 adapter that exposes the 22 body/head/arm joints and translates validated commands into GDK-shaped request previews without importing GDK or touching hardware.
- User-visible behavior: The same `ControlRouter`, `DirectJointBackend`, and `SafetyGateway` can route a one-joint jog to `G2Adapter`; the adapter reports the expected G2 joint name, target position, velocity, and lifetime in dry-run output.
- Success criteria: G2 exposes 22 independently commandable joints with vendor limits; a `+0.01 rad` jog becomes a one-joint request preview; invalid, expired, forged, or malformed-time commands cannot reach the request preview; dry-run never mutates observed state or imports/calls `agibot_gdk`; the existing Phase 1 tests remain green.
- Out of scope: PICO button mapping, keyboard control, IK, grippers/end effectors, live GDK initialization, G2 network connection or motion, Leju implementation, and edits under `test-teleop/` or `Galaxis-VR-Teleop/`.

## How

- Approach: Keep the existing `RobotAdapter` interface. Add a G2-specific schema and a dry-run-only `G2Adapter` that stores a plain immutable request preview. Reuse the named-joint and safety path, harden its integer time-field contracts, and keep all GDK knowledge inside the adapter module.
- Components / files touched: `src/teleop/robots/g2.py`, robot exports, `contracts/command.py`, `contracts/robot_state.py`, `control/safety.py`, a G2 dry-run demo, focused tests, README, and this plan only.
- Phases: Write failing adapter/pipeline tests; implement the smallest adapter; run all tests and the offline demo; independently review the plan and resulting changes.
- Risks / unknowns: Vendor position limits are copied from the installed `mc_impl_config.json`; the temporary dry-run step/velocity values are not approval for live motion. A verified software stop/hold path and live joint velocity limits are still unknown.
- Safety boundary: No vendor SDK import, environment sourcing, network access, XR service, robot discovery, hardware command, or physical movement. `dry_run=False` must be rejected.
- Verification plan: Run unittest discovery; run the G2 dry-run demo and observe one `idx21_arm_l_joint1` preview at `0.31 rad`; compile all Python files; search new generic source for unintended vendor coupling; confirm no file outside `teleop-runtime/` changed.
- Verification result: Seventeen tests passed; the dry-run demo exposed 22 joints and translated `idx21_arm_l_joint1` from `0.30` plus `0.01` into a `0.31 rad` preview with `hardware_command_sent=False`; 23 Python files compiled; no `agibot_gdk` import was found; independent review reported no HIGH, MEDIUM, or LOW findings. Only files under `teleop-runtime/` were edited.
