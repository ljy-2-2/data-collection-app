#!/usr/bin/env python3
"""DAgger expert sender backed by the shared desktop teleoperation runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "teleop_runtime"
sys.path[:0] = [str(RUNTIME), str(RUNTIME / "src")]

from apps.platform_right_arm import (  # noqa: E402
    RIGHT_GRIPPER,
    RIGHT_JOINTS,
    LegacyXRInputAdapter,
    RightArmTeleopSession,
)
from collection_ipc import CollectionClient  # noqa: E402

ENGAGE = 0.95
RELEASE = 0.90


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hz", type=float, default=100.0)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--max-joint-vel", type=float, default=2.0)
    parser.add_argument("--collect-socket")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--external-record-control", action="store_true")
    parser.add_argument("--ready-file")
    parser.add_argument("--activation-file")
    parser.add_argument("--inactive-ready-file")
    parser.add_argument("--gui-event-file")
    parser.add_argument("--action-log-hz", type=float, default=30.0)
    parser.add_argument("--controller-timeout", type=float, default=0.0)
    parser.add_argument("--control-side", choices=("right",), default="right")
    choices = ["A", "B", "X", "Y", "left_menu", "right_menu", "left_axis", "right_axis"]
    parser.add_argument("--record-start-button", choices=choices, default="A")
    parser.add_argument("--record-stop-button", choices=choices, default="B")
    parser.add_argument("--plot", action="store_true")
    args = parser.parse_args()
    if args.hz <= 0 or args.max_joint_vel <= 0 or args.scale <= 0:
        parser.error("hz, scale, and max joint velocity must be positive")
    if args.controller_timeout < 0 or args.action_log_hz <= 0:
        parser.error("controller timeout must be non-negative and action log Hz positive")
    return args


def _active(path: str | None) -> bool:
    if not path:
        return True
    try:
        return Path(path).read_text().strip() == "active"
    except OSError:
        return False


def _write_json(path: str | None, payload: dict) -> None:
    if path:
        Path(path).write_text(json.dumps(payload, ensure_ascii=False))


def _append_event(path: str | None, event: str, **payload: object) -> None:
    if not path:
        return
    with Path(path).open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "event": event,
                    **payload,
                    "pc_ts_ns": time.time_ns(),
                },
                separators=(",", ":"),
            )
            + "\n"
        )


def _emit_gui_events(
    frame,
    grip_latched: dict[str, bool],
    previous: dict[str, bool],
    event_path: str | None,
) -> dict[str, bool]:
    controllers = {"left": frame.left_controller, "right": frame.right_controller}
    for side, controller in controllers.items():
        if controller.grip > ENGAGE and not grip_latched[side]:
            grip_latched[side] = True
            _append_event(event_path, "grip_takeover", side=side)
        elif controller.grip < RELEASE:
            grip_latched[side] = False
    buttons = {
        "A": frame.right_controller.buttons.primary,
        "B": frame.right_controller.buttons.secondary,
        "X": frame.left_controller.buttons.primary,
        "Y": frame.left_controller.buttons.secondary,
    }
    if buttons["X"] and not previous.get("X", False):
        _append_event(event_path, "resume_inference", button="X")
    if buttons["Y"] and not previous.get("Y", False):
        _append_event(event_path, "end_session", button="Y")
    return buttons


def main() -> int:
    args = _arguments()
    period = 1.0 / args.hz
    action_period = 1.0 / args.action_log_hz
    action_names = [*RIGHT_JOINTS, RIGHT_GRIPPER]
    print("data-platform DAgger: shared desktop teleoperation runtime -> RIGHT arm")
    print("Grip takes over, X resumes inference, Y ends the session; e-stop in hand.")

    xr = LegacyXRInputAdapter()
    xr.init()  # Native-library ordering: XR must precede GDK.
    import agibot_gdk as gdk

    if gdk.gdk_init() != gdk.GDKRes.kSuccess:
        raise RuntimeError("gdk_init failed; check GDK environment and DDS")
    robot = gdk.Robot()
    time.sleep(2.0)
    session = RightArmTeleopSession(
        gdk=gdk,
        robot=robot,
        scale=args.scale,
        control_hz=args.hz,
        max_joint_velocity_rad_s=args.max_joint_vel,
    )
    if not args.yes and input("Type YES to start streaming: ").strip() != "YES":
        return 1

    collector = CollectionClient(args.collect_socket) if args.collect_socket else None
    deadline = time.monotonic() + args.controller_timeout if args.controller_timeout else None
    initial = None
    while initial is None or initial.stale:
        initial = xr.read_frame()
        if deadline is not None and time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    ready = initial is not None and not initial.stale
    action_state = session.snapshot_action()
    _write_json(
        args.ready_file,
        {
            "pc_ts_ns": time.time_ns(),
            "state": "controller_ready" if ready else "teleop_prewarmed",
        },
    )

    grip_latched = {"left": False, "right": False}
    previous_buttons: dict[str, bool] = {}
    if ready:
        previous_buttons = {
            "A": initial.right_controller.buttons.primary,
            "B": initial.right_controller.buttons.secondary,
            "X": initial.left_controller.buttons.primary,
            "Y": initial.left_controller.buttons.secondary,
        }
        grip_latched = {
            "left": initial.left_controller.grip > ENGAGE,
            "right": initial.right_controller.grip > ENGAGE,
        }
    recording = False
    was_active = not bool(args.activation_file)
    inactive_announced = False
    next_action = time.monotonic()
    next_tick = time.monotonic()

    try:
        while True:
            frame = xr.read_frame()
            fresh = frame is not None and not frame.stale
            buttons = previous_buttons
            if fresh:
                buttons = _emit_gui_events(
                    frame,
                    grip_latched,
                    previous_buttons,
                    args.gui_event_file,
                )

            active = _active(args.activation_file)
            if active and not was_active:
                session.reset()
                action_state = session.snapshot_action()
                next_action = time.monotonic()
                print("[activate] expert mode: refreshed current 8-D hold action")
            if not active and not inactive_announced:
                session.reset()
                _write_json(
                    args.inactive_ready_file,
                    {
                        "pc_ts_ns": time.time_ns(),
                        "state": "teleop_inactive",
                    },
                )
                inactive_announced = True
            elif active:
                inactive_announced = False
            was_active = active

            if active and fresh:
                start_pressed = buttons.get(
                    args.record_start_button, False
                ) and not previous_buttons.get(args.record_start_button, False)
                stop_pressed = buttons.get(
                    args.record_stop_button, False
                ) and not previous_buttons.get(args.record_stop_button, False)
                if collector and not args.external_record_control and stop_pressed and recording:
                    if collector.send("stop", pc_ts_ns=time.time_ns()):
                        recording = False
                elif (
                    collector
                    and not args.external_record_control
                    and start_pressed
                    and not recording
                ):
                    if collector.send("start", pc_ts_ns=time.time_ns()):
                        recording = True
                try:
                    result = session.execute(frame)
                except ValueError as error:
                    session.reset()
                    print(f"[teleop] frame rejected safely: {error}", flush=True)
                else:
                    action_state = session.update_action(action_state, result)
                now = time.monotonic()
                if collector and now >= next_action:
                    collector.send(
                        "action",
                        pc_ts_ns=time.time_ns(),
                        names=action_names,
                        action=action_state,
                        source="expert",
                    )
                    while next_action <= now:
                        next_action += action_period
            else:
                session.reset()

            if fresh:
                previous_buttons = dict(buttons)
            next_tick += period
            time.sleep(max(0.0, next_tick - time.monotonic()))
    except KeyboardInterrupt:
        print("[exit] streaming stopped; servo targets expire and the arm holds")
    except Exception:
        traceback.print_exc()
    finally:
        try:
            session.stop()
        except Exception:
            pass
        try:
            gdk.gdk_release()
        except Exception:
            pass
        if collector is not None:
            if not args.external_record_control:
                collector.send("shutdown", pc_ts_ns=time.time_ns())
            collector.close()
        try:
            xr.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
