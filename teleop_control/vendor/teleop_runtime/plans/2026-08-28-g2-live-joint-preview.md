# G2 live-state single-joint command preview

Status: implemented (2026-08-28)

## Current result

- Confirmed the installed GDK reference uses `JointControlReq` with `life_time`,
  `joint_names`, `joint_positions`, and `joint_velocities`, followed by the physical
  `robot.joint_control_request(...)` call. This feature models only the request fields
  and deliberately omits that final call.
- Added the real-state preview application and a side-effect-free namespace wrapper.
  The wrapper never sources the vendor `env.sh`; it refuses to start unless
  `g2-network run` has already supplied the required discovery environment.
- Verified 75 tests under Python 3.13 and GDK Python 3.10, including delayed expiry,
  invalid inputs, position limits, default and alternate joints, environment preflight,
  import isolation, and absence of robot write calls. Independent review found no
  remaining concrete defects.
- Verified through `g2-network run` against the connected G2. The observed
  `idx21_arm_l_joint1` position was `-0.163557 rad`; the shared direct-joint and
  safety path accepted `+0.01 rad` and produced target `-0.153557 rad` with
  `life_time=0.099833`, one matching joint name, velocity `0.1 rad/s`, and
  `physical_robot_command_sent=False`.

## What

- Problem / goal: Prove that one real G2 joint-state snapshot can travel through the
  robot-independent direct-joint path and become a GDK-shaped request without sending
  any physical command.
- User-visible behavior: A dedicated command reads the connected G2, applies a small
  named-joint delta, runs `ControlRouter -> DirectJointBackend -> SafetyGateway`, and
  prints the current angle, target angle, request fields, and
  `physical_robot_command_sent=False`.
- Success criteria:
  - The default preview selects `idx21_arm_l_joint1` and requests `+0.01 rad` from the
    freshly read motor position.
  - Joint name and delta are configurable, while unknown joints, zero/non-finite
    deltas, deltas larger than `0.01 rad`, stale state, limit violations, and expired
    commands are rejected before a preview is produced.
  - The preview matches the installed GDK `JointControlReq` fields: `life_time`,
    `joint_names`, `joint_positions`, and `joint_velocities`.
  - GDK is used only for `gdk_init`, `Robot`, `get_joint_states`, and `gdk_release`;
    the preview is built after release and no robot write method is reachable.
  - Happy and rejected paths explicitly report `physical_robot_command_sent=False`.
  - The full offline test suite passes under the default and GDK-compatible Python
    environments, and one connected-G2 run observes a real-angle-based preview.
- Out of scope: Calling `joint_control_request`, any physical motion, continuous PICO
  control, Grip/deadman wiring, IK/URDF work, gripper/chassis control, and other robots.

## How

- Approach: Reuse the verified read-only GDK lifecycle from `read_g2_state.py`. After
  GDK has been released, seed the existing dry-run `G2Adapter` with the observed
  positions, route one `JointJogIntent`, validate it, and call only the pure
  `build_request_preview()` method.
- Components / files touched: One preview app and namespace wrapper, focused tests,
  README, and this feature document. Existing control contracts remain unchanged.
- Phases: RED tests; minimal preview implementation; full offline verification;
  connected-G2 read-and-preview observation; independent review.
- Risks / unknowns: The chosen joint may already be within `0.01 rad` of a position
  limit, so the safety gateway must reject rather than clamp or silently change it.
- Safety boundary: The connected robot is read-only. Preview construction occurs only
  after GDK release, and this feature must contain no SDK command call.
- Verification plan: Test exact lifecycle and request fields with a fake read-only GDK,
  test rejected inputs and CLI markers, scan the new runtime path for SDK write calls,
  run the complete suite in both Python environments, then ask the user to execute one
  `g2-network run` preview and inspect the no-command marker.
