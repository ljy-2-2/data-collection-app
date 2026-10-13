#!/usr/bin/env python3
"""Run a right-arm LeRobot vision policy on the live AgiBot G2.

The companion launcher ``run_diffusion_inference.sh`` starts the Python 3.12
model server and this Python 3.10 GDK client. The policy consumes the two
cameras and 8-D state used by ``agibot_right_arm_dp``: right arm joints 1..7
plus the right omnipicker. The 8-D output uses the same order. Actions are
finite-checked, low-pass filtered, connected with a quintic trajectory, and
speed-clamped against live telemetry before they reach the robot.
"""

from __future__ import annotations

import argparse
import json
import pickle
import socket
import struct
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agibot_gdk as gdk  # noqa: E402
import ik  # noqa: E402
from collection_ipc import CollectionClient  # noqa: E402
from collector import gdk_io  # noqa: E402


MODEL_CAMERAS = {
    "head_color": "head_color",
    "hand_right_color": "hand_right_color",
}
RIGHT_GRIP_NAME = "idx71_gripper_r_inner_joint1"
# This must match meta/info.json and the checkpoint normalization tensors.
ACTION_NAMES = ik.ARM_JOINTS["right"] + [RIGHT_GRIP_NAME]
RIGHT_ARM_ACTION_INDICES = tuple(range(7))
RIGHT_GRIP_ACTION_INDEX = 7
GRIP_LIMITS = (-0.785, 0.0)  # GDK omnipicker limits [rad]
GRIP_MIN_DELTA = 0.005
GRIP_SEND_PERIOD_S = 0.05
# /home/yeap/lerobot_datasets/agibot_right_arm_dp/meta/stats.json
TRAIN_STATE_MIN = np.asarray([
    2.2925801, -0.0123979, -3.0526824, -2.1255343,
    -2.1045158, -0.9938949, -1.1989460, -0.7850000,
], dtype=np.float32)
TRAIN_STATE_MAX = np.asarray([
    2.8855975, 1.4034312, -2.6117702, -0.4717614,
    -0.2065874, 0.0576762, 0.3921470, -0.1572290,
], dtype=np.float32)


class QuinticArmSmoother:
    """EMA-filter policy targets and join them with minimum-jerk segments.

    Each segment uses s(u) = 10u^3 - 15u^4 + 6u^5, whose velocity and
    acceleration are zero at both endpoints. The latest filtered policy
    target is latched whenever the next segment begins.
    """

    def __init__(self, steps: int, filter_alpha: float):
        self.steps = int(steps)
        self.filter_alpha = float(filter_alpha)
        self._filtered_target = None
        self._start = None
        self._target = None
        self._step = 0

    @staticmethod
    def _blend(u: float) -> float:
        return u * u * u * (10.0 + u * (-15.0 + 6.0 * u))

    def update(self, raw_target: np.ndarray, current: np.ndarray) -> np.ndarray:
        raw_target = np.asarray(raw_target, dtype=np.float64)
        current = np.asarray(current, dtype=np.float64)
        if self._filtered_target is None:
            self._filtered_target = raw_target.copy()
            self._start = current.copy()
            self._target = self._filtered_target.copy()
            self._step = 0
        else:
            alpha = self.filter_alpha
            self._filtered_target += alpha * (raw_target - self._filtered_target)

        if self._step >= self.steps:
            self._start = self._target.copy()
            self._target = self._filtered_target.copy()
            self._step = 0

        self._step += 1
        u = min(1.0, self._step / self.steps)
        return self._start + self._blend(u) * (self._target - self._start)

    def reset(self) -> None:
        self._filtered_target = None
        self._start = None
        self._target = None
        self._step = 0


class ShortHandoffBlender:
    """Progressively release a newly resumed policy without delaying it."""

    def __init__(self, steps: int):
        self.steps = int(steps)
        self._step = self.steps

    def reset(self) -> None:
        self._step = 0

    def update(self, target: np.ndarray, current: np.ndarray) -> np.ndarray:
        target = np.asarray(target, dtype=np.float64)
        current = np.asarray(current, dtype=np.float64)
        if self.steps <= 0 or self._step >= self.steps:
            return target
        self._step += 1
        u = self._step / self.steps
        weight = QuinticArmSmoother._blend(u)
        return current + weight * (target - current)


def send_message(sock: socket.socket, value: object) -> None:
    data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    sock.sendall(struct.pack("!Q", len(data)) + data)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        part = sock.recv(size - len(out))
        if not part:
            raise ConnectionError("model server disconnected")
        out.extend(part)
    return bytes(out)


def recv_message(sock: socket.socket) -> object:
    size = struct.unpack("!Q", recv_exact(sock, 8))[0]
    if size > 10_000_000:
        raise ValueError(f"oversized model response ({size} bytes)")
    return pickle.loads(recv_exact(sock, size))


def connect_model(path: str, timeout_s: float = 30.0) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            sock.connect(path)
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                sock.close()
                raise RuntimeError(f"model server did not open {path}")
            time.sleep(0.1)


def make_gripper_request(gdk, side: str, position: float):
    """Build the dedicated GDK end-effector request for one omnipicker."""
    row = gdk.JointState()
    row.position = float(np.clip(position, *GRIP_LIMITS))
    request = gdk.JointStates()
    request.group = f"{side}_tool"
    request.target_type = "omnipicker"
    request.states = [row]
    request.nums = 1
    return request


def gdk_success(gdk, result) -> bool:
    return result in (None, 0) or result == gdk.GDKRes.kSuccess


def make_state(tensors: dict) -> np.ndarray | None:
    """Extract the named 7 right joints + right gripper from a full live bundle."""
    try:
        joint_names = [str(name) for name in tensors["state__joint__name"]]
        joint_values = np.asarray(
            tensors["state__joint__position"], np.float32
        ).ravel()
        grip_names = [
            str(name) for name in tensors["state__right_effector__name"]
        ]
        grip_values = np.asarray(
            tensors["state__right_effector__position"], np.float32
        ).ravel()
    except (KeyError, TypeError, ValueError):
        return None
    if len(joint_names) != joint_values.size or len(grip_names) != grip_values.size:
        return None
    joints = dict(zip(joint_names, joint_values))
    grips = dict(zip(grip_names, grip_values))
    if (
        not all(name in joints for name in ik.ARM_JOINTS["right"])
        or RIGHT_GRIP_NAME not in grips
    ):
        return None
    state = np.asarray(
        [joints[name] for name in ik.ARM_JOINTS["right"]]
        + [grips[RIGHT_GRIP_NAME]],
        dtype=np.float32,
    )
    return state if state.shape == (8,) and np.all(np.isfinite(state)) else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model-socket", required=True)
    ap.add_argument("--collect-socket", default=None,
                    help="collector IPC socket for DAgger action recording")
    ap.add_argument("--ready-file", default=None,
                    help="write a marker after the first successful robot command")
    ap.add_argument("--initialized-file", default=None,
                    help="write after GDK, model and collector are connected")
    ap.add_argument("--activation-file", default=None,
                    help="stay prewarmed but send commands only while file says active")
    ap.add_argument("--inactive-ready-file", default=None,
                    help="acknowledge that policy command streaming is inactive")
    ap.add_argument("--yes", action="store_true",
                    help="skip the terminal YES prompt")
    ap.add_argument("--policy-name", default="Diffusion Policy",
                    help="policy label used in terminal logs")
    ap.add_argument(
        "--hz",
        type=float,
        default=30.0,
        help="policy command rate; must match the 30 Hz training dataset",
    )
    ap.add_argument(
        "--training-hz",
        type=float,
        default=30.0,
        help="training dataset rate, used to detect action-chunk timing mismatch",
    )
    ap.add_argument("--max-joint-vel", type=float, default=0.50, help="hard arm-target clamp in rad/s")
    ap.add_argument(
        "--quintic-steps",
        type=int,
        default=5,
        help=(
            "command samples in each quintic arm trajectory; larger is "
            "smoother but adds delay (default: 5, 1 disables interpolation)"
        ),
    )
    ap.add_argument(
        "--action-filter-alpha",
        type=float,
        default=0.25,
        help=(
            "EMA weight for new arm targets in (0,1]; smaller is smoother "
            "(default: 0.25, 1 disables filtering)"
        ),
    )
    ap.add_argument(
        "--handoff-steps",
        type=int,
        default=0,
        help=(
            "minimum-jerk policy release after activation, in command samples; "
            "0 disables it"
        ),
    )
    ap.add_argument(
        "--max-arm-target-error",
        type=float,
        default=0.0,
        help="reject distant raw right-arm targets in rad; 0 disables this check",
    )
    ap.add_argument(
        "--max-normalized-state",
        type=float,
        default=2.0,
        help="reject live MIN_MAX-normalized state outside +/-N; 0 disables",
    )
    ap.add_argument("--camera-retries", type=int, default=2)
    ap.add_argument("--camera-retry-sleep-ms", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="run cameras and policy but never send robot commands")
    args = ap.parse_args()
    if (
        args.hz <= 0
        or args.training_hz <= 0
        or args.max_joint_vel <= 0
        or args.quintic_steps < 1
        or args.handoff_steps < 0
        or not 0 < args.action_filter_alpha <= 1
        or args.max_arm_target_error < 0
        or args.max_normalized_state < 0
    ):
        raise SystemExit(
            "--hz, --training-hz and --max-joint-vel must be positive; "
            "--quintic-steps must be >= 1; --action-filter-alpha must be in "
            "(0,1]; --handoff-steps must be non-negative; "
            "safety limits must be non-negative"
        )

    print("=" * 68)
    print(
        f"AgiBot {args.policy_name} inference: "
        "live cameras -> model -> right-arm servo"
    )
    print(f"  rate <= {args.hz:g} Hz; hard speed clamp {args.max_joint_vel:g} rad/s")
    print(
        f"  arm smoothing: EMA alpha={args.action_filter_alpha:g}; "
        f"quintic steps={args.quintic_steps} "
        f"({1000.0 * args.quintic_steps / args.hz:.0f} ms/segment)"
    )
    if abs(args.hz - args.training_hz) > 1e-6:
        print(
            f"  WARNING: dataset was recorded at {args.training_hz:g} Hz, but "
            f"--hz={args.hz:g}; action timing will not match training."
        )
    if args.dry_run:
        print("  DRY RUN: actions are checked and printed but never sent to the robot.")
    else:
        print("  THIS MOVES THE ROBOT. Keep e-stop available and test unobstructed.")
    print("=" * 68)
    if gdk.gdk_init() != gdk.GDKRes.kSuccess:
        raise SystemExit("gdk_init failed (source GDK env and check robot DDS network)")
    # Camera and bundle topics appear only after DDS discovery.  Live testing
    # on this robot shows valid shapes/FPS roughly two seconds after init.
    time.sleep(2.0)
    robot = gdk.Robot()
    bundles = gdk_io.BundleSource()
    cameras = gdk_io.CameraFetcher()
    model = None
    collector = None
    period = 1.0 / args.hz
    next_command = time.monotonic()
    next_grip_send = 0.0
    last_grip_command = None
    arm_smoother = QuinticArmSmoother(
        args.quintic_steps, args.action_filter_alpha
    )
    handoff_blender = ShortHandoffBlender(args.handoff_steps)
    sent = skipped = 0
    ready_announced = False
    last_report = time.monotonic()
    try:
        model = connect_model(args.model_socket)
        print("[init] GDK and model server connected; waiting for complete camera bundles")
        if not args.dry_run:
            if (not args.yes
                    and input("Type YES to enable RIGHT-arm model commands: ").strip() != "YES"):
                print("Not confirmed — no robot commands were sent.")
                return 1
        if args.collect_socket:
            collector = CollectionClient(args.collect_socket)
            print("[dataset] connected; streaming policy actions")
        if args.initialized_file:
            Path(args.initialized_file).write_text(json.dumps({
                "pc_ts_ns": time.time_ns(),
                "state": "inference_initialized",
            }))
        was_externally_active = not bool(args.activation_file)
        inactive_announced = False
        while True:
            externally_active = True
            if args.activation_file:
                try:
                    externally_active = (
                        Path(args.activation_file).read_text().strip() == "active"
                    )
                except OSError:
                    externally_active = False
            if not externally_active:
                if not inactive_announced and args.inactive_ready_file:
                    Path(args.inactive_ready_file).write_text(json.dumps({
                        "pc_ts_ns": time.time_ns(),
                        "state": "inference_inactive",
                    }))
                    inactive_announced = True
                was_externally_active = False
                time.sleep(0.005)
                continue
            if not was_externally_active:
                send_message(model, {"type": "reset"})
                reply = recv_message(model)
                if reply != {"type": "reset_done"}:
                    raise RuntimeError(f"model reset failed: {reply!r}")
                arm_smoother.reset()
                handoff_blender.reset()
                next_command = time.monotonic()
                next_grip_send = 0.0
                last_grip_command = None
                ready_announced = False
                sent = skipped = 0
                inactive_announced = False
                was_externally_active = True
                print("[activate] policy model/GDK already warm; planner reset")
            # Do not poll (and thereby deduplicate/consume) a fresh bundle
            # before the rate gate opens.  The robot publishes at about
            # 30.3 Hz, slightly faster than a nominal 30 Hz deadline; polling
            # first used to discard those early bundles and reduce inference
            # throughput to roughly 20 Hz.
            if time.monotonic() < next_command:
                time.sleep(0.001)
                continue
            try:
                bundle = bundles.poll()
            except RuntimeError:
                # GDK raises while the bundle ring buffer is not populated yet.
                time.sleep(0.01)
                continue
            if bundle is None:
                time.sleep(0.001)
                continue
            next_command = time.monotonic() + period
            tensors, cam_ts = gdk_io.split_bundle(bundle)
            state = make_state(tensors)
            if state is None:
                skipped += 1
                continue
            state_normalized = (
                2.0 * (state - TRAIN_STATE_MIN)
                / (TRAIN_STATE_MAX - TRAIN_STATE_MIN)
                - 1.0
            )
            max_normalized_state = float(np.max(np.abs(state_normalized)))
            if (
                args.max_normalized_state > 0
                and max_normalized_state > args.max_normalized_state
            ):
                if time.monotonic() - last_report >= 1.0:
                    print(
                        "[safety] live state is outside training range: "
                        f"max normalized={max_normalized_state:.2f}, limit="
                        f"{args.max_normalized_state:.2f}\n"
                        "  state=" + " ".join(f"{v:+.3f}" for v in state) + "\n"
                        "  norm= " + " ".join(
                            f"{v:+.2f}" for v in state_normalized
                        )
                    )
                    last_report = time.monotonic()
                skipped += 1
                continue
            images = {}
            for model_name, gdk_name in MODEL_CAMERAS.items():
                ts = cam_ts.get(gdk_name)
                if ts is None:
                    images = {}
                    break
                image, _ = cameras.fetch_exact(
                    gdk_io.camera_type(gdk_name), ts, args.camera_retries,
                    args.camera_retry_sleep_ms,
                )
                if image is None or image.encoding != gdk.Encoding.JPEG:
                    images = {}
                    break
                images[model_name] = np.asarray(image.data, np.uint8).tobytes()
            if len(images) != len(MODEL_CAMERAS):
                skipped += 1
                continue

            inference_started = time.monotonic()
            send_message(model, {
                "type": "observe",
                "state": state,
                "images": images,
            })
            reply = recv_message(model)
            inference_ms = (time.monotonic() - inference_started) * 1000.0
            planner = reply.get("planner", {})
            action = np.asarray(reply.get("action", []), np.float64)
            if action.shape != (8,) or not np.all(np.isfinite(action)):
                print(f"[safety] invalid model action skipped: shape={action.shape}")
                skipped += 1
                continue
            limit = args.max_joint_vel * period
            # `state` is decoded from the same timestamped GDK bundle as the
            # camera observations.  Re-querying get_joint_states() and
            # get_end_state() here costs a substantial part of a 33 ms control
            # period and mixes newer telemetry with older images.  Use the
            # already validated bundle state for both safety checks and clamps.
            now_positions = state.astype(np.float64, copy=False)
            raw_arm_error = float(np.max(np.abs(
                action[list(RIGHT_ARM_ACTION_INDICES)]
                - now_positions[list(RIGHT_ARM_ACTION_INDICES)]
            )))
            if (
                not args.dry_run
                and args.max_arm_target_error > 0
                and raw_arm_error > args.max_arm_target_error
            ):
                print(
                    f"[safety] raw arm target differs from telemetry by "
                    f"{raw_arm_error:.3f} rad (limit "
                    f"{args.max_arm_target_error:.3f}); command skipped"
                )
                skipped += 1
                continue
            command = action.copy()
            arm_indices = list(RIGHT_ARM_ACTION_INDICES)
            smoothed_arm = arm_smoother.update(
                action[arm_indices], now_positions[arm_indices]
            )
            smoothed_arm = handoff_blender.update(
                smoothed_arm, now_positions[arm_indices]
            )
            command[arm_indices] = np.clip(
                smoothed_arm,
                now_positions[arm_indices] - limit,
                now_positions[arm_indices] + limit,
            )
            # GDK's motion-control parameter table contains arm joints only.
            # Omnipickers are controlled through move_ee_pos, even though their
            # telemetry exposes URDF-style idx31/idx71 names.
            arm_req = gdk.JointServoControlReq()
            arm_req.control_period = 1.2 / args.hz
            arm_req.joint_names = [
                ACTION_NAMES[i] for i in RIGHT_ARM_ACTION_INDICES
            ]
            arm_req.joint_positions = [
                float(command[i]) for i in RIGHT_ARM_ACTION_INDICES
            ]
            command[RIGHT_GRIP_ACTION_INDEX] = np.clip(
                command[RIGHT_GRIP_ACTION_INDEX], *GRIP_LIMITS)
            if args.dry_run:
                results = {"arms": None, "right_grip": None}
            else:
                results = {"arms": robot.joint_servo_control(arm_req)}
                grip_target = float(command[RIGHT_GRIP_ACTION_INDEX])
                grip_changed = (
                    last_grip_command is None
                    or abs(grip_target - last_grip_command) >= GRIP_MIN_DELTA
                )
                now = time.monotonic()
                if grip_changed and now >= next_grip_send:
                    next_grip_send = now + GRIP_SEND_PERIOD_S
                    grip_result = robot.move_ee_pos(
                        make_gripper_request(gdk, "right", grip_target)
                    )
                    results["right_grip"] = grip_result
                    if gdk_success(gdk, grip_result):
                        last_grip_command = grip_target
            failed = {name: result for name, result in results.items()
                      if not args.dry_run and not gdk_success(gdk, result)}
            if not failed:
                sent += 1
                if args.ready_file and not ready_announced and not args.dry_run:
                    ready_path = Path(args.ready_file)
                    ready_path.write_text(json.dumps({
                        "pc_ts_ns": time.time_ns(),
                        "state": "command_sent",
                    }))
                    ready_announced = True
                if collector is not None and not args.dry_run:
                    collector.send(
                        "action",
                        pc_ts_ns=time.time_ns(),
                        names=ACTION_NAMES,
                        action=[float(value) for value in command],
                        source="policy",
                    )
            else:
                print(f"[GDK] command rejected: {failed!r}")
                skipped += 1
            if time.monotonic() - last_report >= 1.0:
                count_name = "processed" if args.dry_run else "sent"
                raw_delta = float(np.max(np.abs(action - now_positions)))
                clamp_delta = float(np.max(np.abs(command - now_positions)))
                print(
                    f"[status] {count_name}={sent}/s skipped={skipped}/s "
                    f"model-rpc={inference_ms:.1f}ms "
                    f"buffered={planner.get('buffered', '?')} "
                    f"generating={planner.get('generating', '?')} "
                    f"pending={planner.get('pending', '?')} "
                    f"underruns={planner.get('underruns', '?')} "
                    f"plan-age={planner.get('plan_age', '?')} "
                    f"expired-plans={planner.get('expired_predictions', '?')} "
                    f"tracking={planner.get('tracking_error', '?')} "
                    f"active-right-error={raw_arm_error:.3f} "
                    f"state-norm-max={max_normalized_state:.2f} "
                    f"all-raw-error={raw_delta:.3f} "
                    f"command-current-max={clamp_delta:.3f}\n"
                    "  raw_action=" + " ".join(f"{v:+.3f}" for v in action) + "\n"
                    "  smooth_arm=" + " ".join(
                        f"{v:+.3f}" for v in smoothed_arm
                    ) + "\n"
                    "  command=   " + " ".join(f"{v:+.3f}" for v in command)
                )
                sent = skipped = 0
                last_report = time.monotonic()
    except KeyboardInterrupt:
        if args.dry_run:
            print("\n[exit] dry run stopped; no robot commands were sent.")
        else:
            print("\n[exit] stopped; servo targets will expire and the robot holds.")
    finally:
        if collector is not None:
            collector.close()
        if model is not None:
            try:
                send_message(model, {"type": "shutdown"})
                model.close()
            except OSError:
                pass
        cameras.close()
        bundles.close()
        try:
            gdk.gdk_release()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
