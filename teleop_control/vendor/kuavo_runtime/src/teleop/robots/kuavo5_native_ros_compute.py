"""Audited Kuavo v63 ROS FK/IK compute bridge; no automatic connection.

The remote program is intended for a dedicated, explicitly authorised SSH
session. The PC-side JSON-line RPC has no robot-motion operation.
"""

import json
import math
from queue import Empty, Queue
import threading

from teleop.robots.kuavo5_native_ik_probe import (
    EXPECTED_EXECUTABLE_SHA256, EXPECTED_MODEL_SHA256,
)


_END = object()


class JsonLineComputeRPC:
    """Validate identity and match one response to one FK/IK request."""

    def __init__(self, writer, reader, *, timeout_s: float = 0.1,
                 ready_timeout_s: float = 5.0):
        for label, value in (("timeout_s", timeout_s), ("ready_timeout_s", ready_timeout_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} must be finite and positive")
        self._writer, self._reader = writer, reader
        self._timeout_s = float(timeout_s)
        self._received = Queue()
        self._lock = threading.Lock()
        self._last_id = 0
        self._failed = False
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        packet = self._take(float(ready_timeout_s))
        if not isinstance(packet, dict) or packet.get("event") != "ready" \
                or packet.get("robot_version") != "63" \
                or packet.get("model_sha256") != EXPECTED_MODEL_SHA256 \
                or packet.get("executable_sha256") != EXPECTED_EXECUTABLE_SHA256 \
                or packet.get("service_nodes") != {"fk": "/arms_ik_node", "ik": "/arms_ik_node"} \
                or packet.get("ik_topic_publishers") != [] \
                or packet.get("ik_result_subscribers") != ["/nodelet_manager"] \
                or packet.get("arm_traj_before") != 0 \
                or packet.get("physical_robot_command_sent") is not False:
            self._failed = True
            raise ValueError("Kuavo compute bridge identity or side-effect precheck failed")

    def _read_loop(self):
        try:
            while True:
                line = self._reader.readline(65538)
                if not line:
                    break
                if len(line) > 65536:
                    raise ValueError("compute response exceeds 64 KiB")
                self._received.put(json.loads(line))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            self._received.put(exc)
        finally:
            self._received.put(_END)

    def _take(self, timeout_s=None):
        try:
            packet = self._received.get(timeout=self._timeout_s if timeout_s is None else timeout_s)
        except Empty as exc:
            self._failed = True
            raise ValueError("Kuavo compute service timed out") from exc
        if packet is _END or isinstance(packet, BaseException):
            self._failed = True
            raise ValueError("Kuavo compute bridge disconnected or returned invalid JSON")
        return packet

    def call(self, operation: str, request: dict):
        if operation not in ("fk", "ik"):
            raise ValueError("only official FK and IK compute operations are allowed")
        if not isinstance(request, dict):
            raise ValueError("compute request must be a mapping")
        with self._lock:
            if self._failed:
                raise ValueError("Kuavo compute bridge is unusable after failure")
            self._last_id += 1
            request_id = self._last_id
            try:
                self._writer.write(json.dumps(dict(event="request", request_id=request_id,
                                                   operation=operation, payload=request),
                                              allow_nan=False) + "\n")
                self._writer.flush()
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                self._failed = True
                raise ValueError("cannot write Kuavo compute request") from exc
            packet = self._take()
            if not isinstance(packet, dict) or packet.get("event") != "response" \
                    or packet.get("request_id") != request_id \
                    or packet.get("operation") != operation \
                    or not isinstance(packet.get("payload"), dict):
                self._failed = True
                raise ValueError("Kuavo compute response is out of order or invalid")
            if packet["payload"].get("bridge_error"):
                self._failed = True
                raise ValueError("Kuavo compute bridge refused: " +
                                 str(packet["payload"]["bridge_error"])[:200])
            return packet["payload"]


def remote_program() -> str:
    """Return an in-memory, compute-only ROS worker; never run on import."""
    return r'''
import hashlib
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
import xmlrpc.client
import rosgraph
import rosnode
import rospy
from kuavo_msgs.msg import sensorsData, twoArmHandPoseCmd
from kuavo_msgs.srv import fkSrv, twoArmHandPoseCmdSrv

MODEL_SHA = "4d7a3819a4879602f8af56f1125212a27fa747c35a297cff98e7e45b504a44d1"
EXECUTABLE_SHA = "211d4d39a8ec6e7f40610e3613431fc873bb4919bb547b2df158e6fe8cbfb793"
EXECUTABLE_PATH = "/home/lab/kuavo-ros-control/devel/lib/motion_capture_ik/arms_ik_node"
ARM_NAMES = [f"zarm_{s}{i}_joint" for s in ("l", "r") for i in range(1, 8)]

rospy.init_node("galaxis_native_compute", anonymous=True, disable_rosout=True)
version = str(rospy.get_param("/robot_version"))
model_bytes = Path(rospy.get_param("/model_path")).read_bytes()
model_sha = hashlib.sha256(model_bytes).hexdigest()
if version != "63" or model_sha != MODEL_SHA:
    raise RuntimeError("Kuavo version or official IK model mismatch")
master = rosgraph.Master(rospy.get_name())
pub, sub, srv = master.getSystemState()
publishers = {name: sorted(nodes) for name, nodes in pub}
subscribers = {name: sorted(nodes) for name, nodes in sub}
services = {name: sorted(nodes) for name, nodes in srv}
if services.get("/ik/fk_srv") != ["/arms_ik_node"] or services.get("/ik/two_arm_hand_pose_cmd_srv") != ["/arms_ik_node"]:
    raise RuntimeError("FK/IK services are not owned by the audited node")
if publishers.get("/ik/two_arm_hand_pose_cmd", []):
    raise RuntimeError("a concurrent IK command topic publisher is active")
if subscribers.get("/ik/result", []) != ["/nodelet_manager"]:
    raise RuntimeError("IK result subscribers differ from the audited runtime")
traj_publishers = publishers.get("/kuavo_arm_traj", [])
if "/arms_ik_node" not in traj_publishers:
    raise RuntimeError("arm trajectory output is not observable")

api_uri = rosnode.get_api_uri(master, "/arms_ik_node")
code, _, pid = xmlrpc.client.ServerProxy(api_uri).getPid(rospy.get_name())
if code != 1 or type(pid) is not int or pid <= 0:
    raise RuntimeError("cannot identify official IK process")
argv = shlex.split(subprocess.check_output(["ps", "-p", str(pid), "-o", "args="], text=True).strip())
if not argv or argv[0] != EXECUTABLE_PATH:
    raise RuntimeError("official IK executable path changed")
executable_sha = hashlib.sha256(Path(EXECUTABLE_PATH).read_bytes()).hexdigest()
if executable_sha != EXECUTABLE_SHA:
    raise RuntimeError("official IK executable hash changed")

limits = {}
for joint in ET.fromstring(model_bytes).iter("joint"):
    name = joint.get("name")
    if name in ARM_NAMES:
        limit = joint.find("limit")
        if limit is None or limit.get("lower") is None or limit.get("upper") is None:
            raise RuntimeError("official model arm limit missing")
        limits[name] = (float(limit.get("lower")), float(limit.get("upper")))
if set(limits) != set(ARM_NAMES):
    raise RuntimeError("official model arm order mismatch")

trajectory_callers = []
trajectory_lock = threading.Lock()
def on_trajectory(message):
    header = getattr(message, "_connection_header", {}) or {}
    with trajectory_lock:
        trajectory_callers.append(header.get("callerid", "unknown"))
trajectory_sub = rospy.Subscriber("/kuavo_arm_traj", rospy.AnyMsg, on_trajectory,
                                  queue_size=100, tcp_nodelay=True)
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline and trajectory_sub.get_num_connections() < len(traj_publishers):
    time.sleep(0.02)
if trajectory_sub.get_num_connections() < len(traj_publishers):
    raise RuntimeError("trajectory monitor did not connect")
time.sleep(0.5)
with trajectory_lock:
    arm_traj_before = len(trajectory_callers)
if arm_traj_before:
    raise RuntimeError("arm trajectories already active")

def audit_graph():
    current_pub, current_sub, current_srv = master.getSystemState()
    p = {name: sorted(nodes) for name, nodes in current_pub}
    s = {name: sorted(nodes) for name, nodes in current_sub}
    v = {name: sorted(nodes) for name, nodes in current_srv}
    if v.get("/ik/fk_srv") != ["/arms_ik_node"] or v.get("/ik/two_arm_hand_pose_cmd_srv") != ["/arms_ik_node"]:
        raise RuntimeError("FK/IK service owner changed")
    if p.get("/ik/two_arm_hand_pose_cmd", []):
        raise RuntimeError("IK command topic publisher appeared")
    if s.get("/ik/result", []) != ["/nodelet_manager"]:
        raise RuntimeError("IK result subscriber changed")
    if p.get("/kuavo_arm_traj", []) != traj_publishers:
        raise RuntimeError("trajectory publisher graph changed")
    with trajectory_lock:
        if trajectory_callers:
            raise RuntimeError("arm trajectory message observed")

def finite_vector(values, length, label):
    if not isinstance(values, list) or len(values) != length:
        raise ValueError(label + " dimension mismatch")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError(label + " must be finite numeric")
    return list(map(float, values))

def check_arm_limits(q):
    for name, value in zip(ARM_NAMES, q):
        lo, hi = limits[name]
        if not lo <= value <= hi:
            raise ValueError("official model arm limit exceeded: " + name)

def pose_json(pose):
    return dict(pos_xyz=list(pose.pos_xyz), quat_xyzw=list(pose.quat_xyzw))
def hands_json(hands):
    return {"left_pose": pose_json(hands.left_pose),
            "right_pose": pose_json(hands.right_pose)}

rospy.wait_for_service("/ik/fk_srv", timeout=2.0)
rospy.wait_for_service("/ik/two_arm_hand_pose_cmd_srv", timeout=2.0)
fk = rospy.ServiceProxy("/ik/fk_srv", fkSrv, persistent=False)
ik = rospy.ServiceProxy("/ik/two_arm_hand_pose_cmd_srv", twoArmHandPoseCmdSrv, persistent=False)
print(json.dumps(dict(event="ready", robot_version=version, model_sha256=model_sha,
                      executable_sha256=executable_sha,
                      service_nodes={"fk": "/arms_ik_node", "ik": "/arms_ik_node"},
                      ik_topic_publishers=[], ik_result_subscribers=["/nodelet_manager"],
                      arm_traj_before=arm_traj_before,
                      physical_robot_command_sent=False)), flush=True)

try:
    for line in sys.stdin:
        request_id, operation = None, None
        try:
            if len(line) > 65536:
                raise ValueError("compute request too large")
            packet = json.loads(line)
            if packet.get("event") != "request" or type(packet.get("request_id")) is not int:
                raise ValueError("invalid compute request envelope")
            request_id, operation = packet["request_id"], packet.get("operation")
            payload = packet.get("payload")
            if not isinstance(payload, dict) or operation not in ("fk", "ik"):
                raise ValueError("only FK/IK compute requests are accepted")
            audit_graph()
            if operation == "fk":
                q = finite_vector(payload.get("q"), 14, "FK arm joints")
                check_arm_limits(q)
                result = fk(q)
                response = dict(success=bool(result.success), hand_poses=hands_json(result.hand_poses))
            else:
                if type(payload.get("frame")) is not int or payload["frame"] != 0:
                    raise ValueError("only audited frame mode 0 is accepted")
                if payload.get("use_custom_ik_param") is not False or payload.get("joint_angles_as_q0") is not True:
                    raise ValueError("current-state q0 seed is required")
                hands = payload.get("hand_poses")
                if not isinstance(hands, dict) or set(hands) != {"left_pose", "right_pose"}:
                    raise ValueError("both hand targets are required")
                command = twoArmHandPoseCmd()
                command.use_custom_ik_param = False
                command.joint_angles_as_q0 = True
                command.frame = 0
                for side in ("left", "right"):
                    source = hands[side + "_pose"]
                    target = getattr(command.hand_poses, side + "_pose")
                    target.pos_xyz = finite_vector(source.get("pos_xyz"), 3, side + " position")
                    target.quat_xyzw = finite_vector(source.get("quat_xyzw"), 4, side + " quaternion")
                    target.elbow_pos_xyz = finite_vector(source.get("elbow_pos_xyz"), 3, side + " elbow")
                    target.joint_angles = finite_vector(source.get("joint_angles"), 7, side + " q0")
                seed = list(command.hand_poses.left_pose.joint_angles) + list(command.hand_poses.right_pose.joint_angles)
                check_arm_limits(seed)
                result = ik(command)
                q_arm = finite_vector(list(result.q_arm), 14, "IK arm result") if result.success else []
                if result.success:
                    check_arm_limits(q_arm)
                response = dict(success=bool(result.success), with_torso=bool(result.with_torso),
                                q_arm=q_arm, q_torso=list(result.q_torso),
                                time_cost=float(result.time_cost),
                                hand_poses=hands_json(result.hand_poses) if result.success else {})
            time.sleep(0.01)
            audit_graph()
            print(json.dumps(dict(event="response", request_id=request_id,
                                  operation=operation, payload=response), allow_nan=False), flush=True)
        except Exception as exc:
            if request_id is not None and operation in ("fk", "ik"):
                print(json.dumps(dict(event="response", request_id=request_id,
                                      operation=operation,
                                      payload={"bridge_error": str(exc)[:200]})), flush=True)
            break
finally:
    trajectory_sub.unregister()
    rospy.signal_shutdown("native compute bridge stopped")
'''
