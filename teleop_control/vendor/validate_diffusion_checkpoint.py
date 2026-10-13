#!/usr/bin/env python3
"""Reject missing, truncated, or zero-filled LeRobot Diffusion checkpoints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from safetensors import safe_open


REQUIRED_FILES = (
    "config.json",
    "model.safetensors",
    "policy_preprocessor.json",
    "policy_preprocessor_step_3_normalizer_processor.safetensors",
    "policy_postprocessor.json",
    "policy_postprocessor_step_0_unnormalizer_processor.safetensors",
)
CRITICAL_TENSORS = (
    "diffusion.unet.down_modules.0.0.conv1.block.0.weight",
    "diffusion.unet.final_conv.0.block.0.weight",
    "diffusion.unet.final_conv.1.weight",
)


def fail(message: str) -> None:
    raise SystemExit(f"[checkpoint] INVALID: {message}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("checkpoint", type=Path)
    args = ap.parse_args()
    checkpoint = args.checkpoint.expanduser()
    if not checkpoint.is_dir():
        fail(f"directory not found: {checkpoint}")
    for name in REQUIRED_FILES:
        path = checkpoint / name
        if not path.is_file() or path.stat().st_size == 0:
            fail(f"required file is missing or empty: {path}")

    try:
        config = json.loads((checkpoint / "config.json").read_text())
        inputs = config["input_features"]
        outputs = config["output_features"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        fail(f"cannot parse policy config: {exc}")
    expected_inputs = {
        "observation.state",
        "observation.images.head_color",
        "observation.images.hand_right_color",
    }
    if set(inputs) != expected_inputs:
        fail(
            f"expected input features {sorted(expected_inputs)}, "
            f"got {sorted(inputs)}"
        )
    if tuple(inputs["observation.state"].get("shape", ())) != (8,):
        fail(f"expected 8-D state, got {inputs['observation.state'].get('shape')}")
    if tuple(outputs.get("action", {}).get("shape", ())) != (8,):
        fail(f"expected 8-D action, got {outputs.get('action', {}).get('shape')}")
    temporal = {
        name: config.get(name)
        for name in ("n_obs_steps", "horizon", "n_action_steps")
    }
    if (
        not all(isinstance(value, int) and value > 0 for value in temporal.values())
        or temporal["n_action_steps"] > temporal["horizon"]
    ):
        fail(
            f"invalid temporal config: {temporal}"
        )

    model_path = checkpoint / "model.safetensors"
    try:
        with safe_open(str(model_path), framework="pt", device="cpu") as weights:
            keys = set(weights.keys())
            for name in CRITICAL_TENSORS:
                if name not in keys:
                    fail(f"required tensor is missing: {name}")
                value = weights.get_tensor(name)
                if not torch.isfinite(value).all():
                    fail(f"tensor contains NaN/Inf: {name}")
                if value.numel() == 0 or torch.count_nonzero(value).item() == 0:
                    fail(
                        f"tensor is entirely zero: {name}. The checkpoint is likely "
                        "truncated or zero-filled; re-copy it and verify SHA-256."
                    )
    except (OSError, RuntimeError) as exc:
        fail(f"cannot read {model_path}: {exc}")

    print(
        f"[checkpoint] OK: {checkpoint} state=8 action=8 "
        f"cameras=head_color,hand_right_color temporal={temporal}",
        flush=True,
    )


if __name__ == "__main__":
    main()
