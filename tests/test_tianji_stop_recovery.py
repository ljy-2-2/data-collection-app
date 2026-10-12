"""Exercise the worker loop with a stale SDK, without connecting any hardware."""
import io
import json
import queue
from types import SimpleNamespace

import numpy as np
import pytest

from teleop_control import tianji_worker as worker


@pytest.mark.parametrize("failure", [None, "stop", "release"])
def test_stop_releases_faulted_connection_and_allows_a_fresh_check(monkeypatch, tmp_path, failure):
    incoming = queue.Queue()
    robots, samples = [], []
    phase = "fault"
    failures_observed = 0
    idle_samples = 0

    def command(action):
        incoming.put(json.dumps({"action": action, "id": action}) + "\n")

    class Input:
        def __iter__(self):
            while True:
                line = incoming.get(timeout=5)
                if line is None:
                    return
                yield line

    class Robot:
        def __init__(self, config):
            self.stale = not robots
            self.closed = False
            self.failure = None
            self.reads = 0
            self.last_command = None
            self.q = {side: np.zeros(7) for side in ("left", "right")}
            robots.append(self)

        def wait_ready(self):
            self.read()

        def read(self):
            assert not self.closed, "Worker read an already released SDK connection"
            self.reads += 1
            if self.stale:
                raise ValueError("left: robot UDP feedback not advancing")
            return self.q

        def stop(self):
            if self.failure == "stop":
                raise RuntimeError("fresh idle acknowledgement missing")

        def close(self):
            self.stop()
            if self.failure == "release":
                raise RuntimeError("SDK release failed")
            self.closed = True

    class Publisher:
        def __init__(self, **kwargs):
            pass

        def submit(self, row, *args):
            nonlocal phase, failures_observed, idle_samples
            samples.append(row)
            assert len(samples) < 100, "Stop never recovered to a checkable idle state"
            if phase == "fault":
                assert row["state"] == "error"
                robots[0].failure = failure
                command("stop")
                phase = "failed_stop" if failure else "stopping"
            elif phase == "failed_stop":
                assert row["state"] == "error", "Failed shutdown must never report idle"
                assert not robots[0].closed
                failures_observed += 1
                if failures_observed >= 4:
                    robots[0].failure = None
                    command("stop")
                    phase = "stopping"
            elif phase == "stopping" and row["state"] == "idle":
                assert robots[0].closed
                assert row["joints_deg"] == {}
                idle_samples += 1
                # Several subsequent iterations must remain idle instead of
                # repeatedly reading the stale connection and returning to error.
                if idle_samples >= 4:
                    command("check")
                    phase = "rechecking"
            elif phase == "stopping" and idle_samples:
                pytest.fail("A successful stop returned to the old feedback error")
            elif phase == "rechecking" and len(robots) == 2:
                assert robots[1].reads > 0 and not robots[1].closed
                assert row["state"] == "idle" and row["joints_deg"]
                phase = "complete"
                command("close")

        def close(self):
            pass

    monkeypatch.setattr(worker, "__file__", str(tmp_path / "src/teleop_control/tianji_worker.py"))
    monkeypatch.setattr(worker.sys, "stdin", Input())
    monkeypatch.setattr(worker.sys, "stdout", io.StringIO())
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "load_configuration", lambda: {})
    monkeypatch.setattr(worker, "Models", lambda config: SimpleNamespace(validate=lambda: None))
    monkeypatch.setattr(worker, "RecordingRobot", Robot)
    monkeypatch.setattr(worker, "Publisher", Publisher)
    monkeypatch.setattr(worker, "PicoLauncher", lambda config: SimpleNamespace(
        start=lambda: None, close=lambda: None, app_started=True, status="fixture"))
    monkeypatch.setattr(worker, "LocalVRConnection", lambda *args, **kwargs: SimpleNamespace(
        check=lambda: None, close=lambda: None, snapshot=lambda: (None, False, "", True)))
    command("check")
    try:
        worker.main()
    finally:
        incoming.put(None)
    assert phase == "complete"
    assert len(robots) == 2 and all(robot.closed for robot in robots)
