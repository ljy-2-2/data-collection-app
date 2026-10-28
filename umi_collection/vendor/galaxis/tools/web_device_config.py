#!/usr/bin/env python3
"""Apply a validated browser device selection without launching Tk."""

from __future__ import annotations

from copy import deepcopy
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import yaml


def build_camera_settings(
    active_roles: tuple[str, ...],
    requested_brightness: dict[str, int] | None,
    existing: dict | None = None,
) -> dict:
    """Merge confirmed hardware brightness without discarding saved controls."""
    if (
        not active_roles
        or any(role not in ("left", "right", "center") for role in active_roles)
        or len(set(active_roles)) != len(active_roles)
    ):
        raise ValueError("invalid active camera roles")
    brightness = {} if requested_brightness is None else requested_brightness
    if not isinstance(brightness, dict) or not set(brightness).issubset(active_roles):
        raise ValueError("camera brightness must refer only to active camera roles")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in brightness.values()):
        raise ValueError("camera brightness must be an integer")
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("invalid saved camera settings")

    settings = deepcopy(existing) if existing is not None else {}
    for side in active_roles:
        node_name = f"{side}_camera_brightness_controller"
        node = settings.setdefault(node_name, {})
        if not isinstance(node, dict):
            raise ValueError(f"invalid saved settings for {node_name}")
        parameters = node.setdefault("ros__parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError(f"invalid saved parameters for {node_name}")
        if side in brightness:
            parameters["brightness"] = brightness[side]
        else:
            parameters.setdefault("brightness", 0)
    if "center" in active_roles:
        settings.setdefault("center_camera_flip", {"ros__parameters": {"vertical_flip": False}})
    return settings


def encoder_samples() -> dict[str, tuple[str, float]]:
    import serial

    found: dict[str, tuple[str, float]] = {}
    for device in sorted(Path("/dev").glob("ttyUSB*")):
        port = serial.Serial(port=None, baudrate=115200, timeout=0.1)
        try:
            port.dtr = False
            port.rts = False
            port.port = str(device)
            port.open()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                line = port.readline().decode("utf-8", errors="ignore").strip()
                match = re.match(r"^([LRlr])[^,]*,\s*(-?[0-9.]+)$", line)
                if not match:
                    continue
                value = float(match.group(2))
                if math.isfinite(value):
                    side = "left" if match.group(1).lower() == "l" else "right"
                    found[side] = (str(device), value)
                    break
        finally:
            port.close()
    return found


def build_collection_profile(profile: str, roles: tuple[str, ...], modes: dict | None,
                             sources: dict | None = None) -> dict:
    defaults = dict(image_width=640, image_height=480, framerate=30.0, pixel_format='mjpeg2rgb')
    modes = {role: dict(defaults) for role in roles} if modes is None else modes
    if not isinstance(modes, dict) or set(modes) != set(roles):
        raise ValueError('每路启用相机都需要有效分辨率')
    cameras = {}
    for role in roles:
        mode = modes[role]
        if (not isinstance(mode, dict) or set(mode) != set(defaults)
                or any(type(mode[k]) is not int or not 0 < mode[k] <= 8192 for k in ('image_width','image_height'))
                or isinstance(mode['framerate'], bool) or not isinstance(mode['framerate'], (int,float))
                or not 29.9 <= mode['framerate'] <= 30.1
                or mode['pixel_format'] not in ('mjpeg2rgb','yuyv2rgb')):
            raise ValueError(f'{role} 相机需要支持约 30 帧/秒的原生分辨率')
        cameras[role] = dict(mode)
    sources = {'center':'usb'} if sources is None else sources
    if sources not in ({'center':'usb'}, {'center':'zed_mini'}):
        raise ValueError('unsupported center camera source')
    if sources['center'] == 'zed_mini' and (
            cameras['center']['pixel_format'] != 'yuyv2rgb' or
            (cameras['center']['image_width'],cameras['center']['image_height']) not in
            {(672,376),(1280,720),(1920,1080)}):
        raise ValueError('Invalid native ZED Mini mono resolution')
    return dict(collection_profile=profile, active_cameras=list(roles),
                active_arms=[role for role in ('left','right') if role in roles],
                camera_sources=sources, cameras=cameras,
                camera_quality=dict(version=2, min_rate_hz=27.0, rate_window_sec=5.0,
                                    warn_frame_gap_sec=0.5, stop_min_rate_hz=20.0,
                                    stop_low_rate_sec=10.0, max_frame_gap_sec=3.0, warmup_sec=1.0))


def write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def main() -> int:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    profile = payload["collection_profile"]
    roles: dict[str, str] = payload["camera_roles"]
    device_properties: dict[str, dict[str, str]] = payload["device_properties"]
    expected = {
        "dual": ("left", "right", "center"),
        "single-left": ("left", "center"),
        "single-right": ("right", "center"),
    }.get(profile)
    if expected is None or set(roles) != set(expected):
        raise ValueError("invalid collection profile or camera roles")
    if len(set(roles.values())) != len(roles):
        raise ValueError("camera roles must be distinct")
    for device in roles.values():
        if not re.fullmatch(r"/dev/video\d+", device) or not Path(device).is_char_device():
            raise ValueError(f"invalid camera device: {device}")

    config_dir = Path("/etc/galaxis")
    camera_settings_path = config_dir / "center_camera.yaml"
    existing_camera_settings = (
        yaml.safe_load(camera_settings_path.read_text(encoding="utf-8"))
        if camera_settings_path.exists() else None
    )
    camera_settings = build_camera_settings(
        expected, payload.get("camera_brightness"), existing_camera_settings
    )
    collection_settings = build_collection_profile(profile, expected, payload.get('camera_modes'),
                                                    payload.get('camera_sources'))

    encoders = encoder_samples()
    active_arms = tuple(role for role in ("left", "right") if role in expected)
    if any(side not in encoders for side in active_arms):
        raise RuntimeError("未收到所选左右编码器的 L/R 数据，请检查串口")
    requested_minima = payload.get("encoder_minima") or {}
    if requested_minima:
        if set(requested_minima) != set(active_arms):
            raise ValueError("missing active encoder minimum")
        if any(not math.isfinite(float(requested_minima[side])) for side in active_arms):
            raise ValueError("invalid active encoder minimum")

    rules_dir = Path("/etc/udev/rules.d")
    config_dir.mkdir(parents=True, exist_ok=True)
    rules_dir.mkdir(parents=True, exist_ok=True)
    write(
        config_dir / "encoder_limits.yaml",
        "encoder_node:\n  ros__parameters:\n"
        f"    active_sides: [{', '.join(active_arms)}]\n"
        + "".join(
            f"    {side}_min_raw: "
            f"{float(requested_minima[side]) if requested_minima else encoders[side][1]}\n"
            for side in active_arms
        ),
    )
    write(
        config_dir / "collection_profile.yaml",
        yaml.safe_dump(collection_settings, sort_keys=False, allow_unicode=True, default_flow_style=None),
    )
    write(
        camera_settings_path,
        yaml.safe_dump(camera_settings, sort_keys=False, allow_unicode=True),
    )

    rules = ["# Generated by the data-platform UMI web device setup"]
    for name in (
        "galaxis_left_camera",
        "galaxis_right_camera",
        "galaxis_center_camera",
        "galaxis_left_esp32c6",
        "galaxis_right_esp32c6",
    ):
        (Path("/dev") / name).unlink(missing_ok=True)
    for side, device in roles.items():
        info = device_properties[device]
        rules.append(
            'SUBSYSTEM=="video4linux", KERNEL=="video*", '
            f'ATTRS{{idVendor}}=="{info["ID_VENDOR_ID"]}", '
            f'ATTRS{{idProduct}}=="{info["ID_MODEL_ID"]}", ATTR{{index}}=="0", '
            f'ENV{{ID_PATH}}=="{info["ID_PATH"]}", SYMLINK+="galaxis_{side}_camera"'
        )
    for side in active_arms:
        device, _value = encoders[side]
        info = device_properties[device]
        rules.append(
            'SUBSYSTEM=="tty", '
            f'ENV{{ID_VENDOR_ID}}=="{info["ID_VENDOR_ID"]}", '
            f'ENV{{ID_MODEL_ID}}=="{info["ID_MODEL_ID"]}", '
            f'ENV{{ID_PATH}}=="{info["ID_PATH"]}", '
            f'SYMLINK+="galaxis_{side}_esp32c6", GROUP="plugdev", MODE="0660", '
            'ENV{ID_MM_DEVICE_IGNORE}="1"'
        )
        # Keep /dev role links relative (for example ``ttyUSB0``).  Some
        # consumers resolve the link and then prepend ``/dev`` themselves;
        # an absolute target would become the invalid
        # ``/dev//dev/ttyUSB0`` inside the collection container.
        Path(f"/dev/galaxis_{side}_esp32c6").symlink_to(Path(device).name)
    for side, device in roles.items():
        Path(f"/dev/galaxis_{side}_camera").symlink_to(Path(device).name)
    write(rules_dir / "99-galaxis-devices.rules", "\n".join(rules) + "\n")
    print(json.dumps({"profile": profile, "camera_roles": roles, "encoders": active_arms}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
