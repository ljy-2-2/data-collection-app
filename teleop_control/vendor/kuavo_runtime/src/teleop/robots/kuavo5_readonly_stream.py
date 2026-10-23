"""SSH read-only state protocol. Importing it does not connect or start ROS."""

import json
import math
from numbers import Real
from types import SimpleNamespace

from teleop.robots.kuavo5_state_receiver import Kuavo5StateReceiver


class KuavoReadonlyFeed:
    """Validate remote identity before delivering joint feedback to Receiver."""

    def __init__(self, receiver: Kuavo5StateReceiver, *, version: str,
                 names: tuple[str, ...], urdf_sha256: str):
        self.receiver = receiver
        self.version, self.names, self.sha256 = version, tuple(names), urdf_sha256
        self.ready = False

    def transport_error(self, reason: str) -> None:
        self.receiver.invalidate_input(reason)

    def accept_line(self, line: str) -> bool:
        try:
            return self._accept(line)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.transport_error(f"read-only stream rejected: {exc}")
            raise ValueError(str(exc)) from exc

    def _accept(self, line: str) -> bool:
        if len(line) > 65536:
            raise ValueError("state line exceeds 64 KiB")
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("state packet must be an object")
        if data.get("event") == "ready":
            self.ready = False
            if (data.get("robot_version") != self.version
                    or data.get("joint_names") != list(self.names)
                    or data.get("urdf_sha256") != self.sha256):
                raise ValueError("remote version/model/order handshake mismatch")
            self.ready = True
            return True
        if data.get("event") not in ("frame", "heartbeat"):
            raise ValueError("unexpected read-only stream event")
        if not self.ready:
            raise ValueError("matching remote handshake required before feedback")
        if data.get("robot_version") != self.version:
            raise ValueError("frame robot version mismatch")
        if data.get("event") == "heartbeat":
            # Connection liveness must never refresh the age of joint feedback.
            return True
        age = data.get("source_age_ms")
        if (isinstance(age, bool) or not isinstance(age, Real)
                or not math.isfinite(age) or not 0 <= age <= 250):
            raise ValueError("remote observation is invalid or older than 250 ms")
        return self.receiver.on_message(SimpleNamespace(
            joint_data=SimpleNamespace(joint_q=data["joint_q"]),
            sensor_time=SimpleNamespace(secs=data["sensor_secs"], nsecs=data["sensor_nsecs"])))


def remote_program() -> str:
    """In-memory ROS subscriber; no control SDK, service or actuator publisher."""
    return r'''
import hashlib
import json
from pathlib import Path
import signal
import threading
import time
import xml.etree.ElementTree as ET
import rospy
from kuavo_msgs.msg import sensorsData

stop = threading.Event()
for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, lambda *_: stop.set())
rospy.init_node("galaxis_readonly_state", anonymous=True, disable_signals=True, disable_rosout=True)
version = str(rospy.get_param("/robot_version"))
if version != "63":
    raise ValueError("this audited entry requires physical robot version 63")
model = Path(rospy.get_param("/urdfFile"))
xml = model.read_bytes()
root = ET.fromstring(xml)
links = ["knee_link", "leg_link", "waist_link", "waist_yaw_link"]
links += [f"zarm_{s}{i}_link" for s in ("l", "r") for i in range(1, 8)]
links += ["zhead_1_link", "zhead_2_link"]
names = []
for link in links:
    matches = [j.get("name") for j in root.findall("joint")
               if j.find("child") is not None and j.find("child").get("link") == link]
    if len(matches) != 1:
        raise ValueError("cannot identify joint for child link " + link)
    names.append(matches[0])
print(json.dumps(dict(event="ready", robot_version=version, joint_names=names,
                     urdf_sha256=hashlib.sha256(xml).hexdigest())), flush=True)
lock = threading.Lock()
latest = [None]
def receive(m):
    packet = dict(event="frame", robot_version=version,
                  sensor_secs=m.sensor_time.secs, sensor_nsecs=m.sensor_time.nsecs,
                  joint_q=list(m.joint_data.joint_q))
    with lock:
        latest[0] = (packet, time.monotonic_ns())
subscription = rospy.Subscriber("/sensors_data_raw", sensorsData, receive,
                                queue_size=1, tcp_nodelay=True)
last_stamp = None
heartbeat_at = time.monotonic()
try:
    while not stop.wait(0.05) and not rospy.is_shutdown():
        if time.monotonic() - heartbeat_at >= 0.5:
            print(json.dumps(dict(event="heartbeat", robot_version=version)), flush=True)
            heartbeat_at = time.monotonic()
        with lock:
            item = latest[0]
        if item is None:
            continue
        packet, received = item
        stamp = (packet["sensor_secs"], packet["sensor_nsecs"])
        if stamp == last_stamp:
            continue
        packet = dict(packet, source_age_ms=(time.monotonic_ns() - received) / 1e6)
        print(json.dumps(packet, allow_nan=False), flush=True)
        last_stamp = stamp
except (BrokenPipeError, KeyboardInterrupt):
    pass
finally:
    subscription.unregister()
    rospy.signal_shutdown("read-only stream finished")
'''
