# G2 direct-joint milestone freeze

Status: implemented (2026-08-31)

## What

- Problem / goal: accept and freeze the PICO-to-G2 direct-joint path for the 22 GDK position joints, then make the robot adapter's joint metadata and capabilities explicit enough for another vendor adapter.
- User-visible behavior: a read-only acceptance report lists all 22 G2 joints, their groups, live positions, position limits, step caps, velocity caps, and pass/fail status; no acceptance check sends a motion command.
- Success criteria: all 22 configured joints are ordered and uniquely named; each exposes position, step, velocity, group, and independent-command metadata; adapter capabilities truthfully distinguish dry-run, read-only, and live modes; the complete test suite passes; the architecture status diagram distinguishes implemented, partial, and planned components.
- Out of scope: automatic 22-joint motion, collision-free motion planning, chassis/wheels, grippers/hands, Leju SDK implementation, URDF/vendor IK implementation, Unity changes, camera streaming, and claiming physical movement for a joint without an observed manual test.

## How

- Approach: keep the existing architecture and promote its existing `JointSchema` and `RobotCapabilities` objects into the stable adapter contract. Add missing velocity/group metadata and convenience discovery methods, enforce capability consistency, add a read-only G2 contract acceptance entry point, and document the frozen evidence and remaining manual checks.
- Components / files touched: `src/teleop/contracts/`, `src/teleop/robots/`, focused unit tests, a read-only app/wrapper under `apps/`, `README.md`, and architecture/milestone artifacts under `docs/`.
- Phases: contract tests first; minimal implementation; full offline suite; read-only app verification; manual low-speed physical acceptance only after a separate current confirmation.
- Risks / unknowns: the installed public GDK config exposes per-joint limits but not a complete collision or coupled-constraint model; only the confirmed left-wrist coupling rule is encoded. Therefore the acceptance app must remain read-only and physical per-joint checks must be human-supervised.
- Safety boundary: no Unity edits, no legacy-project edits, no route changes, and no automatic physical motion. A physical check, if approved later, uses existing caps (arms `0.02 rad`/`0.2 rad/s`; body/head `0.01 rad`/`0.1 rad/s`), Grip release and stick-centering as deadman stop, long-B as controller exit, and `Ctrl-C` as terminal stop.
- Verification plan: demonstrate RED then GREEN for contract tests; run all unit tests; run the read-only acceptance CLI with test doubles; optionally run it against live GDK state; inspect PNG/SVG rendering; report physical checks separately from software evidence.
