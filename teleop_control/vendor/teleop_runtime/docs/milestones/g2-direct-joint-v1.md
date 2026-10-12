# G2 direct-joint milestone v1

Frozen: 2026-08-31

## Frozen scope

This milestone covers the PICO controller path for the 22 position joints exposed by the installed G2 GDK configuration:

- body: 5 joints (`idx01`-`idx05`)
- head: 3 joints (`idx11`-`idx13`)
- left arm: 7 joints (`idx21`-`idx27`)
- right arm: 7 joints (`idx61`-`idx67`)

It does not claim chassis/wheel, gripper/hand, IK, Leju, camera-stream, collision-free planning, or automatic full-body motion support.

## Frozen adapter contract

- `get_capabilities()` reports state-read, preview/direct-joint, physical-delivery, IK, base, and end-effector abilities without importing vendor details into the core.
- `get_joint_schema()` owns the ordered joint schema.
- `list_joints()` returns the adapter-owned controller menu.
- `get_joint_info(name)` returns position limits, step cap, velocity cap, body group, and independent-command status.
- `read_state()` returns a named state snapshot.
- `send_joint_command()` accepts only a `ValidatedJointCommand`.
- `solve_ik()` remains an adapter-owned extension point; the default URDF solver and vendor overrides are not part of this milestone.

## Evidence

- The full PICO-to-G2 direct-joint route has issued real GDK requests in an operator-supervised session.
- The operator log showed all 22 names selected in order and wrapped from `idx67_arm_r_joint7` to `idx01_body_joint1`.
- Long-B produced `PICO_G2_LIVE_EXIT`, and the session summary reported `exit_requested=True`.
- A real GDK rejection confirmed the left-wrist constraint `-q26 + q27 <= 1.92`; the adapter now rejects above the `1.919 rad` software threshold before GDK and keeps the session alive.
- Automated contract and runtime tests cover schema order, limits, command freshness, step/velocity caps, Grip release, stale input, mailbox overwrite, long-B exit, and known coupling rejection.

## Acceptance boundary

The read-only acceptance command below checks that all 22 live states exist, are finite, and lie inside their configured position limits. It never sends a command:

```bash
sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_joint_acceptance.sh --settle-seconds 2
```

Success ends with:

```text
G2_JOINT_ACCEPTANCE_SUMMARY passed=True joints=22 physical_robot_command_sent=False
```

A 22-joint movement checklist remains human-observed rather than automatic because the runtime does not yet have a complete self-collision/workspace model. Until that checklist is recorded, the precise claim is “all 22 joints are discoverable and safely addressable; real direct-joint motion is proven on the path, not exhaustively documented for both directions of every joint.”

## Stop and rollback boundary

- Release Grip or center the right stick: stop producing new joint commands.
- Hold B for one second with neutral controls: exit the controller mode.
- Press `Ctrl-C`: stop the PC session and release GDK.
- No schema/acceptance command changes robot firmware, Unity, routes, calibration, or persistent robot settings.
