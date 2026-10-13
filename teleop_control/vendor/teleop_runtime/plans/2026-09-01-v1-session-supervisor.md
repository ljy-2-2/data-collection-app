# PICO + G2 Teleoperation V1 session supervisor

Status: implemented and live accepted (2026-09-01)

## What

- Problem / goal: close the verified direct-joint and robot-video work behind one supervised PC entry point instead of two manually managed terminals.
- User-visible behavior: one launcher performs PICO/XR preflight, then runs the existing G2 video and PICO direct-joint launchers together; one child ending stops the other and the session prints a final summary.
- Success criteria: physical control requires an explicit `--enable-motion` flag; failed PICO/XR preflight never starts either G2 child; the video child runs without its own duplicate PICO watcher; signals and normal child exit clean both children; unit tests cover refusal, preflight failure, normal completion, child failure, and signal cleanup; the complete runtime suite stays green; architecture and milestone documents match the verified V1 state.
- Out of scope: changing Unity, `test-teleop`, joint mapping, safety limits, GDK command behavior, video protocol, IK, locomotion, Leju, recording, platform integration, or performing a new physical motion test in this change.

## How

- Approach: add a thin Bash `SessionSupervisor` launcher that composes the existing launchers and accepts injectable commands for isolated tests. It runs the existing PICO connector and XR observer before obtaining G2 privileges, starts video first and then live direct-joint control, and owns signal/child cleanup. Keep video on `63902` and XR/control on `63901`; do not merge their data paths.
- Components / files touched: `apps/run_pico_g2_teleop.sh`, focused launcher tests, `README.md`, V1 milestone/status plans, and `docs/architecture/` source plus generated SVG/PNG.
- Phases: test-first supervisor implementation; offline verification; documentation and architecture freeze.
- Risks / unknowns: Bash child-process cleanup must not leave a live GDK worker. Seven focused tests cover cleanup and failure paths; the operator also completed the authorized 60-second unified-entry live acceptance without a reported problem.
- Safety boundary: this implementation and verification do not connect to or command G2. The launcher fails closed without `--enable-motion`; tests inject fake connector, XR preflight, video, control, sudo, and network commands.
- Verification plan: observe each focused launcher test fail before implementation, make it pass, run Bash syntax checks and the complete Python suite, execute the real launcher without `--enable-motion` and confirm it refuses before PICO/G2 work, regenerate diagrams from source, and inspect the generated status SVG/PNG.

## Result

- RED: the first five focused tests failed because `apps/run_pico_g2_teleop.sh` did not exist. Two follow-up edge tests were then added; the unexpected clean PICO-watcher exit test exposed a false-success bug, which was fixed before freezing this stage.
- GREEN: all seven focused tests pass for explicit-motion refusal, exclusive session locking, PICO/XR preflight failure, normal control completion, early video failure, unexpected PICO-watcher exit, and signal cleanup of control, video, and the PICO watcher.
- The real launcher without `--enable-motion` exits `2` before PICO or G2 work. Five Bash launchers pass `bash -n`.
- The complete offline runtime suite passes `172/172` when local Unix/loopback sockets are permitted. The restricted sandbox's six socket `PermissionError` results were environment limitations and disappeared in the permitted rerun.
- On 2026-09-01 the operator completed the separately authorized 60-second PICO + G2 unified-entry acceptance and reported that video, direct-joint control, and session exit worked normally.
- `teleoperation-status` and `video-stream-status` sources plus SVG/PNG outputs were regenerated and visually inspected. Control, PICO video, and `SessionSupervisor` are frozen green for V1.
