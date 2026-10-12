# G2 live read-only RobotState adapter

Status: implemented (2026-08-28)

## Current result

- Implemented the live, read-only `G2Adapter.read_state()` path through an injected GDK state reader.
- Live mode advertises no joint-command capability and rejects every `send_joint_command()` call.
- Added lazy GDK startup/read/release handling and a namespace wrapper; neither contains a GDK write call.
- Verified all 62 tests with Python 3.13.13 and the GDK-compatible Python 3.10.20 environment.
- Verified against the connected G2 through `g2-network run`: GDK initialized, all 22 configured joints were read with finite positions, and the process reported `physical_robot_command_sent=False`.
- The observed snapshot contained the body, head, seven left-arm, and seven right-arm joints. No GDK write path was invoked and the robot did not receive a motion command.

## What

- Problem / goal: Replace the G2 dry-run adapter's injected state with real
  `agibot_gdk.Robot.get_joint_states()` telemetry while preserving the unified
  `RobotAdapter -> RobotState` boundary.
- User-visible behavior: A dedicated command, run through `g2-network run`, initializes
  GDK, reads and prints all 22 configured G2 position joints, releases GDK, and always
  reports that no physical robot command was sent.
- Success criteria:
  - `G2Adapter(dry_run=False, state_reader=...)` converts named finite
    `motor_position` rows into one immutable `RobotState` with a local monotonic receive
    timestamp, independent of GDK row order.
  - Missing, duplicate, malformed, or non-finite configured joint telemetry is rejected
    atomically; extra non-schema telemetry is ignored.
  - Live-read-only capabilities do not advertise joint command support and
    `send_joint_command()` always raises before calling any SDK write API.
  - Importing the adapter and CLI does not import GDK; the CLI lazily performs
    `gdk_init -> Robot -> read -> gdk_release`, including release on read failure.
  - Existing dry-run request-preview behavior remains unchanged and the complete
    offline suite passes.
  - A real `g2-network run` observation prints all 22 joints and
    `physical_robot_command_sent=False`.
- Out of scope: Any GDK write call, physical motion, PICO input, direct-joint execution
  on hardware, IK, URDF loading, gripper/chassis/camera control, and Leju integration.

## How

- Approach: Inject a narrow `G2JointStateReader` protocol into live `G2Adapter` so the
  adapter owns vendor-payload translation without importing native GDK during normal
  imports or tests. Add a small app that owns the real GDK lifecycle and prints one
  state snapshot. Keep dry-run construction and preview APIs compatible.
- Components / files touched: `teleop.robots.g2`, one read-only app, focused tests,
  README, and this feature doc.
- Phases: RED adapter/app tests; minimal implementation; full offline suite; real
  read-only G2 run inside `g2ns`; reconcile and review.
- Risks / unknowns: Real GDK rows may contain extra joints or a different row order;
  names are authoritative. Native GDK must always be released after initialization.
- Safety boundary: Real network and GDK reads are allowed; no method named
  `joint_control_request`, `joint_servo_control`, `move_ee_pos`, or equivalent may be
  called. No robot action or shared data is changed.
- Verification plan: Observe focused RED failures, pass focused and full unit tests,
  compile Python, prove import isolation, run the CLI via `g2-network run`, inspect 22
  named finite positions and the no-command marker, then perform an independent diff
  review against this document.
