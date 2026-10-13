#!/usr/bin/env python3
"""Dual-arm XR teleoperation with button-controlled episode recording.

Pipeline per 100 Hz tick:
    receive.py (controller) -> retarget.py (EE target in base_link)
    -> ik.py (7 joint positions per arm) -> joint_servo_control
       (control_period = 1.2/hz), plus both omnipickers.

Squeeze the right grip (>0.95) to engage; release to hold. The right trigger
drives the gripper. When collection IPC is enabled, A starts an episode, B
stops and saves it, and the next A starts the next episode.

Run:  source ~/src/agibot-teleop/env.sh && $TELEOP_PY ~/src/test-teleop/send.py
(env.sh only provides the python env + GDK libs; nothing from agibot-teleop's
code is imported — the URDF lives in ./assets.)
"""
import argparse
import math
import os
import subprocess
import sys
import time
import traceback

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ik
import receive
import retarget
from collection_ipc import CollectionClient
from telemetry import (
    backfill_action_state,
    complete_action,
    end_joint_positions,
    joint_positions,
)

# GDK assigns these logical IDs by robot side/port.  Swapping the physical
# omnipicker mechanisms does not swap the IDs reported by get_end_state().
GRIP_NAMES = {"left": ["idx31_gripper_l_inner_joint1"],
              "right": ["idx71_gripper_r_inner_joint1"]}
GRIP_OPEN, GRIP_CLOSED = -0.785, 0.0  # omnipicker [rad]
GRIP_MIN_DELTA = 0.005  # trigger change that (re)opens a declutched burst
GRIP_TAIL_S = 0.3       # keep streaming this long after the last change
GRIP_SEND_PERIOD_S = 0.05  # move_ee_pos at most 20 Hz; preserve arm loop timing
TELEMETRY_EVERY = 10  # refresh joint telemetry every N ticks (10 Hz at 100 Hz)
PLOT_HZ = 10.0        # keep plotting work well below the servo frequency
PLOT_FRAME_CANDIDATES = (
    "head_color_optical_frame",
    "head_camera_color_optical_frame",
    "head_color",
)


def ee_pose(robot, side):
    """Robot-reported EE 4x4 in base_link (clutch anchor), or None."""
    status = robot.get_motion_control_status()
    names = list(status.frame_names)
    want = ik.EE_FRAMES[side]
    for i, n in enumerate(names):
        if n == want or want in n or n in want:
            p = status.frame_poses[i]
            pos, o = p.position, p.orientation
            return retarget.pose_to_T([pos.x, pos.y, pos.z, o.x, o.y, o.z, o.w])
    return None


def gripper_names(robot, side):
    expected = list(GRIP_NAMES[side])
    try:
        st = robot.get_end_state().get(f"{side}_end_state", {})
        names = [str(n) for n in st.get("names", [])]
        if names:
            return names
    except Exception as e:
        print(f"[init] get_end_state({side}) failed ({e}); using {expected}")
    return expected


def gripper_request(gdk, side, position):
    """Build the dedicated GDK request for the omnipicker at this robot side."""
    row = gdk.JointState()
    row.position = float(min(GRIP_CLOSED, max(GRIP_OPEN, position)))
    req = gdk.JointStates()
    req.group = f"{side}_tool"
    req.target_type = "omnipicker"
    req.states = [row]
    req.nums = 1
    return req


def gdk_success(gdk, result):
    return result in (None, 0) or result == gdk.GDKRes.kSuccess


def fmt_xyz(v):
    return f"({v[0]:+.3f},{v[1]:+.3f},{v[2]:+.3f})"


def start_trajectory_plot():
    env = os.environ.copy()
    env.setdefault("MPLCONFIGDIR", f"/tmp/test-teleop-matplotlib-{os.getuid()}")
    cmd = [
        sys.executable,
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "trajectory_plot.py"),
    ]
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE, text=True, bufsize=1, env=env
    )


def choose_plot_frame(tf, timeout_s=5.0):
    """Return (target frame, uses optical axes), preferring calibrated TFs."""
    deadline = time.monotonic() + timeout_s
    last_error = None
    while time.monotonic() < deadline:
        for frame in PLOT_FRAME_CANDIDATES:
            try:
                tf.lookup_transform_latest(frame, ik.EE_FRAMES["right"])
                return frame, True
            except Exception as e:
                last_error = e
        try:
            # The installed GDK model documents head_link3 and publishes its
            # moving TF. Its REP-103 body axes are converted by plot_point().
            tf.lookup_transform_latest("head_link3", ik.EE_FRAMES["right"])
            return "head_link3", False
        except Exception as e:
            last_error = e
        time.sleep(0.1)
    raise RuntimeError(f"no right-EE/head-camera TF after {timeout_s:g}s: "
                       f"{last_error}")


def plot_point(tf, frame, optical_axes):
    transform, _ = tf.lookup_transform_latest(frame, ik.EE_FRAMES["right"])
    t = transform.translation
    point = np.array([t.x, t.y, t.z], dtype=float)
    if not optical_axes:
        # REP-103 body (x forward, y left, z up) -> camera optical
        # (x right, y down, z forward).
        point = np.array([-point[1], -point[2], point[0]], dtype=float)
    return point


def stop_trajectory_plot(process):
    if process is None:
        return
    try:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=2.0)
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hz", type=float, default=100.0)
    ap.add_argument("--scale", type=float, default=1.0,
                    help="hand-to-robot translation scale (default 1.0)")
    ap.add_argument("--max-joint-vel", type=float, default=2.0,
                    help="per-joint IK speed clamp [rad/s]")
    ap.add_argument("--collect-socket", default=None,
                    help="collector IPC socket; enables controller-button recording")
    ap.add_argument("--control-side", choices=("left", "right", "both"), default="both",
                    help="robot side to control and record (default: both)")
    button_choices = ["A", "B", "X", "Y", "left_menu", "right_menu",
                      "left_axis", "right_axis"]
    ap.add_argument("--record-start-button", default="A", choices=button_choices,
                    help="button that starts an episode (default: A)")
    ap.add_argument("--record-stop-button", default="B", choices=button_choices,
                    help="button that stops/saves an episode (default: B)")
    ap.add_argument("--plot", action="store_true",
                    help="live 3-D right-EE trajectory in the head camera frame")
    ap.add_argument("--yes", action="store_true",
                    help="confirmation already completed by the web control service")
    args = ap.parse_args()
    period = 1.0 / args.hz
    plot_every = max(1, round(args.hz / PLOT_HZ))

    print("=" * 64)
    sides = ("left", "right") if args.control_side == "both" else (args.control_side,)
    print("test-teleop: LEFT/RIGHT controllers -> dual-arm IK -> joint servo")
    print(f"  {args.hz:.0f} Hz dual arms + omnipickers, scale {args.scale:g}")
    print("  squeeze either GRIP to engage; each TRIGGER drives its gripper,")
    print("  release grip = arm holds. THIS MOVES THE ROBOT — e-stop in hand.")
    print("=" * 64)

    ctrl = receive.Controllers()
    ctrl.init()  # MUST precede gdk_init (native lib ordering)
    print("[init] XR SDK up")

    import agibot_gdk as gdk
    if gdk.gdk_init() != gdk.GDKRes.kSuccess:
        print("gdk_init failed (source env.sh? DDS route to robot?)")
        return 1
    robot = gdk.Robot()
    time.sleep(2.0)  # DDS discovery
    print("[init] GDK up")

    solver = ik.ArmIK(max_joint_vel=args.max_joint_vel)
    ret = retarget.Retargeter(scale=args.scale)
    grip_names = {s: gripper_names(robot, s) for s in sides}
    print(f"[init] gripper joints: {grip_names}")

    if not args.yes and input("Type YES to start streaming: ").strip() != "YES":
        print("Not confirmed — nothing sent.")
        os._exit(1)

    collector = None
    if args.collect_socket:
        try:
            collector = CollectionClient(args.collect_socket)
            print(f"[dataset] connected; {args.record_start_button}=START, "
                  f"{args.record_stop_button}=STOP+SAVE")
        except Exception as e:
            print(f"[dataset] cannot connect to collector: {e}")
            os._exit(1)

    joints = joint_positions(robot)
    action_names = [name for side in sides for name in ik.ARM_JOINTS[side] + grip_names[side]]
    action_state = {}
    backfill_action_state(action_state, action_names, joints)
    try:
        backfill_action_state(
            action_state, action_names, end_joint_positions(robot, sides)
        )
    except Exception as e:
        print(f"[init] gripper telemetry unavailable ({e}); "
              "first successful gripper command will initialize its action")

    plot_process = None
    plot_tf = None
    plot_frame = None
    plot_optical_axes = False
    plot_failures = 0
    if args.plot:
        try:
            plot_tf = gdk.TF()
            plot_frame, plot_optical_axes = choose_plot_frame(plot_tf)
            plot_process = start_trajectory_plot()
            if plot_optical_axes:
                print(f"[plot] right EE in calibrated optical frame: {plot_frame}")
            else:
                print("[plot] no camera optical TF found; using head_link3 origin "
                      "with optical-axis conversion")
        except Exception as e:
            print(f"[plot] disabled: cannot initialize head-camera TF ({e})")
            stop_trajectory_plot(plot_process)
            plot_process = None

    prev_buttons = {}
    report = {s: {"ctrl": "no data", "target": "-", "ik": "-", "sent": "-"}
              for s in sides}
    grip_streamed = {s: None for s in sides}  # last trigger value streamed
    grip_requested = {s: None for s in sides}  # last move_ee_pos target
    next_grip_send = {s: 0.0 for s in sides}
    burst_until = {s: 0.0 for s in sides}
    burst_hold = {s: None for s in sides}     # arm hold latched at burst start
    recording_requested = False
    sends = 0
    rejects = 0
    last_res = None
    last_reject = None
    t0 = time.monotonic()
    next_t = t0 + period
    next_print = t0 + 1.0
    tick = 0

    try:
        while True:
            tick += 1
            if tick % TELEMETRY_EVERY == 0:
                joints = joint_positions(robot)
                backfill_action_state(action_state, action_names, joints)
                if not all(name in action_state for name in action_names):
                    try:
                        backfill_action_state(
                            action_state, action_names, end_joint_positions(robot, sides)
                        )
                    except Exception:
                        pass
            if plot_process is not None and tick % plot_every == 0:
                try:
                    point = plot_point(
                        plot_tf, plot_frame, plot_optical_axes
                    )
                    plot_process.stdin.write(
                        f"{point[0]:.9f} {point[1]:.9f} {point[2]:.9f}\n"
                    )
                    plot_failures = 0
                except (BrokenPipeError, OSError):
                    print("[plot] window closed; trajectory plotting disabled")
                    stop_trajectory_plot(plot_process)
                    plot_process = None
                except Exception as e:
                    plot_failures += 1
                    if plot_failures == 1:
                        print(f"[plot] waiting for TF: {e}")
            now = time.monotonic()

            sample = ctrl.read()
            names_out, pos_out = [], []
            grip_out = {}
            if sample is None or sample["stale"]:
                ret.declutch()
                for s in sides:
                    if solver.seeded(s):
                        solver.clear(s)
                    grip_streamed[s], burst_hold[s] = None, None
                    burst_until[s] = 0.0
                    report[s] = {"ctrl": "no XR data" if sample is None else "STALE",
                                 "target": "-", "ik": "-", "sent": "not sending"}
            else:
                buttons = sample.get("buttons", {})
                start_pressed = (buttons.get(args.record_start_button, False)
                                 and not prev_buttons.get(args.record_start_button, False))
                stop_pressed = (buttons.get(args.record_stop_button, False)
                                and not prev_buttons.get(args.record_stop_button, False))
                # STOP wins if both buttons happen to transition in one sample.
                if collector is not None and stop_pressed:
                    if recording_requested and collector.send(
                        "stop", pc_ts_ns=time.time_ns()
                    ):
                        recording_requested = False
                        print(f"\n[dataset] {args.record_stop_button} pressed: "
                              "finishing and saving episode")
                elif collector is not None and start_pressed:
                    if not recording_requested and collector.send(
                        "start", pc_ts_ns=time.time_ns()
                    ):
                        recording_requested = True
                        print(f"\n[dataset] {args.record_start_button} pressed: "
                              "starting episode")
                prev_buttons = dict(buttons)

                for side in sides:
                    c = sample[side]
                    report[side]["ctrl"] = (f"xyz={fmt_xyz(c['pose'][:3])}m "
                                            f"grip={c['grip']:.2f} trig={c['trigger']:.2f}")
                    target, gfrac = ret.update(side, c, sample["head"],
                                               lambda s=side: ee_pose(robot, s))
                    gpos = GRIP_OPEN + gfrac * (GRIP_CLOSED - GRIP_OPEN)

                    if target is not None and not np.all(np.isfinite(target)):
                        report[side]["target"] = "NON-FINITE — skipped"
                        continue

                    if target is not None:  # ---- engaged: arm live + gripper
                        try:
                            if not solver.seeded(side):
                                joints = joint_positions(robot)  # fresh seed on engage
                                solver.seed(side, joints)
                                print(f"[engage] {side}: ik seeded from telemetry")
                            q = solver.solve(side, target, joints, period)
                        except (TypeError, ValueError) as error:
                            ret.declutch(side)
                            solver.clear(side)
                            grip_streamed[side], burst_hold[side] = None, None
                            burst_until[side] = 0.0
                            report[side]["target"] = "TELEMETRY INCOMPLETE — declutched"
                            report[side]["ik"] = str(error)
                            report[side]["sent"] = "not sending"
                            continue
                        names_out += ik.ARM_JOINTS[side]
                        pos_out += q
                        grip_out[side] = gpos
                        grip_streamed[side] = gfrac
                        burst_hold[side] = None
                        perr, rerr = solver.last_err[side]
                        report[side]["target"] = (f"xyz={fmt_xyz(target[:3, 3])}m "
                                                  f"grip_frac={gfrac:.2f}")
                        report[side]["ik"] = ("q=[" + " ".join(f"{v:+.2f}" for v in q)
                                              + f"] err={perr * 1000:.1f}mm/"
                                                f"{math.degrees(rerr):.1f}deg")
                        report[side]["sent"] = (f"{len(ik.ARM_JOINTS[side])} arm + "
                                                f"{len(grip_names[side])} grip "
                                                f"@ grip_pos={gpos:+.3f}rad")
                        continue

                    # ---- declutched: arm stays put, trigger still drives the
                    # gripper. A trigger change opens a GRIP_TAIL_S burst.
                    if solver.seeded(side):
                        solver.clear(side)
                    if grip_streamed[side] is None:
                        grip_streamed[side] = gfrac  # baseline only — no burst,
                        # else startup would command the resting trigger value
                    elif abs(gfrac - grip_streamed[side]) >= GRIP_MIN_DELTA:
                        grip_streamed[side] = gfrac
                        if now >= burst_until[side]:  # burst starts: latch hold
                            arm_names = ik.ARM_JOINTS[side]
                            if all(n in joints for n in arm_names):
                                burst_hold[side] = [joints[n] for n in arm_names]
                            else:
                                burst_hold[side] = None
                        burst_until[side] = now + GRIP_TAIL_S

                    if now < burst_until[side] and burst_hold[side] is not None:
                        grip_out[side] = gpos
                        report[side]["target"] = f"declutched, grip_frac={gfrac:.2f}"
                        report[side]["ik"] = ("hold q=[" + " ".join(
                            f"{v:+.2f}" for v in burst_hold[side]) + "]")
                        report[side]["sent"] = (f"gripper burst: "
                                                f"{len(grip_names[side])} grip "
                                                f"@ grip_pos={gpos:+.3f}rad")
                    else:
                        report[side]["target"] = "declutched"
                        report[side]["ik"] = "-"
                        report[side]["sent"] = "not sending"

            successful_actions = {}
            results = {}
            if names_out:
                req = gdk.JointServoControlReq()
                req.control_period = 1.2 / args.hz
                req.joint_names = names_out
                req.joint_positions = [float(v) for v in pos_out]
                try:
                    res = robot.joint_servo_control(req)
                    ok = gdk_success(gdk, res)
                except Exception as e:  # robot said no — skip the tick, keep going
                    res, ok = repr(e), False
                results["arms"] = res
                if ok:
                    successful_actions.update(zip(names_out, pos_out))
                else:
                    rejects += 1
                    last_reject = res
                    for s in sides:
                        if report[s]["sent"] != "not sending":
                            report[s]["sent"] += f"  ARM REJECTED: {res!r}"

            for side, gpos in grip_out.items():
                previous = grip_requested[side]
                changed = previous is None or abs(gpos - previous) >= GRIP_MIN_DELTA
                if not changed or now < next_grip_send[side]:
                    continue
                next_grip_send[side] = now + GRIP_SEND_PERIOD_S
                try:
                    res = robot.move_ee_pos(gripper_request(gdk, side, gpos))
                    ok = gdk_success(gdk, res)
                except Exception as e:
                    res, ok = repr(e), False
                results[f"{side}_grip"] = res
                if ok:
                    grip_requested[side] = gpos
                    for name in grip_names[side]:
                        successful_actions[name] = gpos
                else:
                    rejects += 1
                    last_reject = res
                    report[side]["sent"] += f"  GRIP REJECTED: {res!r}"

            if results:
                last_res = results
            if successful_actions:
                sends += 1
                if collector is not None:
                    for name, value in successful_actions.items():
                        action_state[name] = float(value)
                    action = complete_action(action_names, action_state)
                    if action is not None:
                        collector.send(
                            "action",
                            pc_ts_ns=time.time_ns(),
                            names=action_names,
                            action=action,
                        )

            now = time.monotonic()
            if now >= next_print:
                next_print += 1.0
                print(f"\n[{now - t0:6.1f}s] {sends} sends in the last 1 s"
                      + (f", {rejects} REJECTED (last: {last_reject!r})"
                         if rejects else "")
                      + f" (last result: {last_res!r})")
                rejects = 0
                for s in sides:
                    r = report[s]
                    print(f"  {s:>5}: controller {r['ctrl']}\n"
                          f"         -> mapped   {r['target']}\n"
                          f"         -> joints   {r['ik']}\n"
                          f"         -> sent     {r['sent']}")
                sends = 0

            d = next_t - time.monotonic()
            if d > 0:
                time.sleep(d)
            next_t += period
    except KeyboardInterrupt:
        print("\n[exit] Ctrl-C: streaming stopped; servo targets expire in "
              f"{1.2 / args.hz * 1000:.0f} ms and the arm holds.")
    except Exception:
        traceback.print_exc()
        print("[exit] error above; streaming stopped (targets expire, arm holds).")

    try:
        gdk.gdk_release()
    except Exception:
        pass
    stop_trajectory_plot(plot_process)
    if collector is not None:
        collector.send("shutdown", pc_ts_ns=time.time_ns())
        collector.close()
    os._exit(0)  # sidestep the XR SDK teardown abort


if __name__ == "__main__":
    sys.exit(main())
