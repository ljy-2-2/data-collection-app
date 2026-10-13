#!/usr/bin/env python3
"""Local inference server for an AgiBot LeRobot policy checkpoint.

This process deliberately has no GDK dependency and must be run with the
Python environment used for LeRobot training (Python 3.12 on this machine).
``agibot_diffusion_infer.py`` sends it two JPEG camera frames and the 8-D
right-arm state over an AF_UNIX socket and receives one 8-D action in reply.
"""

from __future__ import annotations

import argparse
import pickle
import socket
import struct
import sys
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path


def fit_rgb(cv2, rgb, expected: tuple[int, int, int], mode: str):
    """Convert an RGB HWC frame to the model's configured visual shape."""
    channels, dst_h, dst_w = expected
    if rgb.ndim != 3 or rgb.shape[2] != channels or channels != 3:
        raise ValueError(f"decoded image shape {rgb.shape}, expected 3-channel RGB")
    src_h, src_w = rgb.shape[:2]
    if (src_h, src_w) == (dst_h, dst_w):
        return rgb
    if mode == "strict":
        raise ValueError(
            f"image shape {(channels, src_h, src_w)}, expected {expected}"
        )
    if mode == "center-crop":
        src_ratio = src_w / src_h
        dst_ratio = dst_w / dst_h
        if src_ratio > dst_ratio:
            crop_w = max(1, round(src_h * dst_ratio))
            x0 = (src_w - crop_w) // 2
            rgb = rgb[:, x0:x0 + crop_w]
        else:
            crop_h = max(1, round(src_w / dst_ratio))
            y0 = (src_h - crop_h) // 2
            rgb = rgb[y0:y0 + crop_h, :]
    interpolation = cv2.INTER_AREA if rgb.shape[0] >= dst_h and rgb.shape[1] >= dst_w else cv2.INTER_LINEAR
    return cv2.resize(rgb, (dst_w, dst_h), interpolation=interpolation)


def send_message(sock: socket.socket, value: object) -> None:
    data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    sock.sendall(struct.pack("!Q", len(data)) + data)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        data = sock.recv(size - len(chunks))
        if not data:
            raise ConnectionError("inference client disconnected")
        chunks.extend(data)
    return bytes(chunks)


def recv_message(sock: socket.socket) -> object:
    size = struct.unpack("!Q", recv_exact(sock, 8))[0]
    if size > 100_000_000:
        raise ValueError(f"refusing oversized RPC message ({size} bytes)")
    return pickle.loads(recv_exact(sock, size))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--socket", required=True)
    ap.add_argument(
        "--policy-type",
        choices=("diffusion", "act"),
        default="diffusion",
        help="LeRobot policy implementation used to load the checkpoint",
    )
    ap.add_argument("--device", default="cuda", help="cuda or cpu")
    ap.add_argument(
        "--image-fit", choices=("resize", "center-crop", "strict"), default="center-crop",
        help="how live camera frames are fitted to the model's configured size",
    )
    ap.add_argument(
        "--prefetch-actions",
        type=int,
        default=7,
        help="start asynchronous replanning when this many cached actions remain",
    )
    ap.add_argument(
        "--num-inference-steps",
        type=int,
        default=10,
        help="DDPM denoising steps per action chunk",
    )
    ap.add_argument(
        "--sampling-seed",
        type=int,
        default=1000,
        help="fixed diffusion noise seed for deterministic live predictions",
    )
    args = ap.parse_args()

    checkpoint = Path(args.checkpoint).expanduser()
    weights = checkpoint / "model.safetensors"
    if not weights.is_file() or weights.stat().st_size == 0:
        raise SystemExit(f"checkpoint weights missing or empty: {weights}")

    # Import only after validating files, so a bad path produces an actionable
    # error rather than a long CUDA/LeRobot import traceback.
    import cv2
    import numpy as np
    import torch
    from lerobot.common.control_utils import prepare_observation_for_inference
    from lerobot.policies import get_policy_class, make_pre_post_processors

    device = args.device
    if device.startswith("cuda") and not torch.cuda.is_available():
        print("[model] CUDA unavailable; falling back to CPU", flush=True)
        device = "cpu"

    policy_cls = get_policy_class(args.policy_type)
    policy = policy_cls.from_pretrained(str(checkpoint))
    policy.to(device).eval()
    policy.reset()
    plan_steps = policy.config.horizon - policy.config.n_obs_steps + 1
    if args.prefetch_actions < 1 or plan_steps < 2:
        raise SystemExit(
            "--prefetch-actions must be positive and the checkpoint must "
            "provide at least 2 future action steps"
        )
    prefetch_actions = min(args.prefetch_actions, plan_steps - 1)
    if not 1 <= args.num_inference_steps <= policy.config.num_train_timesteps:
        raise SystemExit(
            "--num-inference-steps must be in [1, "
            f"{policy.config.num_train_timesteps}]"
        )
    policy.diffusion.num_inference_steps = args.num_inference_steps
    generator = torch.Generator(device=device).manual_seed(args.sampling_seed)
    fixed_noise = torch.randn(
        (
            1,
            policy.config.horizon,
            policy.config.action_feature.shape[0],
        ),
        generator=generator,
        device=device,
        dtype=next(policy.parameters()).dtype,
    )
    preprocessor, postprocessor = make_pre_post_processors(
        policy.config,
        pretrained_path=str(checkpoint),
        preprocessor_overrides={"device_processor": {"device": device}},
        postprocessor_overrides={"device_processor": {"device": device}},
    )
    expected_images = {
        key.removeprefix("observation.images."): tuple(feature.shape)
        for key, feature in policy.config.input_features.items()
        if key.startswith("observation.images.")
    }
    expected_camera_names = {"head_color", "hand_right_color"}
    state_shape = tuple(policy.config.input_features["observation.state"].shape)
    action_shape = tuple(policy.config.output_features["action"].shape)
    if state_shape != (8,) or action_shape != (8,):
        raise SystemExit(
            f"checkpoint must be right-arm 8-D state/action; got "
            f"state={state_shape}, action={action_shape}"
        )
    if set(expected_images) != expected_camera_names:
        raise SystemExit(
            f"checkpoint cameras must be {sorted(expected_camera_names)}; "
            f"got {sorted(expected_images)}"
        )

    path = Path(args.socket)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    print(
        f"[model] READY policy={args.policy_type} socket={path} device={device} "
        f"async-prefetch={prefetch_actions}/{plan_steps} "
        f"trained-action-steps={policy.config.n_action_steps} "
        f"denoise-steps={args.num_inference_steps} time-paced-actions",
        flush=True,
    )
    resized_reported = set()
    observation_history = deque(maxlen=policy.config.n_obs_steps)
    action_queue = deque()
    last_action = None
    prediction: Future | None = None
    prediction_step = 0
    pending_chunk = None
    pending_step = 0
    control_step = 0
    underruns = 0
    expired_predictions = 0
    installed_age = 0

    def predict_chunk(history):
        """Generate and denormalize one action chunk on the background thread."""
        rows = []
        for observation in history:
            batch = prepare_observation_for_inference(observation, device)
            rows.append(preprocessor(batch))
        batch = {
            key: torch.stack([row[key] for row in rows], dim=1)
            for key in policy.config.input_features
        }
        if policy.config.image_features:
            batch["observation.images"] = torch.stack(
                [batch[key] for key in policy.config.image_features],
                dim=-4,
            )
        with torch.inference_mode(), (
            torch.autocast(device_type="cuda")
            if device.startswith("cuda") and policy.config.use_amp
            else nullcontext()
        ):
            # LeRobot's public predict_action_chunk truncates the trained
            # horizon to n_action_steps.  Async inference needs the remaining
            # trained future steps so it can discard the prefix that elapsed
            # while the GPU was sampling without immediately running dry.
            global_cond = policy.diffusion._prepare_global_conditioning(batch)
            actions = policy.diffusion.conditional_sample(
                batch_size=1, global_cond=global_cond, noise=fixed_noise
            )
            actions = postprocessor(actions)
            actions = actions[:, policy.config.n_obs_steps - 1 :]
        result = actions.squeeze(0).detach().float().cpu().numpy()
        expected = (plan_steps, policy.config.action_feature.shape[0])
        if result.shape != expected or not np.all(np.isfinite(result)):
            raise RuntimeError(
                f"policy returned invalid action chunk {result.shape}, expected {expected}"
            )
        return result

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diffusion-prefetch")

    def start_prediction() -> None:
        nonlocal prediction, prediction_step
        if prediction is not None or not observation_history:
            return
        # prepare_observation_for_inference mutates its input dictionary, so
        # give the worker private NumPy copies rather than corrupting the live
        # observation ring buffer.
        history = [
            {key: value.copy() for key, value in observation.items()}
            for observation in observation_history
        ]
        while len(history) < policy.config.n_obs_steps:
            history.insert(
                0,
                {key: value.copy() for key, value in history[0].items()},
            )
        prediction_step = control_step
        prediction = executor.submit(predict_chunk, history)

    def harvest_finished_prediction(wait: bool = False) -> None:
        """Move a completed worker result into the pending-plan buffer."""
        nonlocal prediction, pending_chunk, pending_step
        if prediction is None or (not wait and not prediction.done()):
            return
        pending_chunk = prediction.result()
        pending_step = prediction_step
        prediction = None

    def install_pending_prediction() -> None:
        """Install a new plan at the action matching the current control time."""
        nonlocal pending_chunk, expired_predictions, installed_age
        if pending_chunk is None:
            return
        chunk = pending_chunk
        pending_chunk = None
        age = max(0, control_step - pending_step)
        installed_age = age
        if age >= len(chunk):
            expired_predictions += 1
            return
        # The first `age` actions correspond to control periods that elapsed
        # during GPU inference.  Replaying them would command a trajectory
        # based on stale images.  Replace the old tail immediately with the
        # time-aligned suffix of the fresher prediction.
        action_queue.clear()
        action_queue.extend(chunk[age:])

    try:
        conn, _ = listener.accept()
        with conn:
            while True:
                request = recv_message(conn)
                if request == {"type": "shutdown"}:
                    return 0
                if request == {"type": "reset"}:
                    if prediction is not None:
                        prediction.cancel()
                        if not prediction.cancelled():
                            try:
                                prediction.result()
                            except Exception:
                                pass
                    policy.reset()
                    observation_history.clear()
                    action_queue.clear()
                    last_action = None
                    prediction = None
                    prediction_step = 0
                    pending_chunk = None
                    pending_step = 0
                    control_step = 0
                    underruns = 0
                    expired_predictions = 0
                    installed_age = 0
                    send_message(conn, {"type": "reset_done"})
                    continue
                if not isinstance(request, dict) or request.get("type") != "observe":
                    raise ValueError("invalid inference RPC request")
                if set(request.get("images", {})) != expected_camera_names:
                    raise ValueError(
                        f"request cameras must be {sorted(expected_camera_names)}; "
                        f"got {sorted(request.get('images', {}))}"
                    )
                images = {}
                for name, jpeg in request["images"].items():
                    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                    if image is None:
                        raise ValueError(f"cannot decode JPEG for {name}")
                    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                    expected = expected_images.get(name)
                    if expected is None:
                        raise ValueError(f"model does not define image input {name!r}")
                    actual = (rgb.shape[2], rgb.shape[0], rgb.shape[1])
                    if actual != expected:
                        rgb = fit_rgb(cv2, rgb, expected, args.image_fit)
                        if name not in resized_reported:
                            print(
                                f"[model] {name}: camera {actual} -> model {expected} "
                                f"using {args.image_fit}", flush=True,
                            )
                            resized_reported.add(name)
                    images[f"observation.images.{name}"] = rgb
                observation = {"observation.state": np.asarray(request["state"], np.float32)}
                if observation["observation.state"].shape != state_shape:
                    raise ValueError(
                        f"live state shape {observation['observation.state'].shape}, "
                        f"expected {state_shape}"
                    )
                observation.update(images)
                observation_history.append(observation)

                harvest_finished_prediction()
                install_pending_prediction()
                if not action_queue and last_action is None:
                    # Only startup waits for a first plan. Once motion begins,
                    # model latency is never allowed to block the servo RPC.
                    start_prediction()
                    harvest_finished_prediction(wait=True)
                    install_pending_prediction()
                if action_queue:
                    result = action_queue.popleft()
                    last_action = result
                else:
                    underruns += 1
                    if prediction is None and pending_chunk is None:
                        start_prediction()
                    result = last_action
                if (
                    len(action_queue) <= prefetch_actions
                    and prediction is None
                    and pending_chunk is None
                ):
                    start_prediction()
                if result is None:
                    raise RuntimeError("asynchronous planner has no action to return")
                try:
                    send_message(conn, {
                        "action": np.asarray(result).tolist(),
                        "planner": {
                            "buffered": len(action_queue),
                            "generating": prediction is not None,
                            "pending": pending_chunk is not None,
                            "underruns": underruns,
                            "plan_age": installed_age,
                            "expired_predictions": expired_predictions,
                        },
                    })
                except BrokenPipeError:
                    return 0
                control_step += 1
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        listener.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    sys.exit(main())
