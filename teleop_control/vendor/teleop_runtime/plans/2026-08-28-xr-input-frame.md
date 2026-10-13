# XR input adapter and frame contract

Status: implemented (2026-08-28)

## What

- Problem / goal: Connect the verified PICO/XRoboToolkit input stream to the robot-independent `teleop-runtime` without coupling the core to `xrobotoolkit_sdk` or to G2. Preserve the old `test-teleop/receive.py` timestamp/stale behavior and the XRoboToolkit paper's XR-side modular boundary.
- User-visible behavior: A read-only PC command prints normalized, typed snapshots containing the PICO head pose, left/right controller poses, joystick axes, grip, trigger, buttons, device timestamp, receive time, sequence number, and stale state.
- Success criteria:
  - Immutable contracts validate finite OpenXR poses, joystick ranges, analog ranges, integer timestamps, and left/right button mappings.
  - An `XRInputAdapter` protocol isolates the core from a concrete input SDK.
  - `XRobotoolkitInputAdapter` lazily initializes the SDK, returns `None` before the first valid timestamp, and marks data stale after 0.3 seconds without timestamp progress.
  - Unit tests use an injected fake SDK and do not require PICO, XR services, GDK, IK, or robot hardware.
  - The existing full offline test suite remains green.
  - A real observe-only run sees advancing timestamps and changing PICO pose/controller inputs without importing robot SDKs or producing commands.
- Out of scope: PICO-to-joint button mapping, `TeleopIntent`, retargeting, IK, `ControlRouter`, `DirectJointBackend`, `SafetyGateway`, `RobotAdapter`, G2/Leju SDK calls, video, hand/body/motion-tracker contracts, and any physical robot command.

## How

- Approach: Add immutable XR contracts under `teleop.contracts`, an input protocol plus XRoboToolkit implementation under `teleop.xr`, and one thin read-only observer under `apps/`. The concrete adapter uses the same SDK getters and 0.3-second stale rule as the old working receiver, while adding the previously omitted joystick axes and device timestamp. The v1 contract documents the paper's OpenXR right-handed XR-world pose convention; it does not perform robot-frame conversion. The installed pybind does not expose the paper's active input-mode field, so v1 does not fabricate one.
- Components / files touched: `src/teleop/contracts/xr.py`, `src/teleop/contracts/__init__.py`, `src/teleop/xr/`, `apps/observe_xr.py`, focused tests, this plan, and the runtime README.
- Phases: contracts and fake-SDK tests; adapter and observer; full offline regression; real PICO observe-only verification.
- Risks / unknowns: The vendor SDK has a known process-teardown issue and controller analog values can remain zero when PICO hand tracking is the active input. The observer must report this clearly and never interpret neutral values as robot commands.
- Safety boundary: Code-only changes under `teleop-runtime`; the old `test-teleop/` and Unity project remain unchanged. Verification may read the already-running local XR service and PICO stream, but must not initialize GDK, connect a robot, solve IK, or send a joint command.
- Verification plan: Run focused RED/GREEN `unittest` cases, the complete `teleop-runtime` suite, the observer with a fake SDK/error path, import checks proving no robot vendor module is loaded, and a time-bounded real PICO read-only observation of timestamp, pose, axes, trigger/grip, and buttons.

## Result

- Added immutable `Pose`, `ControllerButtons`, `ControllerState`, and `XRFrame` contracts plus the robot-independent `XRInputAdapter` protocol.
- Added `XRoboToolkitInputAdapter` with lazy SDK loading, explicit field validation, sequence and receive timestamps, and the old receiver's 0.3-second stale rule.
- Added `apps/observe_xr.py`; it only reports XR input and its summary explicitly states `hardware_command_sent=False`.
- Focused tests were written RED first, then passed GREEN. After independent review fixes, the complete suite passed 28/28 in both the default Python and the installed `xrobotoolkit_sdk` environment; bytecode compilation also passed.
- Import isolation showed neither `xrobotoolkit_sdk` nor `agibot_gdk` is loaded by importing `teleop.xr`.
- Real PICO observe-only verification received 1,953 frames with 0 stale frames and observed controller activity including `X`, `A`, and trigger values. No IK, robot SDK, or hardware command path was invoked.
- A later 3-second regression, while the current XR service supplied no valid timestamps, correctly returned `no valid headset timestamp` with 0 frames and `hardware_command_sent=False`; reconnecting that external stream was not required to validate the code-only review fixes.
