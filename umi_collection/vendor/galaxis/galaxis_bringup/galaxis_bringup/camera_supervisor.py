"""Restart only the affected USB driver after a hotplug or an explicit role change."""
import argparse
import json
import math
import re
from pathlib import Path
import signal
import subprocess
import threading
import time

from .camera_devices import discover, read_selection, resolve_roles

SELECTION = Path('/opt/camera-selection/roles.json')


def enforce_fixed_frame_rate(device):
    """Do not let auto-exposure silently lower the requested capture rate."""
    for control in ('exposure_dynamic_framerate','exposure_auto_priority'):
        try:
            result = subprocess.run(['v4l2-ctl','-d',device,'--set-ctrl',control+'=0',
                                     '--get-ctrl',control],
                capture_output=True,text=True,timeout=2,check=False)
        except (OSError,subprocess.TimeoutExpired):
            return False
        if result.returncode == 0 and re.search(r'\b'+control+r'\s*:\s*0\b',result.stdout):
            return True
    return False


def performance_arguments(role, item, path=Path('/opt/camera-selection/performance.json'),
                          profile_path=Path('/etc/galaxis/collection_profile.yaml')):
    """Use a measured mode only for its original camera identity."""
    try:
        settings = json.loads(path.read_text()).get(role, {})
        wanted = settings.get('identity', {})
        if not wanted or any(item.get(k, '') != wanted.get(k, '') for k in ('vendor','product','serial')):
            return []
        width, height = int(settings['width']), int(settings['height'])
        fps = float(settings['fps'])
        if profile_path.exists():
            import yaml
            profile = yaml.safe_load(profile_path.read_text()) or {}
            mode = profile.get('cameras',{}).get(role,{})
            if (width,height) != (mode.get('image_width'),mode.get('image_height')):
                return []
            if role == 'center' and profile.get('camera_sources',{}).get('center','usb') != 'usb':
                return []
        if width <= 0 or height <= 0 or not math.isfinite(fps) or not 1 <= fps <= 400:
            return []
        result = subprocess.run(['v4l2-ctl','-d',item['device'],'--list-formats-ext'],
            capture_output=True,text=True,timeout=2,check=False)
        if result.returncode:
            return []
        mjpeg = False; size = None; supported = False
        for line in result.stdout.splitlines():
            if re.search(r"\[\d+\]:",line):
                mjpeg = "'MJPG'" in line;size = None
            m = re.search(r'Size: Discrete (\d+)x(\d+)',line)
            if m:size = tuple(map(int,m.groups()))
            m = re.search(r'\(([\d.]+) fps\)',line)
            if mjpeg and size == (width,height) and m and abs(float(m[1])-fps)<.2:
                supported = True
        if not supported:
            print(f"{role}: requested fast mode unsupported; using existing camera defaults",flush=True)
            return []
        return ['-p',f'image_width:={width}','-p',f'image_height:={height}','-p',f'framerate:={fps}']
    except (OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired):
        return []


def selected_cameras(candidates):
    return resolve_roles(read_selection(SELECTION), candidates)


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def main():
    from ament_index_python.packages import get_package_prefix
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--camera-role', required=True, choices=('left','right','center'))
    options, ros_args = parser.parse_known_args()
    executable = str(Path(get_package_prefix('usb_cam'))/'lib/usb_cam/usb_cam_node_exe')
    shutdown = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: shutdown.set())
    child = None
    current = None
    retry_at = 0.0
    try:
        while not shutdown.is_set():
            candidates = discover()
            item = selected_cameras(candidates).get(options.camera_role)
            # Support an existing non-App setup as well; never cache realpath.
            if not SELECTION.exists():
                device = str(Path(f'/dev/galaxis_{options.camera_role}_camera').resolve())
                item = next((c for c in candidates if c['device'] == device), None)
            token = (item['device'], item['generation']) if item else None
            if child is not None and (child.poll() is not None or token != current):
                stop_child(child)
                child = None
                retry_at = time.monotonic()+1.0
            if item and child is None and time.monotonic() >= retry_at:
                # A fresh driver process resolves the current device each time.
                # USB camera libraries may otherwise pin an obsolete videoN.
                if enforce_fixed_frame_rate(item['device']):
                    print(f"{options.camera_role}: auto exposure keeps the requested frame rate",flush=True)
                child = subprocess.Popen([executable, *ros_args, '--ros-args', '-p',
                                          'video_device:='+item['device'], '-p',
                                          'mjpeg_passthrough:=true', *performance_arguments(options.camera_role, item)])
                current = token
                print(f"{options.camera_role} camera connected: {item['device']}", flush=True)
            shutdown.wait(1.0)
    finally:
        stop_child(child)
