"""Restart only the affected USB driver after a hotplug or an explicit role change."""
import argparse
from pathlib import Path
import signal
import subprocess
import threading
import time

from .camera_devices import discover, read_selection, resolve_roles

SELECTION = Path('/opt/camera-selection/roles.json')


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
                child = subprocess.Popen([executable, *ros_args, '--ros-args', '-p',
                                          'video_device:='+item['device']])
                current = token
                print(f"{options.camera_role} camera connected: {item['device']}", flush=True)
            shutdown.wait(1.0)
    finally:
        stop_child(child)
