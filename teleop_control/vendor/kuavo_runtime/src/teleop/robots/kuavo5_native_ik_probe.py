"""Validate one no-motion Kuavo v63 FK -> IK -> FK service calculation."""

import math
from numbers import Real


EXPECTED_VERSION = "63"
EXPECTED_MODEL_SHA256 = "4d7a3819a4879602f8af56f1125212a27fa747c35a297cff98e7e45b504a44d1"
EXPECTED_EXECUTABLE_SHA256 = "211d4d39a8ec6e7f40610e3613431fc873bb4919bb547b2df158e6fe8cbfb793"
EXPECTED_NODE = "/arms_ik_node"


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite numeric")
    return float(value)


def _vector(value, length, name):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    return tuple(_finite(item, name) for item in value)


def validate_report(data):
    """Fail closed on any identity, side-effect, shape or accuracy mismatch."""
    if not isinstance(data, dict) or data.get("event") != "KUAVO5_NATIVE_IK_PROBE":
        raise ValueError("unexpected probe report")
    if data.get("robot_version") != EXPECTED_VERSION:
        raise ValueError("robot version mismatch")
    if data.get("model_sha256") != EXPECTED_MODEL_SHA256:
        raise ValueError("official IK model hash mismatch")
    if data.get("executable_sha256") != EXPECTED_EXECUTABLE_SHA256:
        raise ValueError("running IK executable hash mismatch")
    if data.get("service_nodes") != {"fk": EXPECTED_NODE, "ik": EXPECTED_NODE}:
        raise ValueError("FK/IK services are not owned by the audited node")
    if data.get("ik_topic_publishers") != []:
        raise ValueError("IK command topic has a publisher; refusing shared-state calculation")
    if data.get("ik_result_subscribers") != ["/nodelet_manager"]:
        raise ValueError("IK result subscribers differ from the audited runtime")
    for name in ("arm_traj_before", "arm_traj_after"):
        value = data.get(name)
        if type(value) is not int or value != 0:
            raise ValueError(f"unexpected {name} messages from arms_ik_node")
    if data.get("arm_traj_callers") != []:
        raise ValueError("a robot arm trajectory was observed during the probe")
    if data.get("ik_success") is not True:
        raise ValueError("official IK did not return exact success=true")
    if data.get("with_torso") is not False or data.get("q_torso") not in ([], ()):
        raise ValueError("torso result is not allowed on the fixed-torso probe")
    if data.get("frame") != 0 or type(data.get("frame")) is not int:
        raise ValueError("probe must explicitly use current frame mode 0")
    if data.get("joint_limits_valid") is not True:
        raise ValueError("official model joint-limit validation did not pass")
    clean = dict(data)
    clean["minimum_joint_limit_margin_rad"] = _finite(
        data.get("minimum_joint_limit_margin_rad"), "minimum_joint_limit_margin_rad")
    if clean["minimum_joint_limit_margin_rad"] < 0:
        raise ValueError("IK result is outside official model joint limits")
    if data.get("graph_stable") is not True or data.get("graph_rechecks") != 3 \
            or type(data.get("graph_rechecks")) is not int:
        raise ValueError("ROS graph was not revalidated before all compute calls")
    if data.get("physical_robot_command_sent") is not False:
        raise ValueError("report indicates a physical command")
    for name in ("q_before", "q_ik", "q_after"):
        clean[name] = _vector(data.get(name), 14, name)
    limits = (("max_position_error_m", 0.005),
              ("max_rotation_error_deg", 2.0),
              ("max_physical_arm_delta_rad", 0.03))
    for name, limit in limits:
        clean[name] = _finite(data.get(name), name)
        if clean[name] < 0 or clean[name] > limit:
            raise ValueError(f"{name} exceeds {limit}")
    clean["ik_time_ms"] = _finite(data.get("ik_time_ms"), "ik_time_ms")
    if clean["ik_time_ms"] < 0:
        raise ValueError("ik_time_ms must be non-negative")
    measured = max(abs(a - b) for a, b in zip(clean["q_before"], clean["q_after"]))
    if measured > clean["max_physical_arm_delta_rad"] + 1e-6:
        raise ValueError("continuous physical arm delta is smaller than the endpoint delta")
    return clean


def remote_program() -> str:
    """Return an in-memory ROS service probe with no actuator publisher."""
    return r'''
import hashlib
import json
import math
from pathlib import Path
import shlex
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
import xmlrpc.client
import rosgraph
import rosnode
import rospy
from kuavo_msgs.msg import sensorsData, twoArmHandPoseCmd
from kuavo_msgs.srv import fkSrv, twoArmHandPoseCmdSrv

EXPECTED_MODEL = "4d7a3819a4879602f8af56f1125212a27fa747c35a297cff98e7e45b504a44d1"
EXPECTED_EXECUTABLE = "211d4d39a8ec6e7f40610e3613431fc873bb4919bb547b2df158e6fe8cbfb793"
EXPECTED_EXECUTABLE_PATH = "/home/lab/kuavo-ros-control/devel/lib/motion_capture_ik/arms_ik_node"
ARM_NAMES = [f"zarm_{side}{index}_joint" for side in ("l", "r") for index in range(1, 8)]
signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("probe timeout")))
signal.alarm(12)
rospy.init_node("galaxis_native_ik_probe", anonymous=True, disable_signals=True, disable_rosout=True)
version = str(rospy.get_param("/robot_version"))
model = Path(rospy.get_param("/model_path"))
model_bytes = model.read_bytes()
model_sha = hashlib.sha256(model_bytes).hexdigest()
master = rosgraph.Master(rospy.get_name())
publishers, subscribers, services = master.getSystemState()
pub_map = {name: list(nodes) for name, nodes in publishers}
sub_map = {name: list(nodes) for name, nodes in subscribers}
service_map = {name: list(nodes) for name, nodes in services}
service_nodes = {
    "fk": service_map.get("/ik/fk_srv", []),
    "ik": service_map.get("/ik/two_arm_hand_pose_cmd_srv", []),
}
if version != "63" or model_sha != EXPECTED_MODEL:
    raise RuntimeError("physical robot version or IK model mismatch")
if service_nodes != {"fk": ["/arms_ik_node"], "ik": ["/arms_ik_node"]}:
    raise RuntimeError("audited FK/IK service ownership mismatch")
api_uri = rosnode.get_api_uri(master, "/arms_ik_node")
code, _, pid = xmlrpc.client.ServerProxy(api_uri).getPid(rospy.get_name())
if code != 1 or type(pid) is not int or pid <= 0:
    raise RuntimeError("cannot identify the running arms_ik_node process")
process_command = subprocess.check_output(
    ["ps", "-p", str(pid), "-o", "args="], text=True).strip()
argv = shlex.split(process_command)
if not argv or argv[0] != EXPECTED_EXECUTABLE_PATH:
    raise RuntimeError("running arms_ik_node executable path mismatch")
executable_sha = hashlib.sha256(Path(EXPECTED_EXECUTABLE_PATH).read_bytes()).hexdigest()
if executable_sha != EXPECTED_EXECUTABLE:
    raise RuntimeError("running arms_ik_node differs from the audited binary")
ik_publishers = pub_map.get("/ik/two_arm_hand_pose_cmd", [])
if ik_publishers:
    raise RuntimeError("IK command topic has active publishers")
ik_result_subscribers = sorted(sub_map.get("/ik/result", []))
if ik_result_subscribers != ["/nodelet_manager"]:
    raise RuntimeError("IK result subscribers differ from the audited runtime")
traj_publishers = sorted(pub_map.get("/kuavo_arm_traj", []))
if "/arms_ik_node" not in traj_publishers:
    raise RuntimeError("arms_ik_node trajectory publisher is not observable")

def audit_graph(stage):
    current_publishers, current_subscribers, current_services = master.getSystemState()
    current_pub = {name: sorted(nodes) for name, nodes in current_publishers}
    current_sub = {name: sorted(nodes) for name, nodes in current_subscribers}
    current_srv = {name: sorted(nodes) for name, nodes in current_services}
    if current_srv.get("/ik/fk_srv", []) != ["/arms_ik_node"]:
        raise RuntimeError("FK service ownership changed " + stage)
    if current_srv.get("/ik/two_arm_hand_pose_cmd_srv", []) != ["/arms_ik_node"]:
        raise RuntimeError("IK service ownership changed " + stage)
    if current_pub.get("/ik/two_arm_hand_pose_cmd", []):
        raise RuntimeError("IK command publisher appeared " + stage)
    if current_sub.get("/ik/result", []) != ["/nodelet_manager"]:
        raise RuntimeError("IK result subscribers changed " + stage)
    if current_pub.get("/kuavo_arm_traj", []) != traj_publishers:
        raise RuntimeError("arm trajectory publishers changed " + stage)

root = ET.fromstring(model_bytes)
joint_limits = {}
for joint in root.iter("joint"):
    name = joint.get("name")
    if name not in ARM_NAMES:
        continue
    limit = joint.find("limit")
    if limit is None or limit.get("lower") is None or limit.get("upper") is None:
        raise RuntimeError("arm joint is missing model position limits")
    joint_limits[name] = (float(limit.get("lower")), float(limit.get("upper")))
if set(joint_limits) != set(ARM_NAMES):
    raise RuntimeError("official IK model does not contain 14 expected arm joints")

lock = threading.Lock()
arm_traj_messages = []
latest_arm_state = [None]
anchor_arm_state = [None]
peak_arm_delta = [0.0]

def decode_arm(message):
    all_q = list(message.joint_data.joint_q)
    if len(all_q) != 20:
        return None
    q = [float(v) for v in all_q[4:18]]
    return q if len(q) == 14 and all(math.isfinite(v) for v in q) else None

def observe_arm_traj(message):
    header = getattr(message, "_connection_header", {}) or {}
    with lock:
        arm_traj_messages.append((time.monotonic(), header.get("callerid", "unknown")))

def observe_state(message):
    q = decode_arm(message)
    if q is None:
        return
    with lock:
        latest_arm_state[0] = q
        if anchor_arm_state[0] is not None:
            peak_arm_delta[0] = max(peak_arm_delta[0],
                                    max(abs(a-b) for a, b in zip(anchor_arm_state[0], q)))

traj_subscription = rospy.Subscriber("/kuavo_arm_traj", rospy.AnyMsg, observe_arm_traj,
                                     queue_size=100, tcp_nodelay=True)
state_subscription = rospy.Subscriber("/sensors_data_raw", sensorsData, observe_state,
                                      queue_size=1, tcp_nodelay=True)
connect_deadline = time.monotonic() + 2.0
while time.monotonic() < connect_deadline:
    with lock:
        state_ready = latest_arm_state[0] is not None
    if (state_ready and state_subscription.get_num_connections() >= 1
            and traj_subscription.get_num_connections() >= len(traj_publishers)):
        break
    time.sleep(0.02)
else:
    raise RuntimeError("state/trajectory monitors did not connect")
time.sleep(0.5)
with lock:
    arm_traj_before = len(arm_traj_messages)
    q_before = list(latest_arm_state[0])
    anchor_arm_state[0] = list(q_before)
    peak_arm_delta[0] = 0.0
if arm_traj_before:
    raise RuntimeError("robot arm trajectories are already active; calculation refused")

def ensure_quiet(stage):
    with lock:
        if arm_traj_messages:
            raise RuntimeError("robot arm trajectory observed " + stage)

def check_limits(q, label):
    margin = float("inf")
    for name, value in zip(ARM_NAMES, q):
        lower, upper = joint_limits[name]
        if not lower <= value <= upper:
            raise RuntimeError(f"{label} {name} is outside the official IK model limits")
        margin = min(margin, value-lower, upper-value)
    return margin

minimum_joint_limit_margin = check_limits(q_before, "current state")

rospy.wait_for_service("/ik/fk_srv", timeout=2.0)
rospy.wait_for_service("/ik/two_arm_hand_pose_cmd_srv", timeout=2.0)
fk = rospy.ServiceProxy("/ik/fk_srv", fkSrv, persistent=False)
ik = rospy.ServiceProxy("/ik/two_arm_hand_pose_cmd_srv", twoArmHandPoseCmdSrv,
                        persistent=False)
audit_graph("before FK")
ensure_quiet("before FK")
forward_before = fk(q_before)
ensure_quiet("after FK")
if forward_before.success is not True:
    raise RuntimeError("official FK failed at current state")

request = twoArmHandPoseCmd()
request.hand_poses = forward_before.hand_poses
request.hand_poses.left_pose.joint_angles = q_before[:7]
request.hand_poses.right_pose.joint_angles = q_before[7:]
request.use_custom_ik_param = False
request.joint_angles_as_q0 = True
request.frame = 0
audit_graph("before IK")
inverse = ik(request)
ensure_quiet("after IK")
q_ik = [float(v) for v in inverse.q_arm]
if inverse.success is not True or len(q_ik) != 14 or not all(math.isfinite(v) for v in q_ik):
    raise RuntimeError("official IK failed or returned an invalid 14-joint result")
if inverse.with_torso is not False or list(inverse.q_torso):
    raise RuntimeError("official IK returned a torso solution on the fixed-torso route")
minimum_joint_limit_margin = min(minimum_joint_limit_margin,
                                 check_limits(q_ik, "IK result"))
audit_graph("before validation FK")
forward_after = fk(q_ik)
ensure_quiet("after validation FK")
if forward_after.success is not True:
    raise RuntimeError("official FK failed on the IK candidate")

def position_error(a, b):
    if len(a) != 3 or len(b) != 3:
        raise RuntimeError("invalid FK position dimensions")
    aa, bb = [float(v) for v in a], [float(v) for v in b]
    if not all(math.isfinite(v) for v in aa+bb):
        raise RuntimeError("non-finite FK position")
    return math.sqrt(sum((x-y)**2 for x, y in zip(aa, bb)))
def rotation_error_deg(a, b):
    if len(a) != 4 or len(b) != 4:
        raise RuntimeError("invalid FK quaternion dimensions")
    aa, bb = [float(v) for v in a], [float(v) for v in b]
    if not all(math.isfinite(v) for v in aa+bb):
        raise RuntimeError("non-finite FK quaternion")
    na = math.sqrt(sum(v*v for v in aa)); nb = math.sqrt(sum(v*v for v in bb))
    if na <= 0 or nb <= 0:
        raise RuntimeError("invalid zero quaternion")
    dot = abs(sum(x*y for x, y in zip(aa, bb)) / (na*nb))
    return math.degrees(2.0*math.acos(min(1.0, max(-1.0, dot))))

before = forward_before.hand_poses
after = forward_after.hand_poses
pos_error = max(position_error(before.left_pose.pos_xyz, after.left_pose.pos_xyz),
                position_error(before.right_pose.pos_xyz, after.right_pose.pos_xyz))
rot_error = max(rotation_error_deg(before.left_pose.quat_xyzw, after.left_pose.quat_xyzw),
                rotation_error_deg(before.right_pose.quat_xyzw, after.right_pose.quat_xyzw))
time.sleep(0.75)
with lock:
    arm_traj_after = len(arm_traj_messages) - arm_traj_before
    traj_callers = sorted(set(caller for _, caller in arm_traj_messages))
    q_after = list(latest_arm_state[0])
    physical_delta = float(peak_arm_delta[0])
traj_subscription.unregister()
state_subscription.unregister()
physical_command_observed = bool(arm_traj_after or physical_delta > 0.03)
report = dict(event="KUAVO5_NATIVE_IK_PROBE", robot_version=version,
              model_sha256=model_sha, executable_sha256=executable_sha,
              service_nodes={"fk": service_nodes["fk"][0], "ik": service_nodes["ik"][0]},
              ik_topic_publishers=ik_publishers, ik_result_subscribers=ik_result_subscribers,
              arm_traj_before=arm_traj_before, arm_traj_after=arm_traj_after,
              arm_traj_callers=traj_callers,
              q_before=q_before, q_ik=q_ik, q_after=q_after,
              with_torso=bool(inverse.with_torso), q_torso=list(inverse.q_torso),
              ik_success=bool(inverse.success), ik_time_ms=float(inverse.time_cost),
              max_position_error_m=pos_error, max_rotation_error_deg=rot_error,
              max_physical_arm_delta_rad=physical_delta, frame=int(request.frame),
              joint_limits_valid=True,
              minimum_joint_limit_margin_rad=minimum_joint_limit_margin,
              graph_stable=True, graph_rechecks=3,
              physical_robot_command_sent=physical_command_observed)
print(json.dumps(report, allow_nan=False), flush=True)
signal.alarm(0)
rospy.signal_shutdown("native IK calculation probe complete")
'''
