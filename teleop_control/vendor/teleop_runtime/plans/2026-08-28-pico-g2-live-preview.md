# PICO-to-G2 live single-joint preview

Status: complete（真实 PICO + G2 只读预览已通过；后续真机路线已由 V1 里程碑取代）

## What

- Problem / goal: Join the verified PICO `JointJogIntent` path with live G2 state and
  GDK-shaped command preview while preserving the no-motion boundary.
- User-visible behavior: In explicit `joint-jog` mode, X/A selects a joint, holding
  right Grip and deflecting the right stick emits one `+0.01/-0.01 rad` intent, and the
  terminal prints the G2 current angle, checked target, and request preview ending in
  `physical_robot_command_sent=False`.
- Success criteria:
  - The PICO side reuses `XRoboToolkitInputAdapter` and `JointJogMapper`; native SDK
    diagnostics remain on stdout/stderr while a dedicated file descriptor carries only
    versioned, newline-delimited intent messages to the FIFO.
  - Each message contains a strictly increasing XR sequence, local monotonic emission
    time, one schema joint name, and exactly `+0.01` or `-0.01 rad`.
  - The G2 side initializes GDK once, reads live joint state for each accepted message,
    runs `ControlRouter -> DirectJointBackend -> SafetyGateway`, builds only a
    `JointControlReq` preview, and always releases GDK.
  - Malformed, replayed, future, or older-than-`200 ms` PICO messages and all shared
    safety failures are rejected without a preview or robot command.
  - The host PICO process and `g2ns` GDK process remain separate because the installed
    XR service/SDK uses host loopback `127.0.0.1:60061`; a supervised FIFO carries only
    robot-independent intent data across the namespace boundary. The launcher stops the
    peer if either side fails or the user presses Ctrl-C.
  - Both Python test environments, static no-write checks, and one real PICO + connected
    G2 preview session pass without physical movement.
- Out of scope: `robot.joint_control_request`, any physical G2 motion, continuous servo
  control, IK/URDF, video, Unity changes, recording changes, and Leju integration.

## How

- Approach: Add one dual-mode PC application. Its host-side emitter converts XR frames
  to validated JSON-line intent messages. Its namespace-side worker validates freshness
  and ordering, owns a read-only GDK lifecycle, and reuses the existing live-state
  preview builder. A small shell entry point connects and supervises the processes with
  a FIFO, leaving the XR SDK beside `RoboticsServiceProcess` and GDK inside `g2ns`.
- Components / files touched: One app, one read-only GDK worker wrapper, one launch
  script, focused tests, README, and this document. Core contracts and adapters remain
  unchanged.
- Phases: RED protocol/emitter/worker tests; minimal implementation; full offline
  verification; independent review; real combined preview observation.
- Risks / unknowns: GDK initialization may buffer early PICO messages; timestamps make
  those messages fail closed, so the user may need to repeat the gesture after the
  worker prints ready. Closing either side must terminate the pipe cleanly.
- Safety boundary: PICO and G2 are read-only inputs. The new files contain no robot SDK
  write call; GDK networking is entered only with the existing `g2-network run` tool.
- Verification plan: Test message boundaries, mapper emission, GDK lifecycle, state to
  preview values, stale/replay/error handling, process summaries, no-write AST scan,
  wrapper preflight, full suites in Python 3.13 and GDK Python 3.10, then observe one
  real Grip + right-stick gesture producing a live G2 preview with the false marker.

## Current evidence

- Offline implementation and review fixes are complete: GDK initialization/release
  failures are structured and always summarized; the launcher supervises both child
  sessions and stops/reaps them on error or Ctrl-C.
- `95/95` tests pass in both Python 3.13 and the GDK Python 3.10 environment.
- Shell syntax, Python compilation, and the static no-write scan pass.
- Rejected stale/future messages no longer advance replay state, and an adapter-stop
  failure can no longer skip `gdk_release()` or the structured worker summary.
- A real PICO-only observation received 1,457 fresh frames and captured controller
  activity, X, A, and a full right-stick deflection. A protocol run confirmed that SDK
  diagnostics no longer enter the intent channel. The same run reported
  `max_right_grip=0.000` because Trigger, not Grip, was pressed; this is not evidence of
  a Unity input bug.
- A corrected real Grip test received 1,456 fresh frames, measured
  `max_right_grip=1.000` and `max_abs_right_axis_y=1.000`, and emitted six strictly
  validated JSON intents containing both `+0.01` and `-0.01 rad`. The temporary Unity
  Grip changes were fully reverted; the existing headset APK works unchanged.
- 后续已完成真实 PICO + G2 只读预览，并继续完成单关节真机、统一会话和视频联合验收；当前权威状态见 `docs/milestones/pico-g2-teleoperation-v1.md`。
