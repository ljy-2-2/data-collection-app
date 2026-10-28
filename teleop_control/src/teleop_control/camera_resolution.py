"""Discover native camera sizes; never upscale pixels to simulate resolution."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def usb_connection_info(device: str, *, sys_class: Path = Path('/sys/class/video4linux')) -> dict:
    """Report negotiated link speed, never infer the camera's USB capability.

    A SuperSpeed-capable camera can currently be connected at High Speed.
    Resolve device symlinks so by-id/by-path selections work on other PCs too.
    """
    try:
        name = Path(device).resolve().name
        if not re.fullmatch(r'video\d+', name):
            return {}
        interface = (sys_class / name / 'device').resolve(strict=True)
        for candidate in (interface, *interface.parents):
            if not (candidate / 'idVendor').is_file():
                continue
            speed = float((candidate / 'speed').read_text().strip())
            if not 0 < speed <= 1_000_000:
                return {}
            unit = f'{speed / 1000:g} Gbps' if speed >= 1000 else f'{speed:g} Mbps'
            return dict(speed_mbps=speed, usb_path=candidate.name,
                        label=f'USB 当前连接 {unit}')
    except (OSError, ValueError):
        pass
    return {}


def read_modes(device: str) -> list[dict]:
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--device", device, "--list-formats-ext"],
            capture_output=True, text=True, check=False, timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("无法读取相机分辨率，请检查相机连接和 v4l2-ctl") from exc
    if result.returncode:
        raise RuntimeError("无法读取相机分辨率：" + (result.stderr or result.stdout).strip())
    modes = parse_modes(result.stdout)
    from .camera_devices import zed
    return zed.mono_modes(modes) if zed.is_zed_device(device) else modes


def parse_modes(output: str) -> list[dict]:
    # Same discrete V4L2 mode discovery as the original collection GUI. Keep
    # 30 Hz (including 29.97). Keep both formats when advertised so a faster
    # replacement camera can use YUYV after the real three-camera FPS check.
    fourcc = None
    size = None
    modes = {}
    for line in output.splitlines():
        match = re.search(r"\[\d+\]:\s+'([^']+)'", line)
        if match:
            fourcc, size = match[1], None
            continue
        if "Size:" in line:
            match = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
            size = (int(match[1]), int(match[2])) if match else None
            continue
        match = re.search(r"Interval:\s+Discrete.*\(([\d.]+)\s+fps\)", line)
        if not match or not size or fourcc not in ("MJPG", "YUYV"):
            continue
        fps = float(match[1])
        if not 29.9 <= fps <= 30.1 or not all(0 < n <= 8192 for n in size):
            continue
        width, height = size
        mode = dict(id=f"{width}x{height}-{fourcc}-{fps:g}", width=width, height=height,
                    fps=fps, fourcc=fourcc,
                    pixel_format="mjpeg2rgb" if fourcc == "MJPG" else "yuyv2rgb",
                    label=f"{width}×{height} · {'MJPEG' if fourcc == 'MJPG' else 'YUYV'} · {fps:g}帧")
        modes[(*size, fourcc)] = mode
    return sorted(modes.values(), key=lambda m: (m['width']*m['height'], m['width'], m['fourcc'] != 'MJPG'))


def choose_mode(modes: list[dict], saved: dict | None = None) -> dict:
    if not modes:
        raise RuntimeError("该相机未提供可用的 30 帧分辨率，请重新检测或更换相机")
    if saved:
        for mode in modes:
            if mode['id'] == saved.get('id'):
                return dict(mode)
    if any(m.get('backend') == 'zed-uvc' for m in modes):
        # Stereo YUYV is uncompressed on USB. HD720 keeps bandwidth and CPU
        # available for the two wrist cameras and the 100 Hz sensors.
        hd = next((m for m in modes if (m['width'], m['height']) == (1280,720)), None)
        if hd:
            return dict(hd)
    # Prefer native Full HD at 30 Hz. A camera without this mode keeps its
    # highest supported size below Full HD; never upscale or silently use 15 Hz.
    full_hd = next((m for m in modes if (m['width'], m['height']) == (1920, 1080)), None)
    below = [m for m in modes if m['width'] <= 1920 and m['height'] <= 1080]
    return dict(full_hd or max(below or modes, key=lambda m: m['width']*m['height']))


def ros_parameters(mode: dict) -> dict:
    return dict(image_width=mode['width'], image_height=mode['height'],
                framerate=mode['fps'], pixel_format=mode['pixel_format'])
