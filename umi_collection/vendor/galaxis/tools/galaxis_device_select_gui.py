#!/usr/bin/env python3
"""Interactive GUI to assign camera and ESP32 roles for dual/single-arm runs.

The result is printed as JSON on stdout for downstream udev-rule generation.

Stationary keeps its legacy L/R setup. Portable first offers dual arm
(L/R/center + two encoders), single-left (L/center + one encoder), or
single-right (R/center + one encoder). Setup opens with however many candidate
ports are currently available; APPLY validates the selected profile.

The L/R assignment is bootstrapped from two pieces of side-channel information:
  1. The encoder firmware may emit lines prefixed with "L001," / "R001,". When
     present, that prefix tells us which side a given /dev/ttyACM* belongs to.
  2. Each L/R encoder shares a USB hub with its matching camera. Once we know
     the encoder's side from (1), we propagate the assignment to its hub-mate
     camera by comparing USB topology paths (e.g. "1-1.4.x"). In portable
     mode the remaining (un-matched) camera is the CENTER/head camera.

Both signals are best-effort. The user can correct any assignment with the
LEFT<->RIGHT Swap buttons, or with the per-camera pulldown (which auto-swaps
the conflicting slot to keep all three roles distinct).

Heavy GUI dependencies (cv2/serial/tkinter/PIL/matplotlib) are imported lazily
inside the runtime classes, so unit tests can import the module-level pure
helpers (parse_line, usb_hub_of, validate_cam_count, assign_camera_roles,
resolve_camera_pick, can_apply_portable, build_apply_payload) without
installing those packages.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Pure helpers — no heavy deps, safe to import from tests.
# ---------------------------------------------------------------------------

# Matches a USB topology path component such as "1-1.4" or "1-1.4.2".
_USB_LOC_RE = re.compile(r"^\d+-[\d.]+$")

SUPPORTED_VARIANTS = ("stationary", "portable")
SUPPORTED_COLLECTION_PROFILES = ("dual", "single-left", "single-right")
PROFILE_CAMERA_SIDES: Dict[str, Tuple[str, ...]] = {
    "dual": ("left", "right", "center"),
    "single-left": ("left", "center"),
    "single-right": ("right", "center"),
}
PROFILE_ARM_SIDES: Dict[str, Tuple[str, ...]] = {
    "dual": ("left", "right"),
    "single-left": ("left",),
    "single-right": ("right",),
}
CAM_SIDES_BY_VARIANT: Dict[str, Tuple[str, ...]] = {
    "stationary": ("left", "right"),
    "portable": ("left", "right", "center"),
}
EXPECTED_CAM_COUNT: Dict[str, int] = {
    "stationary": 2,
    "portable": 3,
}
PICO_ACTIVITY = "com.galaxis.vrteleop/com.unity3d.player.UnityPlayerActivity"
CENTER_CAMERA_SETTINGS_PATH = "/etc/galaxis/center_camera.yaml"
COLLECTION_PROFILE_PATH = "/etc/galaxis/collection_profile.yaml"
G2_CENTER_SOURCE = "g2_head_color"
CENTER_CAMERA_SOURCES = ("usb", G2_CENTER_SOURCE)
CENTER_CAMERA_SOURCE_LABELS = {
    "usb": "USB 中心相机",
    G2_CENTER_SOURCE: "G2 头部彩色相机（GDK）",
}
_V4L2_TO_ROS_FORMAT = {
    "MJPG": "mjpeg2rgb",
    "YUYV": "yuyv2rgb",
}


@dataclass(frozen=True)
class CameraMode:
    """One exact V4L2 mode that usb_cam can request."""

    width: int
    height: int
    framerate: float
    pixel_format: str
    fourcc: str

    @property
    def label(self) -> str:
        fps = f"{self.framerate:.3f}".rstrip("0").rstrip(".")
        return f"{self.width}x{self.height} @ {fps} FPS · {self.fourcc}"

    def as_params(self) -> Dict[str, Any]:
        return {
            "image_width": self.width,
            "image_height": self.height,
            "framerate": self.framerate,
            "pixel_format": self.pixel_format,
        }


FORCED_CENTER_MODE = CameraMode(640, 480, 30.0, "yuyv2rgb", "YUYV")


def parse_v4l2_modes(output: str) -> List[CameraMode]:
    """Parse discrete modes from ``v4l2-ctl --list-formats-ext`` output."""
    fourcc: Optional[str] = None
    size: Optional[Tuple[int, int]] = None
    modes: List[CameraMode] = []
    seen = set()
    for line in output.splitlines():
        format_match = re.search(r"\[\d+\]:\s+'([^']+)'", line)
        if format_match:
            fourcc = format_match.group(1).upper()
            size = None
            continue
        size_match = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if size_match:
            size = (int(size_match.group(1)), int(size_match.group(2)))
            continue
        fps_match = re.search(r"\(([0-9]+(?:\.[0-9]+)?)\s+fps\)", line, re.I)
        if not fps_match or fourcc not in _V4L2_TO_ROS_FORMAT or size is None:
            continue
        mode = CameraMode(
            width=size[0],
            height=size[1],
            framerate=float(fps_match.group(1)),
            pixel_format=_V4L2_TO_ROS_FORMAT[fourcc],
            fourcc=fourcc,
        )
        key = (mode.width, mode.height, mode.framerate, mode.pixel_format)
        if key not in seen:
            seen.add(key)
            modes.append(mode)
    return modes


def choose_camera_mode(
    modes: List[CameraMode], saved: Optional[Dict[str, Any]] = None
) -> Optional[CameraMode]:
    """Choose a saved exact mode, then the closest safe 640x480@30 mode."""
    if not modes:
        return None
    if isinstance(saved, dict):
        try:
            wanted = (
                int(saved["image_width"]),
                int(saved["image_height"]),
                float(saved["framerate"]),
                str(saved["pixel_format"]),
            )
        except (KeyError, TypeError, ValueError):
            wanted = None
        if wanted is not None:
            for mode in modes:
                actual = (
                    mode.width,
                    mode.height,
                    mode.framerate,
                    mode.pixel_format,
                )
                if actual == wanted:
                    return mode
    return min(
        modes,
        key=lambda mode: (
            mode.width != 640 or mode.height != 480,
            abs(mode.framerate - 30.0),
            mode.pixel_format != "yuyv2rgb",
            -(mode.width * mode.height),
        ),
    )


def load_saved_camera_modes(
    path: str = COLLECTION_PROFILE_PATH,
) -> Dict[str, Dict[str, Any]]:
    """Load the small camera-mode mapping without requiring PyYAML in the GUI."""
    try:
        with open(path, encoding="utf-8") as profile_file:
            text = profile_file.read()
    except OSError:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    current: Optional[str] = None
    in_cameras = False
    for line in text.splitlines():
        if re.match(r"^cameras:\s*$", line):
            in_cameras = True
            continue
        if in_cameras and line and not line.startswith(" "):
            break
        side_match = re.match(r"^\s{2}(left|right|center):\s*$", line)
        if in_cameras and side_match:
            current = side_match.group(1)
            result[current] = {}
            continue
        value_match = re.match(
            r"^\s{4}(image_width|image_height|framerate|pixel_format):\s*(\S+)\s*$",
            line,
        )
        if in_cameras and current and value_match:
            key, value = value_match.groups()
            result[current][key] = value if key == "pixel_format" else float(value)
    return result


def load_saved_center_camera_source(path: str = COLLECTION_PROFILE_PATH) -> str:
    """Read the optional center source while preserving USB for old profiles."""
    try:
        with open(path, encoding="utf-8") as profile_file:
            text = profile_file.read()
    except OSError:
        return "usb"
    in_sources = False
    for line in text.splitlines():
        if re.match(r"^camera_sources:\s*$", line):
            in_sources = True
            continue
        if in_sources and line and not line.startswith(" "):
            break
        match = re.match(r"^\s{2}center:\s*['\"]?([^'\"\s]+)", line)
        if in_sources and match:
            source = match.group(1)
            return source if source in CENTER_CAMERA_SOURCES else "usb"
    return "usb"


def cam_sides_for(variant: str) -> Tuple[str, ...]:
    """Return the ordered tuple of camera role names for the given variant."""
    if variant not in CAM_SIDES_BY_VARIANT:
        raise ValueError(f"unknown variant: {variant!r}")
    return CAM_SIDES_BY_VARIANT[variant]


def camera_sides_for_profile(variant: str, profile: str) -> Tuple[str, ...]:
    """Return camera roles for a setup profile without changing stationary."""
    if variant != "portable":
        return cam_sides_for(variant)
    try:
        return PROFILE_CAMERA_SIDES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown collection profile: {profile!r}") from exc


def arm_sides_for_profile(variant: str, profile: str) -> Tuple[str, ...]:
    """Return active encoder/actuator sides for a setup profile."""
    if variant != "portable":
        return ("left", "right")
    try:
        return PROFILE_ARM_SIDES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown collection profile: {profile!r}") from exc


def expected_camera_count(variant: str, profile: str) -> int:
    return len(camera_sides_for_profile(variant, profile))


def validate_cam_count(variant: str, count: int) -> Optional[str]:
    """Validate only the variant; candidate count must not block Setup launch."""
    if variant not in EXPECTED_CAM_COUNT:
        return (
            f"unknown variant {variant!r}; "
            f"expected one of {sorted(EXPECTED_CAM_COUNT)}"
        )
    return None


def load_center_vertical_flip(path: str = CENTER_CAMERA_SETTINGS_PATH) -> bool:
    """Read the previously applied center-camera flip setting, if present."""
    try:
        with open(path, encoding="utf-8") as settings:
            for line in settings:
                match = re.match(r"^\s*vertical_flip\s*:\s*(true|false)\s*$", line, re.I)
                if match:
                    return match.group(1).lower() == "true"
    except OSError:
        pass
    return True


def load_center_brightness(path: str = CENTER_CAMERA_SETTINGS_PATH) -> int:
    """Read the previously applied center-camera brightness, defaulting to 0."""
    try:
        with open(path, encoding="utf-8") as settings:
            for line in settings:
                match = re.match(r"^\s*brightness\s*:\s*(-?\d+)\s*$", line, re.I)
                if match:
                    return int(match.group(1))
    except OSError:
        pass
    return 0


def parse_line(line: bytes) -> Tuple[Optional[str], Optional[float]]:
    """Parse a serial line into (side, value).

    Accepts both labelled and unlabelled forms:
      "L001,0.12345" -> ("left", 0.12345)
      "R001,0.12345" -> ("right", 0.12345)
      "0.12345"      -> (None, 0.12345)

    The label form is what auto-assignment hinges on; the unlabelled form is
    still common in older firmware so we accept it and rely on the user to set
    the side manually via Swap.
    """
    try:
        s = line.decode("utf-8", errors="ignore").strip()
    except Exception:
        return None, None
    if not s or s.startswith("#"):
        return None, None
    side: Optional[str] = None
    num_str = s
    if "," in s:
        # Labelled form: split on the first comma so the head can be inspected.
        # Use [:1] (not [0]) so an empty head doesn't raise IndexError.
        head, num_str = s.split(",", 1)
        head = head.strip()
        if head[:1] in ("L", "l"):
            side = "left"
        elif head[:1] in ("R", "r"):
            side = "right"
    try:
        return side, float(num_str.strip())
    except ValueError:
        return side, None


def usb_hub_of(loc: Optional[str]) -> Optional[str]:
    """Return the parent hub of a USB topology location.

    '1-1.4.2' -> '1-1.4'   (downstream of hub 1-1.4)
    '1-2'     -> '1'       (root hub)

    Two devices share a hub iff this returns the same string for both. That
    equality is what ``assign_camera_roles`` uses to pair cameras to encoders.
    """
    if not loc:
        return None
    if "." in loc:
        return loc.rsplit(".", 1)[0]
    if "-" in loc:
        return loc.split("-", 1)[0]
    return loc


def assign_camera_roles(
    cam_devs_with_hub: List[Tuple[str, Optional[str]]],
    left_esp_hub: Optional[str],
    right_esp_hub: Optional[str],
    variant: str,
) -> Dict[str, Any]:
    """Decide which physical camera dev plays each role for the given variant.

    Inputs:
      cam_devs_with_hub: ordered list of (dev_path, hub) for every camera.
                         Order is treated as the fallback ordering when hub
                         matching fails.
      left_esp_hub, right_esp_hub: hub strings (output of usb_hub_of) of the
                                   left/right ESP devices. May be None when
                                   unknown — auto-match is skipped in that case.
      variant: "stationary" (assign left/right) or "portable" (also center).

    Returns:
      {
        "left": dev|None,
        "right": dev|None,
        "center": dev|None,        # always present for portable; None for stationary
        "matched_left": bool,      # True iff a hub-mate camera was found for L_esp
        "matched_right": bool,
      }

    Determinism: if multiple cameras share the same hub as L_esp (or hub equals
    R's hub), the FIRST one in input order wins. Ambiguity is reported only via
    the matched_* flags; callers are expected to surface a warning.
    """
    if variant not in CAM_SIDES_BY_VARIANT:
        raise ValueError(f"unknown variant: {variant!r}")
    expected = EXPECTED_CAM_COUNT[variant]
    if len(cam_devs_with_hub) < expected:
        raise ValueError(
            f"variant={variant} needs at least {expected} cameras, "
            f"got {len(cam_devs_with_hub)}"
        )

    devs = [d for (d, _h) in cam_devs_with_hub]
    hubs = {d: h for (d, h) in cam_devs_with_hub}

    left_dev: Optional[str] = None
    right_dev: Optional[str] = None
    matched_left = False
    matched_right = False

    if left_esp_hub:
        for d in devs:
            if hubs.get(d) == left_esp_hub:
                left_dev = d
                matched_left = True
                break

    if right_esp_hub:
        for d in devs:
            if d == left_dev:
                continue
            if hubs.get(d) == right_esp_hub:
                right_dev = d
                matched_right = True
                break

    # Fallback: fill any unset role from the remaining devices in input order.
    used = {x for x in (left_dev, right_dev) if x is not None}
    remaining_iter = iter([d for d in devs if d not in used])
    if left_dev is None:
        left_dev = next(remaining_iter, None)
        if left_dev is not None:
            used.add(left_dev)
    if right_dev is None:
        # rebuild iter so we skip the freshly-claimed left_dev
        remaining_iter = iter([d for d in devs if d not in used])
        right_dev = next(remaining_iter, None)
        if right_dev is not None:
            used.add(right_dev)

    center_dev: Optional[str] = None
    if variant == "portable":
        # The first remaining camera is the head camera.  Extra candidates
        # remain available in the UI pulldown for the operator to choose.
        leftover = [d for d in devs if d not in used]
        center_dev = leftover[0] if leftover else None

    return {
        "left": left_dev,
        "right": right_dev,
        "center": center_dev,
        "matched_left": matched_left,
        "matched_right": matched_right,
    }


def assign_single_arm_camera_roles(
    cam_devs_with_hub: List[Tuple[str, Optional[str]]],
    encoder_hub: Optional[str],
    arm_side: str,
) -> Dict[str, Any]:
    """Assign the selected hand camera by USB hub and CENTER from leftovers."""
    if arm_side not in ("left", "right"):
        raise ValueError(f"unknown arm side: {arm_side!r}")
    if len(cam_devs_with_hub) < 2:
        raise ValueError(f"single-arm setup needs at least 2 cameras, got {len(cam_devs_with_hub)}")
    hand = None
    matched = False
    if encoder_hub:
        hand = next((dev for dev, hub in cam_devs_with_hub if hub == encoder_hub), None)
        matched = hand is not None
    if hand is None:
        hand = cam_devs_with_hub[0][0]
    center = next(dev for dev, _hub in cam_devs_with_hub if dev != hand)
    return {arm_side: hand, "center": center, "matched_arm": matched}


def resolve_camera_pick(
    roles: Dict[str, Optional[str]],
    side: str,
    new_dev: str,
    sides: Tuple[str, ...],
) -> Dict[str, Optional[str]]:
    """Apply a pulldown selection on `side`, auto-swapping any conflicting slot.

    Returns a new dict; does not mutate the input.

    - If `new_dev` is already in `roles[side]`, the dict is unchanged (modulo
      copy).
    - If `new_dev` currently lives in another slot `other`, we swap the two
      slots so all three sides remain assigned to distinct devices.
    - If `new_dev` is not assigned anywhere yet (shouldn't happen in normal
      flow, since the pulldown options are the same dev pool), we still set
      `roles[side] = new_dev`. The previously-held dev at `side` becomes
      unassigned — caller may surface that as a warning.
    """
    if side not in sides:
        raise ValueError(f"side {side!r} not in {sides!r}")
    out = {s: roles.get(s) for s in sides}
    current = out.get(side)
    if current == new_dev:
        return out
    other_side: Optional[str] = None
    for s in sides:
        if s == side:
            continue
        if out.get(s) == new_dev:
            other_side = s
            break
    out[side] = new_dev
    if other_side is not None:
        out[other_side] = current
    return out


def can_apply_portable(
    esp_side_detected: Dict[str, Optional[str]],
    cam_role: Dict[str, Optional[str]],
) -> Optional[str]:
    """Return an error string if portable APPLY is not ready, else None.

    Checks (in priority order):
      1. Both ESP devnodes have observed a labelled side ("L001," / "R001,").
         This is the "gripper L/R info must be flowing" requirement.
      2. All three camera roles (left/right/center) are set.
      3. The three camera devs are mutually distinct.
    """
    missing_label = [dev for dev, side in esp_side_detected.items() if not side]
    if missing_label:
        return (
            "Encoder L/R label not observed yet on: "
            + ", ".join(sorted(missing_label))
            + ". Wait for 'L001,' / 'R001,' lines from both grippers before APPLY."
        )

    unset = [s for s in ("left", "right", "center") if not cam_role.get(s)]
    if unset:
        return f"Camera assignment incomplete: {', '.join(unset)} unset."

    devs = [cam_role["left"], cam_role["right"], cam_role["center"]]
    if len(set(devs)) != 3:
        return (
            f"Camera roles must be distinct devices, got "
            f"left={cam_role['left']} right={cam_role['right']} "
            f"center={cam_role['center']}."
        )
    return None


def build_apply_payload(
    cam_role: Dict[str, Optional[str]],
    esp_role: Dict[str, Optional[str]],
    esp_serials: Dict[str, str],
    calib_min: Dict[str, Optional[float]],
    variant: str,
    center_vertical_flip: bool = False,
    collection_profile: str = "dual",
    camera_brightness_by_dev: Optional[Dict[str, float]] = None,
    camera_mode_by_side: Optional[Dict[str, CameraMode]] = None,
    center_camera_source: str = "usb",
) -> Dict[str, Any]:
    """Build the JSON payload emitted by the GUI on APPLY.

    Stationary keeps the original shape (no "center" key). Portable adds
    ``camera.center.dev``.
    """
    camera_sides = camera_sides_for_profile(variant, collection_profile)
    arm_sides = arm_sides_for_profile(variant, collection_profile)
    brightness_by_dev = camera_brightness_by_dev or {}
    modes_by_side = camera_mode_by_side or {}
    out: Dict[str, Any] = {
        "collection_profile": collection_profile,
        "active_cameras": list(camera_sides),
        "active_arms": list(arm_sides),
        "camera": {
            side: {
                "dev": cam_role[side],
                "brightness": int(round(brightness_by_dev.get(cam_role[side], 0.0))),
            }
            for side in camera_sides
        },
        "esp32": {},
        "encoder_node": {"ros__parameters": {"active_sides": list(arm_sides)}},
    }
    for side in camera_sides:
        mode = modes_by_side.get(side)
        if mode is not None:
            out["camera"][side].update(mode.as_params())
    for side in arm_sides:
        dev = esp_role[side]
        out["esp32"][side] = {
            "dev": dev,
            "serial": esp_serials[dev],
            "min": calib_min[dev],
        }
        out["encoder_node"]["ros__parameters"][f"{side}_min_raw"] = calib_min[dev]
    if variant == "portable" and "center" in camera_sides:
        if center_camera_source not in CENTER_CAMERA_SOURCES:
            raise ValueError(f"unsupported center camera source: {center_camera_source!r}")
        out["camera_sources"] = {"center": center_camera_source}
        out["camera"]["center"]["source"] = center_camera_source
        out["camera"]["center"].update(FORCED_CENTER_MODE.as_params())
        if center_camera_source == G2_CENTER_SOURCE:
            out["camera"]["center"].pop("dev", None)
            out["camera"]["center"].pop("brightness", None)
        out["camera"]["center"]["vertical_flip"] = bool(center_vertical_flip)
    return out


# ---------------------------------------------------------------------------
# Below this line: runtime code that needs cv2/serial/Tk/PIL/matplotlib.
# Imports are deferred into functions/classes so tests can use the module
# without those packages.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EspDev:
    device: str          # /dev/ttyACM*
    vid: int
    pid: int
    serial: str          # serial_number (ID_SERIAL_SHORT相当). Used for stable udev rules.
    location: str        # USB topology path (e.g. "1-1.4.1") - used to pair with camera by hub.


def find_esp_devices(vid_hex: str, pid_hex: str) -> List[EspDev]:
    from serial.tools import list_ports  # lazy

    vid = int(vid_hex, 16)
    pid = int(pid_hex, 16)
    out: List[EspDev] = []
    for p in list_ports.comports():
        if p.vid is None or p.pid is None:
            continue
        if int(p.vid) != vid or int(p.pid) != pid:
            continue
        out.append(
            EspDev(
                device=p.device,
                vid=int(p.vid),
                pid=int(p.pid),
                serial=(p.serial_number or ""),
                location=(p.location or ""),
            )
        )
    out.sort(key=lambda d: (d.serial or "", d.device))
    return out


def list_usb_serial_devices() -> List[EspDev]:
    """Return all USB-backed serial ports that pyserial can identify."""
    from serial.tools import list_ports  # lazy

    out: List[EspDev] = []
    for p in list_ports.comports():
        if p.vid is None or p.pid is None:
            continue
        out.append(
            EspDev(
                device=p.device,
                vid=int(p.vid),
                pid=int(p.pid),
                serial=(p.serial_number or ""),
                location=(p.location or ""),
            )
        )
    out.sort(key=lambda d: (d.location or "", d.device))
    return out


def probe_encoder_port(
    candidate: EspDev,
    baud: int,
    timeout_sec: float = 4.5,
    serial_class: Optional[Callable[..., Any]] = None,
) -> Tuple[Optional[str], str]:
    """Accept a port only after seeing a finite, side-labelled encoder sample.

    DTR/RTS are disabled before opening because asserting either line can reset
    common ESP32 and USB-UART boards. ROM boot text and unlabelled numeric data
    are deliberately ignored so another serial peripheral cannot be selected.
    """
    if serial_class is None:
        import serial  # lazy

        serial_class = serial.Serial

    ser = None
    try:
        ser = serial_class(port=None, baudrate=baud, timeout=0.1)
        ser.dtr = False
        ser.rts = False
        ser.port = candidate.device
        ser.open()
        try:
            ser.reset_input_buffer()
        except (AttributeError, OSError):
            pass

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            line = ser.readline()
            side, value = parse_line(line)
            if side in ("left", "right") and value is not None and math.isfinite(value):
                sample = line.decode("utf-8", errors="replace").strip()
                return side, f"accepted as {side}: {sample}"
        return None, f"rejected: no L/R-labelled sample within {timeout_sec:.1f}s"
    except Exception as exc:
        return None, f"rejected: {type(exc).__name__}: {exc}"
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass


def auto_find_esp_devices(
    baud: int,
    candidates: Optional[List[EspDev]] = None,
    probe_func: Optional[Callable[[EspDev, int], Tuple[Optional[str], str]]] = None,
) -> Tuple[List[EspDev], Dict[str, str]]:
    """Probe USB serial ports in parallel and return protocol-compatible ones."""
    ports = list_usb_serial_devices() if candidates is None else list(candidates)
    if not ports:
        return [], {}
    probe = probe_encoder_port if probe_func is None else probe_func
    accepted: List[EspDev] = []
    diagnostics: Dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(ports))) as pool:
        futures = {pool.submit(probe, port, baud): port for port in ports}
        for future in concurrent.futures.as_completed(futures):
            port = futures[future]
            try:
                side, detail = future.result()
            except Exception as exc:
                side, detail = None, f"rejected: {type(exc).__name__}: {exc}"
            diagnostics[port.device] = (
                f"VID:PID={port.vid:04x}:{port.pid:04x} {detail}"
            )
            if side in ("left", "right"):
                accepted.append(port)
    accepted.sort(key=lambda d: (d.location or "", d.device))
    return accepted, diagnostics


def video_dev_usb_location(dev_path: str) -> Optional[str]:
    """Return the USB topology location (e.g. '1-1.4.2') for a /dev/video* path.

    Mirrors the format pyserial reports as ``ListPortInfo.location`` for
    ttyACM devices, so the result can be compared directly against
    ``EspDev.location`` to decide whether a camera and an ESP share a USB hub.
    """
    name = os.path.basename(dev_path)
    sysfs = f"/sys/class/video4linux/{name}/device"
    try:
        real = os.path.realpath(sysfs)
    except OSError:
        return None
    last: Optional[str] = None
    for p in real.split("/"):
        if _USB_LOC_RE.match(p):
            last = p
    return last


def find_camera_candidates() -> List[str]:
    """Return current capture-capable V4L2 devices with stable USB paths."""
    candidates: List[str] = []
    for dev_path in sorted(glob.glob("/dev/video*")):
        name = os.path.basename(dev_path)
        try:
            with open(f"/sys/class/video4linux/{name}/index", encoding="utf-8") as index:
                if index.read().strip() != "0":
                    continue
        except OSError:
            continue
        if video_dev_usb_location(dev_path):
            candidates.append(dev_path)
    return candidates


def query_camera_modes(dev_path: str) -> List[CameraMode]:
    """Return exact usb_cam-compatible modes advertised by one V4L2 device."""
    import subprocess

    result = subprocess.run(
        ["v4l2-ctl", f"--device={dev_path}", "--list-formats-ext"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return parse_v4l2_modes(result.stdout) if result.returncode == 0 else []


def _run_gui(args: argparse.Namespace) -> None:
    """Build the Tk app and run the mainloop. All heavy imports stay in here."""
    import queue
    import socket
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path

    import cv2
    import serial
    import tkinter as tk
    from tkinter import ttk, messagebox

    from PIL import Image, ImageTk
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

    palette = {
        # Kept in sync with GalaxisTeleopUI.cs in Galaxis-VR-Teleop.
        "page": "#e8f2fa",
        "surface": "#ffffff",
        "surface_soft": "#f2f9fe",
        "blue_soft": "#e0f0ff",
        "blue": "#1f6ee8",
        "blue_bright": "#4094ff",
        "ink": "#0e1f36",
        "muted": "#5c708a",
        "line": "#d1e0f0",
        "green": "#0d9e5c",
        "orange": "#f08c29",
        "red": "#d6303b",
        "terminal": "#0e1724",
        "terminal_text": "#add6bf",
    }

    def configure_galaxis_theme(window: tk.Tk) -> None:
        """Apply the visual language used by the headset teleoperation UI."""
        window.configure(background=palette["page"])
        style = ttk.Style(window)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("TFrame", background=palette["page"])
        style.configure(
            "Card.TFrame",
            background=palette["surface"],
            bordercolor=palette["line"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabel",
            background=palette["page"],
            foreground=palette["ink"],
            font=("Sans", 10),
        )
        style.configure(
            "Card.TLabel", background=palette["surface"], foreground=palette["ink"]
        )
        style.configure(
            "Muted.Card.TLabel",
            background=palette["surface"],
            foreground=palette["muted"],
            font=("Sans", 9),
        )
        style.configure(
            "CardTitle.TLabel",
            background=palette["surface"],
            foreground=palette["ink"],
            font=("Sans", 12, "bold"),
        )
        style.configure(
            "Hero.TLabel",
            background=palette["surface"],
            foreground=palette["ink"],
            font=("Sans", 18, "bold"),
        )
        style.configure(
            "HeroSub.TLabel",
            background=palette["surface"],
            foreground=palette["muted"],
            font=("Sans", 9),
        )
        style.configure(
            "Brand.TLabel",
            background=palette["blue"],
            foreground="white",
            font=("Sans", 11, "bold"),
            padding=(12, 6),
        )
        style.configure(
            "Profile.TLabel",
            background=palette["blue_soft"],
            foreground=palette["blue"],
            font=("Sans", 9, "bold"),
            padding=(10, 5),
        )
        style.configure(
            "Role.TLabel",
            background=palette["page"],
            foreground=palette["blue"],
            font=("Sans", 10, "bold"),
        )
        style.configure(
            "Status.TLabel",
            background=palette["surface_soft"],
            foreground=palette["muted"],
            padding=(10, 7),
        )
        style.configure(
            "Card.TLabelframe",
            background=palette["surface"],
            bordercolor=palette["line"],
            lightcolor=palette["line"],
            darkcolor=palette["line"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=palette["surface"],
            foreground=palette["ink"],
            font=("Sans", 11, "bold"),
            padding=(4, 2),
        )
        style.configure(
            "TButton",
            background=palette["blue_soft"],
            foreground=palette["blue"],
            bordercolor=palette["blue_soft"],
            font=("Sans", 9, "bold"),
            padding=(10, 8),
        )
        style.map(
            "TButton",
            background=[("active", "#d1e8ff"), ("pressed", palette["blue"])],
            foreground=[("pressed", "white")],
        )
        style.configure(
            "Primary.TButton",
            background=palette["blue"],
            foreground="white",
            bordercolor=palette["blue"],
            padding=(14, 10),
        )
        style.map(
            "Primary.TButton",
            background=[("active", palette["blue_bright"]), ("pressed", "#1457bd")],
            foreground=[("disabled", "#dbe8f5")],
        )
        style.configure(
            "Quiet.TButton",
            background=palette["surface_soft"],
            foreground=palette["muted"],
            bordercolor=palette["line"],
        )
        style.configure(
            "Nav.TButton",
            background=palette["surface"],
            foreground=palette["muted"],
            bordercolor=palette["surface"],
            padding=(14, 9),
        )
        style.map(
            "Nav.TButton",
            background=[("active", palette["blue_soft"])],
            foreground=[("active", palette["blue"])],
        )
        style.configure(
            "NavActive.TButton",
            background=palette["blue"],
            foreground="white",
            bordercolor=palette["blue"],
            padding=(14, 9),
        )
        style.configure(
            "TCombobox",
            fieldbackground=palette["surface_soft"],
            background=palette["surface_soft"],
            foreground=palette["ink"],
            bordercolor=palette["line"],
            arrowcolor=palette["blue"],
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", palette["surface_soft"])],
            selectbackground=[("readonly", palette["surface_soft"])],
            selectforeground=[("readonly", palette["ink"])],
        )
        style.configure(
            "Horizontal.TScale",
            background=palette["surface"],
            troughcolor=palette["blue_soft"],
        )
        style.configure(
            "Preview.TLabel",
            background=palette["terminal"],
            foreground=palette["terminal_text"],
            anchor="center",
        )

    def choose_collection_profile() -> str:
        """Show the setup-mode buttons before opening any hardware handles."""
        if args.variant != "portable":
            return "dual"
        if args.collection_profile != "auto":
            return args.collection_profile
        selected: Dict[str, Optional[str]] = {"value": None}
        picker_root = tk.Tk()
        configure_galaxis_theme(picker_root)
        picker_root.title("Galaxis · 选择采集配置")
        picker_root.resizable(False, False)
        frame = ttk.Frame(picker_root, padding=24, style="Card.TFrame")
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="GALAXIS", style="Brand.TLabel").grid(
            row=0, column=0, columnspan=2, pady=(0, 12)
        )
        ttk.Label(frame, text="选择本次采集硬件", style="Hero.TLabel").grid(
            row=1, column=0, columnspan=2
        )
        ttk.Label(
            frame,
            text="COLLECTION PROFILE  ·  之后仍可在 Setup 中调整每台设备",
            style="HeroSub.TLabel",
        ).grid(
            row=2, column=0, columnspan=2, pady=(4, 16)
        )

        def pick(value: str) -> None:
            selected["value"] = value
            picker_root.destroy()

        ttk.Button(
            frame,
            text="双臂\n3 台相机 + 2 路执行器",
            command=lambda: pick("dual"),
            width=28,
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Button(
            frame,
            text="单左臂\nLEFT + CENTER 相机 + 1 路执行器",
            command=lambda: pick("single-left"),
            width=28,
        ).grid(row=4, column=0, sticky="ew", padx=(0, 4), pady=6)
        ttk.Button(
            frame,
            text="单右臂\nRIGHT + CENTER 相机 + 1 路执行器",
            command=lambda: pick("single-right"),
            width=28,
        ).grid(row=4, column=1, sticky="ew", padx=(4, 0), pady=6)
        picker_root.protocol("WM_DELETE_WINDOW", picker_root.destroy)
        picker_root.mainloop()
        if selected["value"] is None:
            raise SystemExit(0)
        return selected["value"]

    # Reuse the production wire-format decoder.  The setup program must show
    # exactly the same PICO poses that the ROS bridge will publish later.
    bridge_root = Path(__file__).resolve().parents[1] / "galaxis_pico" / "galaxis_pico_bridge"
    if str(bridge_root) not in sys.path:
        sys.path.insert(0, str(bridge_root))
    from galaxis_pico_bridge.transport.galaxis_tcp import (  # noqa: PLC0415
        GalaxisPacketDecoder,
        normalize_tracking_payload,
    )

    class ScrollableFrame(ttk.Frame):
        """Vertical scrollbar wrapper to keep the UI usable on small displays."""

        def __init__(self, parent):
            super().__init__(parent)
            self.canvas = tk.Canvas(
                self, highlightthickness=0, background=palette["page"]
            )
            self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=self.scrollbar.set)

            self.canvas.grid(row=0, column=0, sticky="nsew")
            self.scrollbar.grid(row=0, column=1, sticky="ns")
            self.columnconfigure(0, weight=1)
            self.rowconfigure(0, weight=1)

            self.body = ttk.Frame(self.canvas)
            self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

            self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
            self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))

            self.canvas.bind("<MouseWheel>", self._on_mousewheel)
            self.canvas.bind("<Button-4>", self._on_mousewheel)
            self.canvas.bind("<Button-5>", self._on_mousewheel)

        def _on_mousewheel(self, event):
            if event.delta:
                direction = -1 if event.delta > 0 else 1
                self.canvas.yview_scroll(direction, "units")
            elif event.num == 4:
                self.canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self.canvas.yview_scroll(1, "units")

    class EspSerialReader(threading.Thread):
        def __init__(self, devnode: str, baud: int, out_q: "queue.Queue"):
            super().__init__(daemon=True)
            self.devnode = devnode
            self.baud = baud
            self.out_q = out_q
            self.stop_flag = threading.Event()
            self.ser: Optional[serial.Serial] = None

        def stop(self):
            self.stop_flag.set()
            # Closing the serial object wakes readline() promptly, so the
            # owning Tk process never exits while this worker still owns it.
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass

        def run(self):
            try:
                self.ser = serial.Serial(self.devnode, baudrate=self.baud, timeout=0.2)
                # ESP32 boards reset on DTR/RTS toggle by default; force the lines
                # low at open so opening the port does not reboot the firmware.
                try:
                    self.ser.dtr = False
                    self.ser.rts = False
                except Exception:
                    pass
                try:
                    self.ser.reset_input_buffer()
                except Exception:
                    pass
            except Exception as e:
                self.out_q.put(("err", self.devnode, f"open failed: {e}"))
                return

            while not self.stop_flag.is_set():
                try:
                    line = self.ser.readline()
                    if not line:
                        continue
                    side, v = parse_line(line)
                    if v is not None:
                        self.out_q.put(("val", self.devnode, time.time(), side, v))
                except Exception as e:
                    self.out_q.put(("err", self.devnode, f"read failed: {e}"))
                    break

            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass

    class CamReader(threading.Thread):
        def __init__(
            self,
            dev: str,
            out_q: "queue.Queue",
            label: str,
            mode: Optional[CameraMode] = None,
        ):
            super().__init__(daemon=True)
            self.dev = dev
            self.label = label
            self.out_q = out_q
            self.stop_flag = threading.Event()
            self.cap = None
            self._cap_lock = threading.Lock()
            self._frame_pending = False
            self._frame_pending_lock = threading.Lock()
            self._last_preview_at = 0.0
            self.mode = mode

        def set_mode(self, mode: CameraMode) -> bool:
            """Apply an advertised capture mode to the live preview."""
            with self._cap_lock:
                self.mode = mode
                if self.cap is None or not self.cap.isOpened():
                    return False
                return self._apply_mode_locked(mode)

        def _apply_mode_locked(self, mode: CameraMode) -> bool:
            values = (
                (cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode.fourcc)),
                (cv2.CAP_PROP_FRAME_WIDTH, mode.width),
                (cv2.CAP_PROP_FRAME_HEIGHT, mode.height),
                (cv2.CAP_PROP_FPS, mode.framerate),
            )
            results = [bool(self.cap.set(prop, value)) for prop, value in values]
            return all(results)

        def set_brightness(self, value: float) -> bool:
            """Set the V4L2 brightness control on this already-open camera."""
            with self._cap_lock:
                if self.cap is None or not self.cap.isOpened():
                    return False
                return bool(self.cap.set(cv2.CAP_PROP_BRIGHTNESS, float(value)))

        def stop(self):
            self.stop_flag.set()
            # release() and read() must never overlap. OpenCV 4.5 can otherwise
            # return a freed CvMat and abort in cvarrToMat (Unknown array type).
            with self._cap_lock:
                capture, self.cap = self.cap, None
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        pass

        def frame_consumed(self) -> None:
            with self._frame_pending_lock:
                self._frame_pending = False

        def _queue_preview(self, frame) -> None:
            now = time.monotonic()
            # Tk/Pillow rendering is intentionally capped at 15 FPS. Capture
            # still drains at the selected device rate, while one pending-frame
            # slot prevents an unbounded queue from making Setup progressively
            # less responsive.
            if now - self._last_preview_at < (1.0 / 15.0):
                return
            with self._frame_pending_lock:
                if self._frame_pending:
                    return
                self._frame_pending = True
            self._last_preview_at = now
            preview = cv2.resize(frame, (320, 240), interpolation=cv2.INTER_AREA)
            self.out_q.put(("frame", self.dev, time.time(), preview, self))

        def run(self):
            try:
                idx = int(self.dev.replace("/dev/video", ""))
                capture = cv2.VideoCapture(idx, cv2.CAP_V4L2)
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                with self._cap_lock:
                    self.cap = capture
                if not capture.isOpened():
                    self.out_q.put(("err", self.dev, f"camera open failed: {self.dev}"))
                    self.stop()
                    return
                with self._cap_lock:
                    if self.cap is None:
                        return
                    if self.mode is not None:
                        self._apply_mode_locked(self.mode)
                    brightness = self.cap.get(cv2.CAP_PROP_BRIGHTNESS)
                self.out_q.put(("brightness", self.dev, brightness))
            except Exception as e:
                self.out_q.put(("err", self.dev, f"camera open failed: {e}"))
                self.stop()
                return

            while not self.stop_flag.is_set():
                try:
                    with self._cap_lock:
                        capture = self.cap
                        if capture is None:
                            break
                        ok, frame = capture.read()
                except cv2.error as exc:
                    if self.stop_flag.is_set():
                        break
                    self.out_q.put(("err", self.dev, f"camera read failed: {exc}"))
                    break
                if not ok or frame is None:
                    if self.stop_flag.wait(0.01):
                        break
                    continue
                self._queue_preview(frame)

            self.stop()

    class VrProbeReader(threading.Thread):
        """Verify the complete PICO USB path while the ROS bridge is stopped.

        The native setup owns the cameras/encoders and intentionally stops the
        galaxis container. This temporary listener therefore occupies port 63901,
        launches the APK through ADB reverse, and reports healthy only after
        actual TCP payload bytes arrive. It is closed before APPLY returns so
        the real ROS bridge can bind the same port.
        """

        def __init__(self, serial_number: str, out_q: "queue.Queue", generation: int):
            super().__init__(daemon=True)
            self.serial_number = serial_number
            self.out_q = out_q
            self.generation = generation
            self.stop_flag = threading.Event()
            self.listener: Optional[socket.socket] = None
            self.client: Optional[socket.socket] = None

        def stop(self):
            self.stop_flag.set()
            for sock in (self.client, self.listener):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        def _run_adb(self, *parts: str) -> subprocess.CompletedProcess:
            command = ["adb"]
            if self.serial_number:
                command.extend(["-s", self.serial_number])
            command.extend(parts)
            return subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=8,
                check=False,
            )

        def _status(self, state: str, message: str) -> None:
            self.out_q.put(("vr", self.generation, state, message))

        def _resolve_serial(self) -> bool:
            result = subprocess.run(
                ["adb", "devices"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=8,
                check=False,
            )
            devices = []
            for line in result.stdout.splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 2 and fields[1] == "device":
                    devices.append(fields[0])
            if self.serial_number:
                return self.serial_number in devices
            if len(devices) == 1:
                self.serial_number = devices[0]
                return True
            return False

        def run(self):
            try:
                if not self._resolve_serial():
                    self._status("error", "ADB: 未找到唯一一台已授权 PICO")
                    return
                self._status("adb", f"ADB 已连接: {self.serial_number}")

                # The order deliberately mirrors the known-good manual command:
                # adb start-server && adb wait-for-device && adb get-state &&
                # adb reverse --remove ... && adb reverse ... && adb reverse --list
                start = subprocess.run(
                    ["adb", "start-server"], stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, timeout=8, check=False,
                )
                if start.returncode != 0:
                    self._status("error", f"adb start-server 失败: {start.stdout.strip()}")
                    return
                wait = self._run_adb("wait-for-device")
                state = self._run_adb("get-state")
                if wait.returncode != 0 or state.stdout.strip() != "device":
                    self._status("error", f"ADB 设备不可用: {state.stdout.strip() or wait.stdout.strip()}")
                    return

                self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listener.bind(("0.0.0.0", 63901))
                self.listener.listen(1)
                self.listener.settimeout(0.5)

                self._run_adb("reverse", "--remove", "tcp:63901")
                reverse = self._run_adb("reverse", "tcp:63901", "tcp:63901")
                if reverse.returncode != 0:
                    self._status("error", f"ADB tunnel 失败: {reverse.stdout.strip()}")
                    return
                listed = self._run_adb("reverse", "--list")
                if listed.returncode != 0 or "tcp:63901 tcp:63901" not in listed.stdout:
                    self._status("error", f"ADB tunnel 校验失败: {listed.stdout.strip()}")
                    return
                self._status("tunnel", "ADB tunnel 已建立并校验，正在启动 VR…")

                self._run_adb(
                    "shell", "input", "keyevent", "KEYCODE_WAKEUP"
                )
                self._run_adb("shell", "am", "force-stop", "com.galaxis.vrteleop")
                started = self._run_adb(
                    "shell", "am", "start", "-n", PICO_ACTIVITY
                )
                if started.returncode != 0:
                    self._status("error", f"VR 应用启动失败: {started.stdout.strip()}")
                    return
                self._status("app", "VR 应用已启动，等待实时 6DoF 数据…")
                decoder = GalaxisPacketDecoder()

                while not self.stop_flag.is_set():
                    try:
                        self.client, _address = self.listener.accept()
                        self.client.settimeout(0.5)
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                    while not self.stop_flag.is_set():
                        try:
                            payload = self.client.recv(1024 * 1024)
                        except socket.timeout:
                            continue
                        except OSError:
                            break
                        if not payload:
                            break
                        self._status("streaming", f"VR 正常：已收到实时数据 ({len(payload)} bytes)")
                        for command, packet_payload, _timestamp in decoder.feed(payload):
                            if command != 0x6D:
                                continue
                            try:
                                envelope = json.loads(packet_payload.decode("utf-8"))
                                if envelope.get("functionName") != "Tracking":
                                    continue
                                tracking = envelope.get("value")
                                if isinstance(tracking, str):
                                    tracking = json.loads(tracking)
                                if isinstance(tracking, dict):
                                    self.out_q.put((
                                        "vr_pose", self.generation,
                                        normalize_tracking_payload(tracking),
                                    ))
                            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                                continue
                    try:
                        self.client.close()
                    except OSError:
                        pass
                    self.client = None
                    if not self.stop_flag.is_set():
                        self._status("app", "VR 已断开，等待重新连接…")
            except FileNotFoundError:
                self._status("error", "找不到 adb")
            except subprocess.TimeoutExpired:
                self._status("error", "ADB 命令超时")
            except OSError as exc:
                self._status("error", f"VR 检测端口失败: {exc}")
            finally:
                self.stop()

    class App:
        """Variant-aware LEFT/RIGHT (+CENTER) setup UI.

        Architectural invariant: the role dicts (``cam_role`` and ``esp_role``)
        are the single source of truth for which physical device plays which
        side. All UI widgets are keyed by side name, so swapping reduces to
        swapping values in those dicts.
        """

        def __init__(
            self,
            root: tk.Tk,
            cam_devs: List[str],
            esp_devs: List[EspDev],
            baud: int,
            variant: str,
            collection_profile: str,
        ):
            self.root = root
            self.cam_devs = list(cam_devs)
            self.esp_devs = list(esp_devs)
            self.esp_by_dev = {esp.device: esp for esp in self.esp_devs}
            self.baud = baud
            self.variant = variant
            self.collection_profile = collection_profile
            self.vr_serial = args.vr_serial
            self.vr_ready = True
            self.vr_probe_generation = 0
            self.cam_sides = camera_sides_for_profile(variant, collection_profile)
            self.arm_sides = arm_sides_for_profile(variant, collection_profile)

            self.q: "queue.Queue" = queue.Queue()

            # Default ordering as a starting point. Replaced by try_auto_assign()
            # when both encoders emit labelled lines.
            initial_roles: Dict[str, Optional[str]] = {}
            for i, side in enumerate(self.cam_sides):
                initial_roles[side] = self.cam_devs[i] if i < len(self.cam_devs) else None
            self.cam_role: Dict[str, Optional[str]] = initial_roles
            self.esp_role = {
                side: self.esp_devs[i].device if i < len(self.esp_devs) else None
                for i, side in enumerate(self.arm_sides)
            }

            self.esp_side_detected = {esp.device: None for esp in self.esp_devs}
            self.auto_assigned: bool = False

            self.data = {esp.device: [] for esp in self.esp_devs}
            self.latest = {esp.device: None for esp in self.esp_devs}
            self.calib_min = {esp.device: None for esp in self.esp_devs}

            # Widget refs keyed by side
            self.cam_canvas: Dict[str, ttk.Label] = {}
            self.cam_path_lbl: Dict[str, ttk.Label] = {}
            self.cam_picker: Dict[str, ttk.Combobox] = {}
            self.cam_picker_var: Dict[str, tk.StringVar] = {}
            self.cam_mode_picker: Dict[str, ttk.Combobox] = {}
            self.cam_mode_var: Dict[str, tk.StringVar] = {}
            self.cam_brightness_scale: Dict[str, ttk.Scale] = {}
            self.center_source_picker = None
            self.center_camera_source = load_saved_center_camera_source()
            self.center_source_var = tk.StringVar(
                value=CENTER_CAMERA_SOURCE_LABELS[self.center_camera_source]
            )
            self.camera_modes_by_dev = {
                dev: query_camera_modes(dev) for dev in self.cam_devs
            }
            saved_modes = load_saved_camera_modes()
            self.camera_mode_by_side: Dict[str, CameraMode] = {}
            for side in self.cam_sides:
                dev = self.cam_role.get(side)
                mode = (
                    FORCED_CENTER_MODE
                    if side == "center"
                    else choose_camera_mode(
                        self.camera_modes_by_dev.get(dev, []) if dev else [],
                        saved_modes.get(side),
                    )
                )
                if mode is not None:
                    self.camera_mode_by_side[side] = mode
            self.cam_brightness_var: Dict[str, tk.DoubleVar] = {}
            self.cam_brightness_label: Dict[str, ttk.Label] = {}
            self.cam_brightness_by_dev: Dict[str, float] = {}
            center_dev = self.cam_role.get("center")
            if center_dev:
                self.cam_brightness_by_dev[center_dev] = float(
                    load_center_brightness()
                )
            self.center_vertical_flip = load_center_vertical_flip()
            self.center_flip_button = None
            self.esp_path_lbl: Dict[str, ttk.Label] = {}
            self.val_lbl: Dict[str, ttk.Label] = {}
            self.ax: Dict[str, object] = {}
            self.line: Dict[str, object] = {}
            self.plot_canvas: Dict[str, FigureCanvasTkAgg] = {}
            self.tkimg: Dict[str, object] = {side: None for side in self.cam_sides}

            configure_galaxis_theme(root)
            title = "Galaxis Device Setup"
            title += f" — {collection_profile}"
            root.title(title)
            root.geometry("1180x820")
            root.minsize(1080, 700)
            root.columnconfigure(0, weight=1)
            root.rowconfigure(0, weight=1)

            outer = ttk.Frame(root, padding=16)
            outer.grid(row=0, column=0, sticky="nsew")
            outer.columnconfigure(0, weight=1)
            outer.rowconfigure(2, weight=1)

            header = ttk.Frame(outer, padding=(18, 14), style="Card.TFrame")
            header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            header.columnconfigure(1, weight=1)
            ttk.Label(header, text="GALAXIS", style="Brand.TLabel").grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(0, 14)
            )
            ttk.Label(header, text="设备设置", style="Hero.TLabel").grid(
                row=0, column=1, sticky="sw"
            )
            ttk.Label(
                header,
                text="DEVICE SETUP  ·  相机、执行器与采集参数",
                style="HeroSub.TLabel",
            ).grid(row=1, column=1, sticky="nw")
            ttk.Label(
                header,
                text=f"{collection_profile.upper()}  ·  {len(self.cam_sides)} CAM  ·  {len(self.arm_sides)} ARM",
                style="Profile.TLabel",
            ).grid(row=0, column=2, rowspan=2, sticky="e")

            navigation = ttk.Frame(outer, padding=4, style="Card.TFrame")
            navigation.grid(row=1, column=0, sticky="ew", pady=(0, 10))
            self.nav_buttons: Dict[str, ttk.Button] = {}
            nav_items = (
                ("cameras", "相机  CAMERAS"),
                ("actuators", "执行器  ACTUATORS"),
                ("review", "检查与应用  REVIEW"),
            )
            for column, (key, text) in enumerate(nav_items):
                navigation.columnconfigure(column, weight=1)
                button = ttk.Button(
                    navigation,
                    text=text,
                    style="Nav.TButton",
                    command=lambda page=key: self.show_page(page),
                )
                button.grid(row=0, column=column, sticky="ew", padx=2)
                self.nav_buttons[key] = button

            page_host = ttk.Frame(outer)
            page_host.grid(row=2, column=0, sticky="nsew")
            page_host.columnconfigure(0, weight=1)
            page_host.rowconfigure(0, weight=1)
            self.pages: Dict[str, ttk.Frame] = {
                key: ttk.Frame(page_host) for key, _text in nav_items
            }
            for page in self.pages.values():
                page.grid(row=0, column=0, sticky="nsew")

            camera_page = self.pages["cameras"]
            camera_page.columnconfigure(0, weight=1)
            camera_toolbar = ttk.Frame(
                camera_page, padding=(14, 10), style="Card.TFrame"
            )
            camera_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            camera_toolbar.columnconfigure(0, weight=1)
            ttk.Label(
                camera_toolbar,
                text="视觉采集",
                style="CardTitle.TLabel",
            ).grid(row=0, column=0, sticky="w")
            ttk.Label(
                camera_toolbar,
                text="每台相机独立选择设备、分辨率、帧率和像素格式",
                style="Muted.Card.TLabel",
            ).grid(row=1, column=0, sticky="w", pady=(2, 0))
            if self.arm_sides == ("left", "right"):
                ttk.Button(
                    camera_toolbar, text="⇄  交换左右相机", command=self.swap_cam
                ).grid(row=0, column=1, rowspan=2, sticky="e")

            camera_grid = ttk.Frame(camera_page)
            camera_grid.grid(row=1, column=0, sticky="nsew")
            for column, side in enumerate(self.cam_sides):
                camera_grid.columnconfigure(column, weight=1, uniform="camera")
                self._build_camera_section(camera_grid, side).grid(
                    row=0, column=column, sticky="nsew", padx=4
                )

            actuator_page = self.pages["actuators"]
            actuator_page.columnconfigure(0, weight=1)
            actuator_toolbar = ttk.Frame(
                actuator_page, padding=(14, 10), style="Card.TFrame"
            )
            actuator_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            actuator_toolbar.columnconfigure(0, weight=1)
            ttk.Label(
                actuator_toolbar, text="执行器校准", style="CardTitle.TLabel"
            ).grid(row=0, column=0, sticky="w")
            ttk.Label(
                actuator_toolbar,
                text="闭合夹爪后采集 MIN，实时曲线仅保留最近 5 秒",
                style="Muted.Card.TLabel",
            ).grid(row=1, column=0, sticky="w", pady=(2, 0))
            if self.arm_sides == ("left", "right"):
                ttk.Button(
                    actuator_toolbar, text="⇄  交换左右执行器", command=self.swap_esp
                ).grid(row=0, column=1, rowspan=2, sticky="e")
            actuator_grid = ttk.Frame(actuator_page)
            actuator_grid.grid(row=1, column=0, sticky="nsew")
            for column, side in enumerate(self.arm_sides):
                actuator_grid.columnconfigure(column, weight=1, uniform="actuator")
                self._build_encoder_section(actuator_grid, side).grid(
                    row=0, column=column, sticky="nsew", padx=4
                )

            review_page = self.pages["review"]
            review_page.columnconfigure(0, weight=1)
            review_card = ttk.Frame(review_page, padding=20, style="Card.TFrame")
            review_card.grid(row=0, column=0, sticky="nsew")
            review_card.columnconfigure(0, weight=1)
            ttk.Label(
                review_card, text="应用前检查", style="Hero.TLabel"
            ).grid(row=0, column=0, sticky="w")
            ttk.Label(
                review_card,
                text="确认设备映射、相机采集模式与夹爪最小值，然后进入实时采集监看。",
                style="HeroSub.TLabel",
            ).grid(row=1, column=0, sticky="w", pady=(4, 18))
            self.review_summary = ttk.Label(
                review_card,
                text="",
                style="Status.TLabel",
                justify="left",
                anchor="nw",
            )
            self.review_summary.grid(row=2, column=0, sticky="ew", ipady=12)
            ttk.Button(
                review_card,
                text="↻  重新扫描相机",
                command=self.refresh_camera_ports,
            ).grid(row=3, column=0, sticky="w", pady=(16, 0))

            self.auto_status_lbl = ttk.Label(
                outer,
                text="●  正在等待执行器的 Lxxx / Rxxx 标记，以自动完成设备映射…",
                style="Status.TLabel",
            )
            self.auto_status_lbl.grid(row=3, column=0, sticky="ew", pady=(10, 0))

            bottom = ttk.Frame(outer, padding=10, style="Card.TFrame")
            bottom.grid(row=4, column=0, sticky="ew", pady=(10, 0))
            bottom.columnconfigure(0, weight=1)
            bottom.columnconfigure(1, weight=1)
            bottom.columnconfigure(2, weight=1)
            ttk.Button(bottom, text="上一步", command=self.previous_page).grid(
                row=0, column=0, sticky="ew"
            )
            self.primary_button = ttk.Button(
                bottom,
                text="下一步  →",
                command=self.primary_action,
                style="Primary.TButton",
            )
            self.primary_button.grid(
                row=0, column=1, sticky="ew", padx=(10, 0)
            )
            ttk.Button(bottom, text="取消", command=self.cancel, style="Quiet.TButton").grid(
                row=0, column=2, sticky="ew", padx=(10, 0)
            )

            self.refresh_role_labels()
            self.show_page("cameras")

            # Threads
            self.cam_readers: List[CamReader] = []
            self.cam_readers_by_dev: Dict[str, CamReader] = {}
            self.esp_readers = [EspSerialReader(esp.device, baud, self.q) for esp in self.esp_devs]
            self.vr_reader: Optional[VrProbeReader] = None

            self._start_camera_readers()
            for reader in self.esp_readers:
                reader.start()
            self._stopped = False
            self.root.protocol("WM_DELETE_WINDOW", self.cancel)
            self.root.after(50, self.tick)

        def show_page(self, page: str):
            if page not in self.pages:
                return
            if page == "review":
                self.refresh_role_labels()
            self.current_page = page
            self.pages[page].tkraise()
            for key, button in self.nav_buttons.items():
                button.configure(
                    style="NavActive.TButton" if key == page else "Nav.TButton"
                )
            self.primary_button.configure(
                text=(
                    "保存配置并进入采集监看  →"
                    if page == "review"
                    else "下一步  →"
                )
            )

        def previous_page(self):
            order = ("cameras", "actuators", "review")
            index = order.index(self.current_page)
            self.show_page(order[max(index - 1, 0)])

        def primary_action(self):
            order = ("cameras", "actuators", "review")
            index = order.index(self.current_page)
            if index == len(order) - 1:
                self.apply()
            else:
                self.show_page(order[index + 1])

        def _build_camera_section(self, parent, side: str) -> ttk.Frame:
            titles = {
                "left": "LEFT  ·  左手相机",
                "right": "RIGHT  ·  右手相机",
                "center": "CENTER  ·  头部相机",
            }
            f = ttk.Frame(parent, padding=14, style="Card.TFrame")
            f.columnconfigure(0, weight=1)
            ttk.Label(
                f, text=titles[side], style="CardTitle.TLabel"
            ).grid(row=0, column=0, sticky="w", pady=(0, 2))

            self.cam_path_lbl[side] = ttk.Label(f, text="(unset)", style="Muted.Card.TLabel")
            self.cam_path_lbl[side].grid(row=1, column=0, sticky="w", pady=(0, 6))

            self.cam_canvas[side] = ttk.Label(
                f, text="CAMERA PREVIEW", style="Preview.TLabel"
            )
            self.cam_canvas[side].grid(row=2, column=0, sticky="ew", ipady=4)

            row = 3
            if side == "center":
                ttk.Label(f, text="中心相机来源", style="Muted.Card.TLabel").grid(
                    row=row, column=0, sticky="w", pady=(10, 3)
                )
                row += 1
                self.center_source_picker = ttk.Combobox(
                    f,
                    values=list(CENTER_CAMERA_SOURCE_LABELS.values()),
                    textvariable=self.center_source_var,
                    state="readonly",
                )
                self.center_source_picker.grid(row=row, column=0, sticky="ew")
                self.center_source_picker.bind(
                    "<<ComboboxSelected>>", self.on_center_source_pick
                )
                row += 1

            ttk.Label(f, text="相机设备", style="Muted.Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=(10, 3)
            )
            row += 1

            var = tk.StringVar(value=self.cam_role.get(side) or "")
            self.cam_picker_var[side] = var
            picker = ttk.Combobox(
                f,
                values=list(self.cam_devs),
                textvariable=var,
                state="readonly",
            )
            picker.grid(row=row, column=0, sticky="ew")
            picker.bind(
                "<<ComboboxSelected>>",
                lambda _e, s=side: self.on_cam_pick(s),
            )
            self.cam_picker[side] = picker
            row += 1

            ttk.Label(
                f, text="采集模式  ·  分辨率 / 帧率 / 像素格式", style="Muted.Card.TLabel"
            ).grid(row=row, column=0, sticky="w", pady=(10, 3))
            row += 1
            mode_var = tk.StringVar(value="")
            self.cam_mode_var[side] = mode_var
            mode_picker = ttk.Combobox(
                f,
                values=[],
                textvariable=mode_var,
                state="readonly",
            )
            mode_picker.grid(row=row, column=0, sticky="ew")
            mode_picker.bind(
                "<<ComboboxSelected>>",
                lambda _e, s=side: self.on_camera_mode_pick(s),
            )
            self.cam_mode_picker[side] = mode_picker
            row += 1

            ttk.Label(f, text="图像亮度", style="Muted.Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=(10, 2)
            )
            row += 1
            brightness_var = tk.DoubleVar(value=50.0)
            self.cam_brightness_var[side] = brightness_var
            brightness_scale = ttk.Scale(
                f,
                # Observed V4L2 range on the supported UVC cameras.  Do not
                # offer values the hardware cannot accept.
                from_=-64,
                to=64,
                variable=brightness_var,
                command=lambda value, s=side: self.on_camera_brightness(s, value),
            )
            brightness_scale.grid(row=row, column=0, sticky="ew")
            self.cam_brightness_scale[side] = brightness_scale
            row += 1
            self.cam_brightness_label[side] = ttk.Label(
                f, text="Brightness: 50", style="Muted.Card.TLabel"
            )
            self.cam_brightness_label[side].grid(row=row, column=0, sticky="w")
            row += 1
            if side == "center":
                self.center_flip_button = ttk.Button(
                    f,
                    command=self.toggle_center_vertical_flip,
                )
                self.center_flip_button.grid(row=row, column=0, sticky="ew", pady=(8, 0))
                self.refresh_center_flip_button()
            return f

        def _build_encoder_section(self, parent, side: str) -> ttk.Frame:
            title = "LEFT  ·  左夹爪编码器" if side == "left" else "RIGHT  ·  右夹爪编码器"
            f = ttk.Frame(parent, padding=14, style="Card.TFrame")
            f.columnconfigure(0, weight=1)
            ttk.Label(f, text=title, style="CardTitle.TLabel").grid(
                row=0, column=0, sticky="w", pady=(0, 2)
            )

            self.esp_path_lbl[side] = ttk.Label(f, text="(unset)", style="Muted.Card.TLabel")
            self.esp_path_lbl[side].grid(row=1, column=0, sticky="w")

            fig = Figure(figsize=(4, 2.2), dpi=100, facecolor=palette["terminal"])
            ax = fig.add_subplot(111)
            ax.set_facecolor(palette["terminal"])
            ax.set_title("LIVE SIGNAL  ·  LAST 5 SECONDS", color=palette["terminal_text"], fontsize=9)
            ax.set_xlabel("t (s)")
            ax.set_ylabel("value")
            ax.tick_params(colors=palette["terminal_text"], labelsize=8)
            ax.xaxis.label.set_color(palette["terminal_text"])
            ax.yaxis.label.set_color(palette["terminal_text"])
            for spine in ax.spines.values():
                spine.set_color(palette["muted"])
            (line,) = ax.plot([], [], color=palette["blue_bright"], linewidth=1.8)
            canvas = FigureCanvasTkAgg(fig, master=f)
            canvas_widget = canvas.get_tk_widget()
            canvas_widget.configure(
                background=palette["terminal"], highlightthickness=0, borderwidth=0
            )
            canvas_widget.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
            self.ax[side] = ax
            self.line[side] = line
            self.plot_canvas[side] = canvas

            self.val_lbl[side] = ttk.Label(f, text="value: -    min: -", style="Muted.Card.TLabel")
            self.val_lbl[side].grid(row=3, column=0, sticky="w", pady=(6, 0))
            ttk.Button(f, text="◎  采集闭合最小值", command=lambda s=side: self.cap_min_side(s)).grid(
                row=4, column=0, sticky="ew", pady=(8, 0)
            )
            return f

        def refresh_role_labels(self):
            for side in self.cam_sides:
                cam = self.cam_role.get(side)
                if side == "center" and self.center_camera_source == G2_CENTER_SOURCE:
                    self.cam_path_lbl[side].config(
                        text="G2 · HEAD COLOR · 通过本机 GDK 连接"
                    )
                else:
                    self.cam_path_lbl[side].config(text=cam or "(unset)")
                if side in self.cam_picker_var:
                    self.cam_picker_var[side].set(cam or "")
                self.refresh_camera_modes(side)
                self.refresh_camera_brightness(side)
            self.refresh_center_source_controls()
            for side in self.arm_sides:
                esp = self.esp_role.get(side)
                self.esp_path_lbl[side].config(text=esp or "(unset)")
            camera_lines = []
            for side in self.cam_sides:
                mode = self.camera_mode_by_side.get(side)
                device = self.cam_role.get(side) or "未设置"
                if side == "center" and self.center_camera_source == G2_CENTER_SOURCE:
                    device = "G2 头部彩色相机（GDK）"
                camera_lines.append(
                    f"{side.upper():>6}  {device}  ·  "
                    f"{mode.label if mode else '未选择采集模式'}"
                )
            encoder_lines = [
                f"{side.upper():>6}  {self.esp_role.get(side) or '未设置'}  ·  "
                f"MIN {self.calib_min.get(self.esp_role.get(side)) if self.esp_role.get(side) else '未采集'}"
                for side in self.arm_sides
            ]
            self.review_summary.configure(
                text="\n".join(("相机", *camera_lines, "", "执行器", *encoder_lines))
            )

        def _start_camera_readers(self):
            mode_by_dev = {
                self.cam_role[side]: mode
                for side, mode in self.camera_mode_by_side.items()
                if self.cam_role.get(side)
            }
            self.cam_readers = [
                CamReader(
                    dev,
                    self.q,
                    chr(ord("A") + i),
                    mode=mode_by_dev.get(dev),
                )
                for i, dev in enumerate(self.cam_devs)
            ]
            self.cam_readers_by_dev = {reader.dev: reader for reader in self.cam_readers}
            for reader in self.cam_readers:
                reader.start()

        def refresh_camera_ports(self):
            """Rescan hot-plugged cameras and restart only preview readers."""
            discovered = find_camera_candidates()
            for reader in self.cam_readers:
                reader.stop()
            for reader in self.cam_readers:
                reader.join(timeout=2.0)

            self.cam_devs = discovered
            self.camera_modes_by_dev = {
                dev: query_camera_modes(dev) for dev in discovered
            }
            available = set(discovered)
            for side in self.cam_sides:
                if self.cam_role.get(side) not in available:
                    self.cam_role[side] = None
                    self.cam_canvas[side].configure(image="")
                    self.tkimg[side] = None
                self.cam_picker[side].configure(values=discovered)

            self._start_camera_readers()
            self.refresh_role_labels()
            target = expected_camera_count(self.variant, self.collection_profile)
            self.auto_status_lbl.config(
                text=(
                    f"Refreshed camera ports: found {len(discovered)} candidate(s) "
                    f"(target {target}). Select the desired ports before APPLY."
                ),
                foreground=palette["green"] if len(discovered) >= target else palette["orange"],
            )

        def swap_cam(self):
            if self.arm_sides != ("left", "right"):
                return
            self.cam_role["left"], self.cam_role["right"] = (
                self.cam_role["right"],
                self.cam_role["left"],
            )
            self.refresh_role_labels()

        def swap_esp(self):
            if self.arm_sides != ("left", "right"):
                return
            self.esp_role["left"], self.esp_role["right"] = (
                self.esp_role["right"],
                self.esp_role["left"],
            )
            self.refresh_role_labels()

        def on_cam_pick(self, side: str):
            new_dev = self.cam_picker_var[side].get()
            if not new_dev:
                return
            new_roles = resolve_camera_pick(
                self.cam_role, side, new_dev, self.cam_sides
            )
            self.cam_role = new_roles
            self.refresh_role_labels()

        def refresh_camera_modes(self, side: str):
            if side == "center":
                self.camera_mode_by_side[side] = FORCED_CENTER_MODE
                self.cam_mode_picker[side].configure(
                    values=[FORCED_CENTER_MODE.label], state="disabled"
                )
                self.cam_mode_var[side].set(FORCED_CENTER_MODE.label)
                return
            dev = self.cam_role.get(side)
            modes = self.camera_modes_by_dev.get(dev, []) if dev else []
            current = self.camera_mode_by_side.get(side)
            if current not in modes:
                current = choose_camera_mode(modes)
                if current is None:
                    self.camera_mode_by_side.pop(side, None)
                else:
                    self.camera_mode_by_side[side] = current
            self.cam_mode_picker[side].configure(
                values=[mode.label for mode in modes], state="readonly"
            )
            self.cam_mode_var[side].set(current.label if current is not None else "无可用采集模式")

        def on_camera_mode_pick(self, side: str):
            dev = self.cam_role.get(side)
            label = self.cam_mode_var[side].get()
            mode = next(
                (
                    candidate
                    for candidate in self.camera_modes_by_dev.get(dev, [])
                    if candidate.label == label
                ),
                None,
            )
            if mode is None:
                return
            self.camera_mode_by_side[side] = mode
            reader = self.cam_readers_by_dev.get(dev) if dev else None
            if reader is not None and not reader.set_mode(mode):
                self.auto_status_lbl.config(
                    text=f"{side.upper()} 预览无法立即切换到 {mode.label}；保存后采集服务会重新打开设备。",
                    foreground=palette["orange"],
                )

        def refresh_camera_brightness(self, side: str):
            if side == "center" and self.center_camera_source == G2_CENTER_SOURCE:
                self.cam_brightness_label[side].config(
                    text="Brightness: 由 G2 相机管理"
                )
                return
            dev = self.cam_role.get(side)
            value = self.cam_brightness_by_dev.get(dev) if dev else None
            if value is None:
                return
            self.cam_brightness_var[side].set(value)
            self.cam_brightness_label[side].config(text=f"Brightness: {value:.0f}")

        def on_camera_brightness(self, side: str, raw_value: str):
            if side == "center" and self.center_camera_source == G2_CENTER_SOURCE:
                return
            value = float(raw_value)
            self.cam_brightness_label[side].config(text=f"Brightness: {value:.0f}")
            dev = self.cam_role.get(side)
            reader = self.cam_readers_by_dev.get(dev) if dev else None
            if reader is None:
                return
            if not reader.set_brightness(value):
                self.cam_brightness_label[side].config(
                    text="Brightness: unsupported by this camera"
                )
                return
            self.cam_brightness_by_dev[dev] = value

        def on_center_source_pick(self, _event=None):
            label = self.center_source_var.get()
            source = next(
                (
                    key
                    for key, candidate_label in CENTER_CAMERA_SOURCE_LABELS.items()
                    if candidate_label == label
                ),
                "usb",
            )
            self.center_camera_source = source
            if source == G2_CENTER_SOURCE:
                self.camera_mode_by_side["center"] = FORCED_CENTER_MODE
                self.cam_canvas["center"].configure(
                    image="", text="G2 HEAD COLOR\n输出 640 × 480 · 30 FPS\n采集启动时连接"
                )
                self.tkimg["center"] = None
            else:
                self.camera_mode_by_side["center"] = FORCED_CENTER_MODE
                self.cam_canvas["center"].configure(
                    image="", text="CAMERA PREVIEW"
                )
                self.tkimg["center"] = None
            self.refresh_role_labels()

        def refresh_center_source_controls(self):
            if "center" not in self.cam_sides or self.center_source_picker is None:
                return
            self.center_source_var.set(
                CENTER_CAMERA_SOURCE_LABELS[self.center_camera_source]
            )
            use_g2 = self.center_camera_source == G2_CENTER_SOURCE
            self.cam_picker["center"].configure(
                state="disabled" if use_g2 else "readonly"
            )
            self.cam_brightness_scale["center"].configure(
                state="disabled" if use_g2 else "normal"
            )
            if use_g2:
                self.cam_canvas["center"].configure(
                    image="", text="G2 HEAD COLOR\n输出 640 × 480 · 30 FPS\n采集启动时连接"
                )

        def refresh_center_flip_button(self):
            if self.center_flip_button is not None:
                state = "开" if self.center_vertical_flip else "关"
                self.center_flip_button.config(text=f"上下镜像：{state}")

        def toggle_center_vertical_flip(self):
            self.center_vertical_flip = not self.center_vertical_flip
            self.refresh_center_flip_button()

        def try_auto_assign(self):
            """Bootstrap L/R/center using detected encoder labels + USB-hub pairing.

            Runs at most once (guarded by ``self.auto_assigned``); after that
            the user owns the assignment via Swap or pulldown.
            """
            if self.auto_assigned:
                return

            if len(self.arm_sides) == 1:
                side = self.arm_sides[0]
                matching = [
                    esp for esp in self.esp_devs
                    if self.esp_side_detected.get(esp.device) == side
                ]
                if not matching:
                    return
                encoder = matching[0]
                self.esp_role[side] = encoder.device
                target = expected_camera_count(self.variant, self.collection_profile)
                if len(self.cam_devs) < target:
                    self.auto_status_lbl.config(
                        text=f"Detected {side.upper()} encoder; found {len(self.cam_devs)}/{target} cameras.",
                        foreground=palette["orange"],
                    )
                    return
                assignment = assign_single_arm_camera_roles(
                    [(dev, usb_hub_of(video_dev_usb_location(dev))) for dev in self.cam_devs],
                    usb_hub_of(encoder.location),
                    side,
                )
                self.cam_role[side] = assignment[side]
                self.cam_role["center"] = assignment["center"]
                self.auto_assigned = True
                self.refresh_role_labels()
                self.auto_status_lbl.config(
                    text=(
                        f"Auto-assigned {side.upper()} hand + CENTER cameras"
                        + (" by USB hub" if assignment["matched_arm"] else "; verify camera pulldowns")
                    ),
                    foreground=palette["green"] if assignment["matched_arm"] else palette["orange"],
                )
                return

            side_a = self.esp_side_detected.get(self.esp_devs[0].device)
            side_b = self.esp_side_detected.get(self.esp_devs[1].device)

            if not side_a or not side_b:
                if side_a or side_b:
                    self.auto_status_lbl.config(
                        text=f"Auto-assignment: detected A={side_a or '?'}, B={side_b or '?'}; "
                             "waiting for the other encoder...",
                        foreground=palette["muted"],
                    )
                return

            if side_a == side_b:
                self.auto_assigned = True
                self.auto_status_lbl.config(
                    text=f"Auto-assignment FAILED: conflicting labels (A={side_a}, B={side_b})",
                    foreground=palette["red"],
                )
                messagebox.showerror(
                    "Encoder side conflict",
                    f"Both ESP devices reported the same side ({side_a.upper()}):\n\n"
                    f"  A ({self.esp_devs[0].device}): {side_a}\n"
                    f"  B ({self.esp_devs[1].device}): {side_b}\n\n"
                    "Check the encoder firmware / wiring and restart this tool.",
                )
                return

            if side_a == "left":
                left_esp, right_esp = self.esp_devs[0], self.esp_devs[1]
            else:
                left_esp, right_esp = self.esp_devs[1], self.esp_devs[0]

            self.esp_role["left"] = left_esp.device
            self.esp_role["right"] = right_esp.device

            expected_cameras = expected_camera_count(self.variant, self.collection_profile)
            if len(self.cam_devs) < expected_cameras:
                self.auto_assigned = True
                self.refresh_role_labels()
                self.auto_status_lbl.config(
                    text=(
                        f"Detected encoder labels: A={side_a}, B={side_b}. "
                        f"Found {len(self.cam_devs)}/{expected_cameras} camera candidates; "
                        "connect/select the remaining cameras before APPLY."
                    ),
                    foreground=palette["orange"],
                )
                return

            cam_devs_with_hub: List[Tuple[str, Optional[str]]] = [
                (d, usb_hub_of(video_dev_usb_location(d))) for d in self.cam_devs
            ]
            assignment = assign_camera_roles(
                cam_devs_with_hub,
                usb_hub_of(left_esp.location),
                usb_hub_of(right_esp.location),
                self.variant,
            )

            self.cam_role["left"] = assignment["left"]
            self.cam_role["right"] = assignment["right"]
            if "center" in self.cam_sides:
                self.cam_role["center"] = assignment["center"]

            if assignment["matched_left"] and assignment["matched_right"]:
                hub_msg = " (cameras paired by USB hub)"
            else:
                hub_msg = " (could not match all cameras by USB hub; verify and use pulldown if needed)"

            self.auto_assigned = True
            self.refresh_role_labels()
            self.auto_status_lbl.config(
                text=f"Auto-assigned from encoder labels: A={side_a}, B={side_b}{hub_msg}",
                foreground=palette["green"] if "could not" not in hub_msg else palette["orange"],
            )

        def cap_min_side(self, side: str):
            dev = self.esp_role[side]
            if not dev:
                messagebox.showerror("No device", f"No ESP assigned to {side.upper()}")
                return
            v = self.latest.get(dev)
            if v is None:
                messagebox.showerror("No data", f"No data yet from {side.upper()} ESP ({dev})")
                return
            self.calib_min[dev] = float(v)
            self.refresh_role_labels()

        def refresh_vr_probe(self):
            """Restart the temporary setup listener and discard stale results."""
            if self.variant != "portable":
                return
            if self.vr_reader is not None:
                self.vr_reader.stop()
            self.vr_probe_generation += 1
            self.vr_ready = False
            self.vr_status_lbl.config(
                text="正在重新检查 ADB、USB tunnel、VR 应用和实时数据流…",
                foreground=palette["orange"],
            )
            self.vr_pose_lbl.config(text="6DoF: 等待 PICO Tracking 数据…", foreground=palette["muted"])
            self.vr_reader = VrProbeReader(
                self.vr_serial, self.q, self.vr_probe_generation,
            )
            self.vr_reader.start()

        @staticmethod
        def _format_pose(label: str, raw: Dict[str, Any], prefix: str) -> str:
            position = raw.get(f"{prefix}Position")
            rotation = raw.get(f"{prefix}Rotation")
            if not isinstance(position, dict) or not isinstance(rotation, dict):
                return f"{label}: 暂无追踪"
            try:
                pos = ", ".join(f"{float(position[k]):.3f}" for k in ("x", "y", "z"))
                rot = ", ".join(f"{float(rotation[k]):.3f}" for k in ("x", "y", "z", "w"))
            except (KeyError, TypeError, ValueError):
                return f"{label}: 数据格式无效"
            return f"{label}: 位置 ({pos})，四元数 ({rot})"

        def update_vr_pose(self, raw: Dict[str, Any]):
            left_track = "已追踪" if raw.get("leftTracked") else "未追踪"
            right_track = "已追踪" if raw.get("rightTracked") else "未追踪"
            text = "\n".join((
                self._format_pose("头显 HMD", raw, "hmd"),
                self._format_pose(f"左手柄 [{left_track}]", raw, "leftController"),
                self._format_pose(f"右手柄 [{right_track}]", raw, "rightController"),
            ))
            self.vr_pose_lbl.config(text=text, foreground=palette["green"])

        def tick(self):
            now = time.time()
            while True:
                try:
                    typ, dev, *rest = self.q.get_nowait()
                except queue.Empty:
                    break

                if typ == "vr":
                    generation, state, message = dev, rest[0], rest[1]
                    if generation != self.vr_probe_generation:
                        continue
                    self.vr_ready = state == "streaming"
                    color = (
                        palette["green"]
                        if state == "streaming"
                        else palette["red"] if state == "error" else palette["orange"]
                    )
                    self.vr_status_lbl.config(text=message, foreground=color)
                elif typ == "vr_pose":
                    generation, raw = dev, rest[0]
                    if generation == self.vr_probe_generation:
                        self.update_vr_pose(raw)
                elif typ == "frame":
                    _, frame, reader = rest
                    try:
                        self.update_cam_frame(dev, frame)
                    finally:
                        reader.frame_consumed()
                elif typ == "brightness":
                    self.cam_brightness_by_dev[dev] = float(rest[0])
                    for side in self.cam_sides:
                        if self.cam_role.get(side) == dev:
                            self.refresh_camera_brightness(side)
                elif typ == "val":
                    t, side, v = rest
                    self.data[dev].append((t, v))
                    self.latest[dev] = v
                    if side and not self.esp_side_detected.get(dev):
                        self.esp_side_detected[dev] = side
                        self.try_auto_assign()
                else:
                    err = rest[0] if rest else "unknown"
                    messagebox.showerror("Device error", f"{dev}:\n{err}")

            for dev in self.esp_by_dev:
                self.data[dev] = [(t, v) for (t, v) in self.data[dev] if now - t <= 5.0]

            for side in self.arm_sides:
                dev = self.esp_role[side]
                data = self.data.get(dev, []) if dev else []
                x = [t - now for (t, _) in data]
                y = [v for (_, v) in data]
                self.line[side].set_data(x, y)
                self.ax[side].relim()
                self.ax[side].autoscale_view()
                self.plot_canvas[side].draw_idle()

                latest = self.latest.get(dev) if dev else None
                mi = self.calib_min.get(dev) if dev else None
                self.val_lbl[side].config(
                    text=f"value: {latest if latest is not None else '-'}    "
                         f"min: {mi if mi is not None else '-'}"
                )

            self.root.after(50, self.tick)

        def update_cam_frame(self, dev: str, frame_bgr):
            for side in self.cam_sides:
                if side == "center" and self.center_camera_source == G2_CENTER_SOURCE:
                    continue
                if dev == self.cam_role.get(side):
                    display_frame = frame_bgr
                    if side == "center" and self.center_vertical_flip:
                        display_frame = cv2.flip(frame_bgr, 0)
                    rgb = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(rgb)
                    tkimg = ImageTk.PhotoImage(img)
                    self.tkimg[side] = tkimg
                    self.cam_canvas[side].configure(image=tkimg)
                    return

        def apply(self):
            # Cameras
            for side in self.cam_sides:
                if side == "center" and self.center_camera_source == G2_CENTER_SOURCE:
                    continue
                if not self.cam_role.get(side):
                    messagebox.showerror("Missing", f"Please set Camera {side.upper()}.")
                    return
                if side not in self.camera_mode_by_side:
                    messagebox.showerror(
                        "Missing",
                        f"Camera {side.upper()} 没有可保存的分辨率/帧率/格式。",
                    )
                    return
            cam_devs_assigned = [
                self.cam_role[s]
                for s in self.cam_sides
                if s != "center" or self.center_camera_source != G2_CENTER_SOURCE
            ]
            if len(set(cam_devs_assigned)) != len(cam_devs_assigned):
                messagebox.showerror(
                    "Invalid", "Camera roles must be assigned to distinct devices."
                )
                return

            # Encoders
            if any(not self.esp_role.get(side) for side in self.arm_sides):
                messagebox.showerror("Missing", "Please set every active ESP encoder.")
                return
            if len(set(self.esp_role[side] for side in self.arm_sides)) != len(self.arm_sides):
                messagebox.showerror("Invalid", "ESP LEFT and RIGHT cannot be the same.")
                return

            for side in self.arm_sides:
                devnode = self.esp_role[side]
                mi = self.calib_min.get(devnode)
                if mi is None:
                    messagebox.showerror("Missing", f"Please capture MIN for ESP {side} ({devnode}).")
                    return

            # Portable-only: gripper L/R labels must have been observed (so we know
            # the encoder side mapping is correct), and re-check distinct devs +
            # center set via the shared helper.
            if self.variant == "portable" and self.collection_profile == "dual":
                roles_for_validation = dict(self.cam_role)
                if self.center_camera_source == G2_CENTER_SOURCE:
                    roles_for_validation["center"] = "g2://head_color"
                err = can_apply_portable(self.esp_side_detected, roles_for_validation)
                if err:
                    messagebox.showerror("Not ready", err)
                    return
            for side in self.arm_sides:
                dev = self.esp_role[side]
                detected_side = self.esp_side_detected.get(dev)
                if self.variant == "portable" and detected_side != side:
                    messagebox.showerror(
                        "Encoder side mismatch",
                        f"Selected {side.upper()} profile but {dev} reports {detected_side or 'no L/R label'}.",
                    )
                    return
            if any(
                not (
                    self.esp_by_dev[self.esp_role[side]].serial
                    or self.esp_by_dev[self.esp_role[side]].location
                )
                for side in self.arm_sides
            ):
                messagebox.showerror(
                    "Missing stable identity",
                    "ESP USB serial_number and physical location are both missing; "
                    "cannot create stable udev rules.",
                )
                return

            out = build_apply_payload(
                cam_role=self.cam_role,
                esp_role=self.esp_role,
                esp_serials={
                    esp.device: esp.serial for esp in self.esp_devs
                },
                calib_min=self.calib_min,
                variant=self.variant,
                center_vertical_flip=self.center_vertical_flip,
                collection_profile=self.collection_profile,
                camera_brightness_by_dev=self.cam_brightness_by_dev,
                camera_mode_by_side=self.camera_mode_by_side,
                center_camera_source=self.center_camera_source,
            )

            self.stop_all()
            print(json.dumps(out))
            self.root.destroy()

        def cancel(self):
            self.stop_all()
            self.root.destroy()

        def stop_all(self):
            if self._stopped:
                return
            self._stopped = True
            readers = [*self.cam_readers, *self.esp_readers]
            if self.vr_reader is not None:
                readers.append(self.vr_reader)
            for reader in readers:
                try:
                    reader.stop()
                except Exception:
                    pass
            for reader in readers:
                try:
                    reader.join(timeout=2.0)
                except RuntimeError:
                    # A reader that failed before start has nothing to join.
                    pass

    # ---- entry point inside _run_gui ----
    cam_devs = list(args.camera)
    # Keep the original arguments for direct/manual invocations and old tests.
    if not cam_devs:
        cam_devs = [d for d in (args.cam1, args.cam2, args.cam3) if d]

    # Candidate count is deliberately not a launch gate.  The UI remains
    # useful for inspecting a partial hardware setup; APPLY validates roles.
    if args.variant not in SUPPORTED_VARIANTS:
        print(f"ERROR: unsupported variant {args.variant!r}", flush=True)
        raise SystemExit(2)

    collection_profile = choose_collection_profile()
    arm_sides = arm_sides_for_profile(args.variant, collection_profile)
    probe_diagnostics: Dict[str, str] = {}
    if args.esp_auto:
        esp_devs, probe_diagnostics = auto_find_esp_devices(args.baud)
        discovery_mode = "protocol auto-probe"
    else:
        esp_devs = find_esp_devices(args.esp_vid, args.esp_pid)
        discovery_mode = f"fixed VID:PID {args.esp_vid}:{args.esp_pid}"
    required_esp_count = len(arm_sides)
    count_ok = (
        len(esp_devs) == 2 if required_esp_count == 2 else len(esp_devs) in (1, 2)
    )
    if not count_ok:
        msg = (
            f"Profile {collection_profile} needs {required_esp_count} active ESP32 device(s) "
            f"(one or two may be connected for a single-arm profile).\n"
            f"Discovery mode: {discovery_mode}; accepted {len(esp_devs)} device(s).\n"
        )
        for d in esp_devs:
            msg += f"  - {d.device} serial={d.serial} location={d.location}\n"
        if args.esp_auto:
            if probe_diagnostics:
                msg += "Serial probe details:\n"
                for device in sorted(probe_diagnostics):
                    msg += f"  - {device}: {probe_diagnostics[device]}\n"
            else:
                msg += "No USB serial ports with a readable VID/PID were found.\n"
        print(f"ERROR: {msg}", file=sys.stderr, flush=True)
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("ESP32 device count error", msg)
        raise SystemExit(3)

    root = tk.Tk()
    App(root, cam_devs, esp_devs, args.baud, args.variant, collection_profile)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam-vid", required=True)
    ap.add_argument("--cam-pid", required=True)
    ap.add_argument(
        "--camera", action="append", default=[],
        help="Candidate V4L2 node; repeat once for every video port to show.",
    )
    ap.add_argument(
        "--collection-profile",
        choices=["auto", *SUPPORTED_COLLECTION_PROFILES],
        default="auto",
        help="Portable collection profile; auto opens the setup choice buttons.",
    )
    ap.add_argument("--cam1", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--cam2", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--cam3", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--esp-vid", required=True)
    ap.add_argument("--esp-pid", required=True)
    ap.add_argument(
        "--esp-auto", action="store_true",
        help="Probe all USB serial ports and accept the Galaxis L/R data protocol.",
    )
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument(
        "--vr-serial",
        default="",
        help="PICO ADB serial; auto-detected when exactly one device is connected",
    )
    ap.add_argument(
        "--variant",
        choices=list(SUPPORTED_VARIANTS),
        default="stationary",
        help="Robot variant (default: stationary). Portable targets 3 cameras at APPLY.",
    )
    args = ap.parse_args()
    _run_gui(args)


if __name__ == "__main__":
    main()
