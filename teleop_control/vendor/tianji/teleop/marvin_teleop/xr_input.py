"""Read the existing XRoboToolkit bridge in an isolated process, as in the reference runtime."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from dataclasses import replace
from .protocol import parse_frame, GRIP_OFF, GripFilter


class SDKInput:
    def __init__(self, timeout):
        if importlib.util.find_spec("xrobotoolkit_sdk") is None:
            raise RuntimeError(
                "xrobotoolkit_sdk missing in this Python; use ./start.sh or TELEOP_PYTHON with the existing XR SDK environment"
            )
        self.timeout = timeout
        self.lock = threading.Lock()
        self.frame = None
        self.error = "waiting for advancing XRoboToolkit timestamp"
        self.stop_latched = False
        self.connected = False
        self.session = 1
        self.epoch = 0
        self.release_epochs = {"left": 0, "right": 0}
        self.last_stamp = None
        self.baseline = None
        self.closed = False
        env = os.environ.copy()
        root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "marvin_teleop.xr_worker"],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def accept(self, data, now):
        """Only source time advancement renews the input lease; cached reads do not."""
        stamp = data["timeStampNs"]
        captured = data.get("captured_monotonic_s", now)
        if not isinstance(captured, (float, int)) or not 0 <= now - captured:
            raise ValueError("invalid XR capture time")
        with self.lock:
            if stamp == self.last_stamp:
                return
        frame = parse_frame(data, self.session, captured)
        if now - captured > self.timeout:
            with self.lock:
                self.stop_latched |= frame.stop
                self.error = "XR IPC frame expired; discard backlog"
            return
        with self.lock:
            if self.last_stamp is None:
                self.last_stamp = stamp
                self.grip_filter = GripFilter()
                return  # Do not accept a cached sample left over before startup.
            if stamp == self.last_stamp:
                return
            if stamp < self.last_stamp:
                self.session += 1
                self.baseline = None
                self.frame = None
                self.epoch += 1
                self.grip_filter = GripFilter()
            self.last_stamp = stamp
            offset = now - frame.source_time
            self.baseline = (
                offset if self.baseline is None else min(self.baseline, offset)
            )
            if offset - self.baseline > self.timeout:
                self.frame = None
                self.connected = False
                self.error = "XR source clock/backlog changed; wait for new session"
                self.baseline = None
                self.session += 1
                self.grip_filter = GripFilter()
                return
            if not frame.focused or not frame.tracked:
                self.epoch += 1
            if not hasattr(self, "grip_filter"):
                self.grip_filter = GripFilter()
            filtered = self.grip_filter.update(frame.grips, frame.source_time)
            for side, grip in filtered.items():
                if grip <= GRIP_OFF:
                    self.release_epochs[side] += 1
            self.frame = replace(
                frame,
                session=self.session,
                safety_epoch=self.epoch,
                release_epochs=self.release_epochs.copy(),
                control_grips=filtered,
            )
            self.stop_latched |= frame.stop
            self.connected = True
            self.error = ""

    def run(self):
        try:
            for line in self.process.stdout:
                # SDK native stdout is diagnostic text, never a tracking frame.
                if not line.startswith("MARVIN_XR "):
                    continue
                try:
                    data = json.loads(line[len("MARVIN_XR ") :])
                    if "error" in data:
                        raise ValueError(data["error"])
                    self.accept(data, time.monotonic())
                except (ValueError, TypeError, KeyError, OverflowError) as error:
                    with self.lock:
                        self.error, self.connected, self.frame = str(error), False, None
                        self.session += 1
                        self.last_stamp = None
                        self.baseline = None
        finally:
            with self.lock:
                self.connected, self.frame = False, None
                if not self.closed:
                    self.error = (
                        "XR worker exited; restart after checking RoboticsService"
                    )

    def snapshot(self):
        with self.lock:
            fresh = (
                self.frame is not None
                and time.monotonic() - self.frame.received <= self.timeout
            )
            error = self.error or (
                "" if fresh else "XR timestamp stopped / VR Send Data disabled"
            )
            return self.frame, self.stop_latched, error, self.connected and fresh

    def close(self):
        self.closed = True
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self.thread.join(timeout=1)
        self.process.stdout.close()
