#!/usr/bin/env python3
"""ROS 2 bridge for Galaxis PICO 4 Ultra HMD and controllers.

Receives HMD/controller poses and inputs directly from the Galaxis Unity
client and publishes the PICO-named ROS contract used by data collection.

Publishes:
  * ``/pico/hmd/state``                       (galaxis_pico_msgs/PicoHmd)
  * ``/pico/hmd/battery``                     (sensor_msgs/BatteryState)
  * ``/pico/controller/{left,right}/state``   (galaxis_pico_msgs/PicoController;
                                                pose + tracking flags only)
  * ``/pico/controller/{left,right}/tracked`` (std_msgs/Bool; mirror of
                                                ``state.tracked`` for consumers
                                                without the galaxis_pico_msgs dep)
  * ``/pico/controller/{left,right}/valid``   (std_msgs/Bool; mirror of
                                                ``state.valid``)
  * ``/pico/controller/{left,right}/joy``     (sensor_msgs/Joy; per-controller
                                                analog axes and digital buttons)
  * ``/pico/controller/{left,right}/battery`` (sensor_msgs/BatteryState)
  * ``/tf``                                    (live RViz/Foxglove and IK
                                                consumers such as umi_ik_checker)
  * ``/diagnostics``                           (diagnostic_msgs/DiagnosticArray;
                                                canonical health channel —
                                                ``connected``/``streaming``/
                                                ``fps``/``*_tracked``/``*_valid``
                                                are reported here as KeyValues)

The new ``PicoHmd`` / ``PicoController`` topics carry the timestamp metadata
(``device_time_ns`` and ``pc_monotonic_ns``) needed by the offline alignment
post-processor to reconstruct evenly-spaced timestamps from the PICO's own
clock; the bridge intentionally does not perform that correction online.
"""

from typing import Any, Dict, Optional, Set, Tuple

import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time

from sensor_msgs.msg import BatteryState, Joy
from std_msgs.msg import Bool
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

from geometry_msgs.msg import TransformStamped
from tf2_ros import (
    Buffer,
    ConnectivityException,
    ExtrapolationException,
    LookupException,
    TransformBroadcaster,
    TransformListener,
)

from galaxis_pico_msgs.msg import PicoController, PicoHmd

from galaxis_pico_bridge.transport.galaxis_tcp import (
    IsolatedGalaxisTransport,
    PicoFrame,
)


_TF_OFFSET_RETRY_S = 1.0


# ---------------------------------------------------------------------------
# Math helpers (PICO SDK native -> ROS coordinate conversion).
# ---------------------------------------------------------------------------


def _normalize_quat(x, y, z, w):
    n = (x * x + y * y + z * z + w * w) ** 0.5
    if n <= 0.0:
        return 0.0, 0.0, 0.0, 1.0
    return x / n, y / n, z / n, w / n


def _pico_pos_to_ros(x, y, z):
    """PICO SDK native position -> ROS REP-103 position.

    ``PXR_Input.GetControllerPredictPosition`` forwards the SDK
    ``localControllerPose`` values without converting them into Unity's
    left-handed world.  The native pose is right-handed with X right, Y up
    and Z backward; ROS uses X forward, Y left and Z up.

    Mapping: PICO(x,y,z) -> ROS(-z, -x, y).
    """
    return -z, -x, y


def _pico_quat_to_ros(x, y, z, w):
    """PICO SDK native quaternion -> ROS REP-103 quaternion.

    Both bases are right-handed.  Applying the same proper basis rotation as
    :func:`_pico_pos_to_ros` maps the quaternion vector part while preserving
    its scalar part: ``(x,y,z,w) -> (-z,-x,y,w)``.
    """
    return -z, -x, y, w


def _quat_conjugate(quat: Tuple[float, float, float, float]):
    x, y, z, w = quat
    return -x, -y, -z, w


def _quat_multiply(q1, q2):
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    x = w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2
    y = w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2
    z = w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2
    w = w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2
    return _normalize_quat(x, y, z, w)


def _cross(a, b):
    ax, ay, az = a
    bx, by, bz = b
    return ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx


def _rotate_vector(quat, vec):
    qx, qy, qz, qw = _normalize_quat(*quat)
    u = (qx, qy, qz)
    uv = _cross(u, vec)
    uuv = _cross(u, uv)
    uv = tuple(val * (2.0 * qw) for val in uv)
    uuv = tuple(val * 2.0 for val in uuv)
    return (
        vec[0] + uv[0] + uuv[0],
        vec[1] + uv[1] + uuv[1],
        vec[2] + uv[2] + uuv[2],
    )


def _invert_transform(trans, quat):
    inv_q = _quat_conjugate(quat)
    inv_q = _normalize_quat(*inv_q)
    neg_trans = (-trans[0], -trans[1], -trans[2])
    inv_t = _rotate_vector(inv_q, neg_trans)
    return inv_t, inv_q


def _compose_transforms(parent_trans, parent_quat, child_trans, child_quat):
    rotated_child = _rotate_vector(parent_quat, child_trans)
    composed_trans = (
        parent_trans[0] + rotated_child[0],
        parent_trans[1] + rotated_child[1],
        parent_trans[2] + rotated_child[2],
    )
    composed_quat = _quat_multiply(parent_quat, child_quat)
    return composed_trans, composed_quat


def _pico_pose_to_ros(pos, quat):
    ros_pos = _pico_pos_to_ros(*pos)
    ros_quat = _normalize_quat(*_pico_quat_to_ros(*quat))
    return ros_pos, ros_quat


def _fill_xyz(vec3, xyz: Tuple[float, float, float]) -> None:
    """Populate a Vector3-shaped field from a ROS (x, y, z) tuple."""
    vec3.x = float(xyz[0])
    vec3.y = float(xyz[1])
    vec3.z = float(xyz[2])


def _fill_xyzw(quat, xyzw: Tuple[float, float, float, float]) -> None:
    """Populate a Quaternion-shaped field from a ROS (x, y, z, w) tuple."""
    quat.x = float(xyzw[0])
    quat.y = float(xyzw[1])
    quat.z = float(xyzw[2])
    quat.w = float(xyzw[3])


def _get_xyz(obj: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    if not isinstance(obj, dict):
        return None
    try:
        return float(obj["x"]), float(obj["y"]), float(obj["z"])
    except Exception:
        return None


def _get_xyzw(obj: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    if not isinstance(obj, dict):
        return None
    try:
        return float(obj["x"]), float(obj["y"]), float(obj["z"]), float(obj["w"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Diagnostic value formatters.
# ---------------------------------------------------------------------------


def _fmt_nan_float(value: float, fmt: str = ".3f") -> str:
    """Format a float, mapping NaN (which compares unequal to itself) to "nan"."""
    return f"{value:{fmt}}" if value == value else "nan"


def _fmt_nan_int(value: int) -> str:
    """Format an int that uses negatives as the missing-value sentinel."""
    return str(value) if value >= 0 else "nan"


# (status_key, value formatter) — order is preserved in the published
# DiagnosticStatus.values list so downstream tooling can rely on it.
_DIAG_FIELDS: Tuple[Tuple[str, Any], ...] = (
    ("listening", str),
    ("listen_error", str),
    ("connected", str),
    ("streaming", str),
    ("fps", lambda v: f"{v:.1f}"),
    ("last_frame_age_s", _fmt_nan_float),
    ("offset_s", lambda v: f"{v:.6f}"),
    ("rtt_ms", _fmt_nan_float),
    ("publication_delay_s", _fmt_nan_float),
    ("liveness_send_max_s", _fmt_nan_float),
    ("liveness_send_error", str),
    ("left_tracked", str),
    ("right_tracked", str),
    ("left_valid", str),
    ("right_valid", str),
    ("delta_time_s", lambda v: f"{v}" if v == v else "nan"),
    ("hmd_batt_pct", _fmt_nan_int),
    ("left_batt_pct", _fmt_nan_int),
    ("right_batt_pct", _fmt_nan_int),
    ("hmd_charging", str),
)


# (parameter name, default topic) for every publisher topic the node exposes.
_TOPIC_PARAMS: Tuple[Tuple[str, str], ...] = (
    ("diag_topic", "/diagnostics"),
    ("hmd_topic", "/pico/hmd/state"),
    ("hmd_battery_topic", "/pico/hmd/battery"),
    ("left_controller_topic", "/pico/controller/left/state"),
    ("right_controller_topic", "/pico/controller/right/state"),
    ("left_controller_tracked_topic", "/pico/controller/left/tracked"),
    ("right_controller_tracked_topic", "/pico/controller/right/tracked"),
    ("left_controller_valid_topic", "/pico/controller/left/valid"),
    ("right_controller_valid_topic", "/pico/controller/right/valid"),
    ("left_controller_joy_topic", "/pico/controller/left/joy"),
    ("right_controller_joy_topic", "/pico/controller/right/joy"),
    ("left_controller_battery_topic", "/pico/controller/left/battery"),
    ("right_controller_battery_topic", "/pico/controller/right/battery"),
)


# Field-name maps for the per-controller PicoController message. Keeping the
# raw protocol keys here (rather than scattered across publishers) so the
# mapping remains centralized at the ROS publishing boundary.
_CONTROLLER_KEYS = {
    "left": {
        "pos": "leftControllerPosition",
        "rot": "leftControllerRotation",
        "tracked": "leftTracked",
        "valid": "leftValid",
        "joystick": "leftJoystick",
        "trigger_value": "leftTriggerValue",
        "grip_value": "leftGripValue",
        "thumb_click": "leftThumbstickClick",
        "thumb_touch": "leftThumbstickTouched",
        "trigger_press": "leftTriggerPressed",
        "trigger_touch": "leftIndexTriggerTouched",
        "grip_press": "leftGripPressed",
        "primary_press": "buttonXPressed",
        "primary_touch": "buttonXTouched",
        "secondary_press": "buttonYPressed",
        "secondary_touch": "buttonYTouched",
        "menu_press": "startPressed",
    },
    "right": {
        "pos": "rightControllerPosition",
        "rot": "rightControllerRotation",
        "tracked": "rightTracked",
        "valid": "rightValid",
        "joystick": "rightJoystick",
        "trigger_value": "rightTriggerValue",
        "grip_value": "rightGripValue",
        "thumb_click": "rightThumbstickClick",
        "thumb_touch": "rightThumbstickTouched",
        "trigger_press": "rightTriggerPressed",
        "trigger_touch": "rightIndexTriggerTouched",
        "grip_press": "rightGripPressed",
        "primary_press": "buttonAPressed",
        "primary_touch": "buttonATouched",
        "secondary_press": "buttonBPressed",
        "secondary_touch": "buttonBTouched",
        "menu_press": "backPressed",
    },
}


class PicoBridgeNode(Node):
    """Publish PICO sensor data with metadata sufficient for offline alignment."""

    def __init__(self):
        super().__init__("pico_bridge")

        # ---- Parameters -------------------------------------------------
        self.declare_parameter("galaxis_listen_host", "0.0.0.0")
        self.declare_parameter("galaxis_listen_port", 63901)

        self.declare_parameter("parent_frame", "pico_origin")
        self.declare_parameter("hmd_frame", "pico_hmd")
        self.declare_parameter("left_frame", "left_hand_root")
        self.declare_parameter("right_frame", "right_hand_root")
        self.declare_parameter("left_controller_frame", "pico_left_controller")
        self.declare_parameter("right_controller_frame", "pico_right_controller")

        for name, default in _TOPIC_PARAMS:
            self.declare_parameter(name, default)

        self.declare_parameter("connect_retry_sec", 1.0)
        self.declare_parameter("diag_period_sec", 0.5)
        self.declare_parameter("battery_status_hz", 1.0)

        # ---- Cached parameter values ------------------------------------
        # Read on every published frame; cache to avoid per-frame
        # get_parameter() lookups and Param object allocation.
        self._parent_frame = self.get_parameter("parent_frame").value
        self._hmd_frame = self.get_parameter("hmd_frame").value
        self._left_frame = self.get_parameter("left_frame").value
        self._right_frame = self.get_parameter("right_frame").value
        self._left_controller_frame = self.get_parameter("left_controller_frame").value
        self._right_controller_frame = self.get_parameter(
            "right_controller_frame"
        ).value
        self._hardware_id = "galaxis-pico4-ultra"

        # ---- Latest-frame state (consumed by status / battery / diag timers) ----
        self._lock = threading.Lock()
        self._latest_raw: Optional[Dict[str, Any]] = None

        # ---- Publishers --------------------------------------------------
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )
        self._diag_pub = self.create_publisher(
            DiagnosticArray, self.get_parameter("diag_topic").value, 10
        )
        # Small pose messages are training inputs, not disposable previews.
        # A reliable bounded history survives short recorder scheduling stalls.
        state_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE,
                               history=HistoryPolicy.KEEP_LAST, depth=256)
        self._hmd_pub = self.create_publisher(
            PicoHmd, self.get_parameter("hmd_topic").value, state_qos
        )
        self._left_ctrl_pub = self.create_publisher(
            PicoController, self.get_parameter("left_controller_topic").value, state_qos
        )
        self._right_ctrl_pub = self.create_publisher(
            PicoController, self.get_parameter("right_controller_topic").value, state_qos
        )
        # Default-reliable QoS so consumers like recording_gate (RELIABLE
        # subscriber) match without an extra QoS override.
        self._controller_flag_pubs: Dict[str, Dict[str, Any]] = {
            side: {
                "tracked": self.create_publisher(
                    Bool,
                    self.get_parameter(f"{side}_controller_tracked_topic").value,
                    10,
                ),
                "valid": self.create_publisher(
                    Bool,
                    self.get_parameter(f"{side}_controller_valid_topic").value,
                    10,
                ),
            }
            for side in ("left", "right")
        }
        self._left_joy_pub = self.create_publisher(
            Joy, self.get_parameter("left_controller_joy_topic").value, qos
        )
        self._right_joy_pub = self.create_publisher(
            Joy, self.get_parameter("right_controller_joy_topic").value, qos
        )

        self._tf_broadcaster = TransformBroadcaster(self)
        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self, spin_thread=True)
        self._hand_controller_transforms: Dict[
            Tuple[str, str],
            Tuple[Tuple[float, float, float], Tuple[float, float, float, float]],
        ] = {}
        self._missing_offset_logged: Set[Tuple[str, str]] = set()
        self._hand_controller_retry_after: Dict[Tuple[str, str], float] = {}

        # (logical name, raw pct status-key, raw charging status-key or None,
        # topic parameter name)
        self._battery_specs: Tuple[Tuple[str, str, Optional[str], str], ...] = (
            ("hmd", "hmd_batt_pct", "hmd_charging", "hmd_battery_topic"),
            ("left", "left_batt_pct", None, "left_controller_battery_topic"),
            ("right", "right_batt_pct", None, "right_controller_battery_topic"),
        )
        self._battery_pubs: Dict[str, Any] = {
            name: self.create_publisher(
                BatteryState, self.get_parameter(topic_param).value, qos
            )
            for name, _, _, topic_param in self._battery_specs
        }

        # Shared snapshot cache to deduplicate _collect_status across
        # near-simultaneous timer callbacks. TTL = half the shortest enabled
        # publish period.
        diag_period = float(self.get_parameter("diag_period_sec").value)
        diag_hz = 1.0 / diag_period if diag_period > 0 else 0.0
        enabled_hz = [
            h
            for h in (
                float(self.get_parameter("battery_status_hz").value),
                diag_hz,
            )
            if h > 0
        ]
        self._status_cache_ttl = 0.5 / max(enabled_hz) if enabled_hz else 0.0
        self._cached_status: Optional[Dict[str, Any]] = None
        self._cached_at: float = 0.0

        # ---- Transport ---------------------------------------------------
        self._transport = IsolatedGalaxisTransport(
            host=str(self.get_parameter("galaxis_listen_host").value),
            port=int(self.get_parameter("galaxis_listen_port").value),
            on_frame=self._on_frame,
            logger=self.get_logger(),
        )

        # ---- Timers ------------------------------------------------------
        self._connect_timer = self.create_timer(
            float(self.get_parameter("connect_retry_sec").value),
            self._transport.start_listener,
        )
        self._diag_timer = self.create_timer(
            float(self.get_parameter("diag_period_sec").value),
            self._publish_diagnostics,
        )
        self._battery_status_timer = self._create_hz_timer(
            "battery_status_hz",
            self._publish_battery_status,
        )

        self.get_logger().info("pico_bridge started with Galaxis PICO transport")

    def _create_hz_timer(self, hz_param: str, callback):
        hz = float(self.get_parameter(hz_param).value)
        if hz <= 0.0:
            self.get_logger().info(f"{hz_param}={hz}; timer disabled")
            return None
        return self.create_timer(1.0 / hz, callback)

    # ----------------------------------------------------------------------
    # Per-frame entry point invoked from the transport's recv thread.
    # ----------------------------------------------------------------------

    def _on_frame(self, frame: PicoFrame) -> None:
        # Replace (do not mutate) the snapshot reference so concurrent readers
        # already holding a reference keep seeing a consistent dict.
        with self._lock:
            self._latest_raw = frame.raw

        recv_stamp = self.get_clock().now().to_msg()
        # Align poses to the actual host reception, not later DDS publication.
        # Legacy/test frames without a receive wall clock retain old behavior.
        if frame.pc_received_wall_ns > 0:
            recv_stamp.sec, recv_stamp.nanosec = divmod(frame.pc_received_wall_ns, 1_000_000_000)
        self._publish_pico_messages(frame, recv_stamp)
        self._publish_tf_from_data(frame.raw, recv_stamp)
        self._publish_joy_from_data(frame.raw, recv_stamp)

    # ----------------------------------------------------------------------
    # New per-sensor publishers (PicoHmd / PicoController).
    # ----------------------------------------------------------------------

    def _publish_pico_messages(self, frame: PicoFrame, recv_stamp) -> None:
        data = frame.raw

        hmd_pos = _get_xyz(data.get("hmdPosition", {}))
        hmd_rot = _get_xyzw(data.get("hmdRotation", {}))
        if hmd_pos is not None and hmd_rot is not None:
            ros_pos, ros_quat = _pico_pose_to_ros(hmd_pos, hmd_rot)
            msg = PicoHmd()
            msg.header.stamp = recv_stamp
            msg.header.frame_id = self._parent_frame
            msg.device_time_ns = int(frame.device_time_ns)
            msg.pc_monotonic_ns = int(frame.pc_monotonic_ns)
            msg.seq = int(frame.seq)
            msg.pico_id = frame.pico_id
            _fill_xyz(msg.position, ros_pos)
            _fill_xyzw(msg.rotation, ros_quat)
            self._hmd_pub.publish(msg)

        self._publish_controller(frame, recv_stamp, "left", self._left_ctrl_pub)
        self._publish_controller(frame, recv_stamp, "right", self._right_ctrl_pub)

    def _publish_controller(
        self,
        frame: PicoFrame,
        recv_stamp,
        side: str,
        publisher,
    ) -> None:
        data = frame.raw
        keys = _CONTROLLER_KEYS[side]

        pos = _get_xyz(data.get(keys["pos"], {}))
        rot = _get_xyzw(data.get(keys["rot"], {}))
        if pos is None or rot is None:
            # Skip when the protocol payload lacks this controller's pose; the
            # next sample with valid data will publish.
            return

        ros_pos, ros_quat = _pico_pose_to_ros(pos, rot)

        msg = PicoController()
        msg.header.stamp = recv_stamp
        msg.header.frame_id = self._parent_frame
        msg.device_time_ns = int(frame.device_time_ns)
        msg.pc_monotonic_ns = int(frame.pc_monotonic_ns)
        msg.seq = int(frame.seq)
        msg.pico_id = frame.pico_id

        _fill_xyz(msg.position, ros_pos)
        _fill_xyzw(msg.rotation, ros_quat)

        msg.tracked = bool(data.get(keys["tracked"], False))
        msg.valid = bool(data.get(keys["valid"], False))

        publisher.publish(msg)

        flag_pubs = self._controller_flag_pubs[side]
        tracked_msg = Bool()
        tracked_msg.data = msg.tracked
        flag_pubs["tracked"].publish(tracked_msg)
        valid_msg = Bool()
        valid_msg.data = msg.valid
        flag_pubs["valid"].publish(valid_msg)

    # ----------------------------------------------------------------------
    # Diagnostics / status publishing (unchanged behavior, sourced from
    # transport.metrics() instead of internal fields).
    # ----------------------------------------------------------------------

    def _collect_status(self) -> Dict[str, Any]:
        """Snapshot every status field with a single lock acquisition.

        ``_latest_raw`` is treated read-only after snapshot; the recv path
        replaces the dict reference rather than mutating in place, so holding
        a reference is safe.
        """
        with self._lock:
            latest = self._latest_raw

        m = self._transport.metrics()
        connected = bool(m["connected"])
        listening = bool(m["listening"])
        listen_error = str(m["listen_error"])
        last_frame_age_s = float(m["last_frame_age_s"])
        fps = float(m["fps"])
        off_ns = int(m["offset_ns"])
        rtt_ns = m["rtt_ns"]

        # Streaming = connected and the last sample arrived within the last
        # second. NaN compares false, so missing samples mark streaming False.
        streaming = (
            connected
            and last_frame_age_s == last_frame_age_s
            and last_frame_age_s <= 1.0
        )

        offset_s = off_ns / 1e9
        rtt_ms = float("nan") if rtt_ns is None else rtt_ns / 1e6

        def _batt(key: str) -> int:
            if not latest:
                return -1
            val = latest.get(key, None)
            return -1 if val is None else int(val)

        def _float(key: str) -> float:
            if not latest:
                return float("nan")
            val = latest.get(key, None)
            return float("nan") if val is None else float(val)

        def _bool(key: str) -> bool:
            return bool(latest.get(key, False)) if latest else False

        return {
            "listening": listening,
            "listen_error": listen_error,
            "connected": connected,
            "streaming": bool(streaming),
            "fps": fps,
            "last_frame_age_s": last_frame_age_s,
            "offset_s": offset_s,
            "rtt_ms": rtt_ms,
            "publication_delay_s": float(m.get("publication_delay_s", 0.0)),
            "liveness_send_max_s": float(m.get("liveness_send_max_s", 0.0)),
            "liveness_send_error": str(m.get("liveness_send_error", "")),
            "left_tracked": _bool("leftTracked"),
            "right_tracked": _bool("rightTracked"),
            "left_valid": _bool("leftValid"),
            "right_valid": _bool("rightValid"),
            "delta_time_s": _float("deltaTime"),
            "hmd_batt_pct": _batt("hmdBattPct"),
            "left_batt_pct": _batt("leftBattPct"),
            "right_batt_pct": _batt("rightBattPct"),
            "hmd_charging": _bool("hmdCharging"),
        }

    def _get_status(self) -> Dict[str, Any]:
        """Return a recent snapshot, reusing the cached one within TTL.

        rclpy's default single-threaded executor serializes timer callbacks,
        so callbacks that expire in the same spin iteration share one
        snapshot without extra locking.
        """
        now = time.monotonic()
        if (
            self._cached_status is not None
            and (now - self._cached_at) < self._status_cache_ttl
        ):
            return self._cached_status
        self._cached_status = self._collect_status()
        self._cached_at = now
        return self._cached_status

    def _publish_battery_status(self):
        status = self._get_status()
        stamp = self.get_clock().now().to_msg()
        for name, pct_key, charging_key, _ in self._battery_specs:
            msg = BatteryState()
            msg.header.stamp = stamp
            msg.location = name
            msg.voltage = float("nan")
            msg.temperature = float("nan")
            msg.current = float("nan")
            msg.charge = float("nan")
            msg.capacity = float("nan")
            msg.design_capacity = float("nan")
            msg.power_supply_health = BatteryState.POWER_SUPPLY_HEALTH_UNKNOWN
            msg.power_supply_technology = BatteryState.POWER_SUPPLY_TECHNOLOGY_UNKNOWN

            pct = status[pct_key]
            if pct < 0:
                msg.percentage = float("nan")
                msg.present = False
                msg.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_UNKNOWN
            else:
                msg.percentage = float(pct) / 100.0
                msg.present = True
                if charging_key is not None:
                    msg.power_supply_status = (
                        BatteryState.POWER_SUPPLY_STATUS_CHARGING
                        if status[charging_key]
                        else BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
                    )
                else:
                    msg.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_UNKNOWN
            self._battery_pubs[name].publish(msg)

    def _publish_diagnostics(self):
        status = self._get_status()

        arr = DiagnosticArray()
        arr.header.stamp = self.get_clock().now().to_msg()

        st = DiagnosticStatus()
        st.name = "pico_bridge"
        st.hardware_id = self._hardware_id

        if not status["listening"]:
            st.level = DiagnosticStatus.ERROR
            st.message = (
                f"listener error: {status['listen_error']}"
                if status["listen_error"]
                else "listener starting"
            )
        elif not status["connected"]:
            st.level = DiagnosticStatus.WARN
            st.message = "listening; headset disconnected"
        elif not status["streaming"]:
            st.level = DiagnosticStatus.WARN
            st.message = "connected but not streaming"
        else:
            st.level = DiagnosticStatus.OK
            st.message = "streaming"

        st.values = [
            KeyValue(key=key, value=fmt(status[key])) for key, fmt in _DIAG_FIELDS
        ]

        arr.status.append(st)
        self._diag_pub.publish(arr)

    # ----------------------------------------------------------------------
    # TF + per-controller Joy publishing.
    # ----------------------------------------------------------------------

    def _publish_tf_from_data(self, data: Dict[str, Any], stamp) -> None:
        # Called only for a received frame. A periodic connection snapshot may
        # lag the first frame; it must not suppress its TF or button edges.
        hmd_pos = _get_xyz(data.get("hmdPosition", {}))
        hmd_rot = _get_xyzw(data.get("hmdRotation", {}))
        l_pos = _get_xyz(data.get("leftControllerPosition", {}))
        l_rot = _get_xyzw(data.get("leftControllerRotation", {}))
        r_pos = _get_xyz(data.get("rightControllerPosition", {}))
        r_rot = _get_xyzw(data.get("rightControllerRotation", {}))

        if hmd_pos and hmd_rot:
            ros_pos, ros_quat = _pico_pose_to_ros(hmd_pos, hmd_rot)
            self._send_tf(self._parent_frame, self._hmd_frame, stamp, ros_pos, ros_quat)
        if l_pos and l_rot:
            self._publish_hand_root_tf(
                self._parent_frame,
                self._left_frame,
                self._left_controller_frame,
                stamp,
                l_pos,
                l_rot,
            )
        if r_pos and r_rot:
            self._publish_hand_root_tf(
                self._parent_frame,
                self._right_frame,
                self._right_controller_frame,
                stamp,
                r_pos,
                r_rot,
            )

    def _send_tf(self, parent: str, child: str, stamp, ros_pos, ros_quat):
        """Broadcast a ROS-coordinate TF. Callers convert from PICO first."""
        t = TransformStamped()
        t.header.stamp = stamp
        t.header.frame_id = parent
        t.child_frame_id = child
        _fill_xyz(t.transform.translation, ros_pos)
        _fill_xyzw(t.transform.rotation, ros_quat)
        self._tf_broadcaster.sendTransform(t)

    def _publish_hand_root_tf(
        self,
        parent: str,
        hand_frame: str,
        controller_frame: str,
        stamp,
        controller_pos: Tuple[float, float, float],
        controller_quat: Tuple[float, float, float, float],
    ):
        if not hand_frame or not controller_frame:
            return
        offset = self._get_hand_controller_transform(hand_frame, controller_frame)
        if offset is None:
            return
        hand_to_controller_trans, hand_to_controller_quat = offset
        controller_to_hand_trans, controller_to_hand_quat = _invert_transform(
            hand_to_controller_trans,
            hand_to_controller_quat,
        )
        controller_ros_pos, controller_ros_quat = _pico_pose_to_ros(
            controller_pos, controller_quat
        )
        hand_trans, hand_quat = _compose_transforms(
            controller_ros_pos,
            controller_ros_quat,
            controller_to_hand_trans,
            controller_to_hand_quat,
        )
        self._send_tf(parent, hand_frame, stamp, hand_trans, hand_quat)

    def _get_hand_controller_transform(
        self,
        hand_frame: str,
        controller_frame: str,
    ) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float, float]]]:
        key = (hand_frame, controller_frame)
        if key in self._hand_controller_transforms:
            return self._hand_controller_transforms[key]

        # This method runs synchronously in the TCP receive callback.  Waiting
        # for a missing robot-description TF here stalls every subsequent VR
        # sample (two 100 ms waits reduced a 90 Hz stream to roughly 5 Hz).
        # Probe without a timeout and rate-limit misses instead.  tf2's
        # three-argument lookup is an immediate cache query.
        now = time.monotonic()
        if now < self._hand_controller_retry_after.get(key, 0.0):
            return None

        try:
            tf_msg = self._tf_buffer.lookup_transform(
                hand_frame,
                controller_frame,
                Time(),
            )
        except (LookupException, ConnectivityException, ExtrapolationException) as exc:
            self._hand_controller_retry_after[key] = now + _TF_OFFSET_RETRY_S
            if key not in self._missing_offset_logged:
                self.get_logger().warn(
                    f"TF lookup failed for {hand_frame} <- {controller_frame}: {exc}"
                )
                self._missing_offset_logged.add(key)
            return None

        trans = (
            tf_msg.transform.translation.x,
            tf_msg.transform.translation.y,
            tf_msg.transform.translation.z,
        )
        quat = _normalize_quat(
            tf_msg.transform.rotation.x,
            tf_msg.transform.rotation.y,
            tf_msg.transform.rotation.z,
            tf_msg.transform.rotation.w,
        )
        self._hand_controller_transforms[key] = (trans, quat)
        self._hand_controller_retry_after.pop(key, None)
        if key in self._missing_offset_logged:
            self._missing_offset_logged.remove(key)
            self.get_logger().info(
                f"TF offset resolved for {hand_frame} <- {controller_frame}"
            )
        return self._hand_controller_transforms[key]

    # Per-controller sensor_msgs/Joy axis / button layout. axes[2] (trigger)
    # and axes[3] (grip) carry analog values when the transport provides them,
    # otherwise they are NaN.
    #
    # axes:
    #   [0] joystick_x
    #   [1] joystick_y
    #   [2] trigger_value
    #   [3] grip_value
    #
    # buttons:
    #   [0] primary_pressed   (X for left / A for right)
    #   [1] secondary_pressed (Y for left / B for right)
    #   [2] menu_pressed      (Start for left / Back for right)
    #   [3] thumbstick_clicked
    #   [4] trigger_pressed
    #   [5] grip_pressed
    #   [6] primary_touched
    #   [7] secondary_touched
    #   [8] thumbstick_touched
    #   [9] trigger_touched
    #   [10] thumb_rest_touched (always 0 under the current protocol)
    def _publish_joy_from_data(self, data: Dict[str, Any], stamp) -> None:
        self._publish_controller_joy(data, stamp, "left", self._left_joy_pub)
        self._publish_controller_joy(data, stamp, "right", self._right_joy_pub)

    def _publish_controller_joy(
        self,
        data: Dict[str, Any],
        stamp,
        side: str,
        publisher,
    ) -> None:
        keys = _CONTROLLER_KEYS[side]
        joystick = data.get(keys["joystick"], {}) or {}

        joy = Joy()
        joy.header.stamp = stamp
        joy.axes = [
            float(joystick.get("x", 0.0) or 0.0),
            float(joystick.get("y", 0.0) or 0.0),
            float(data.get(keys["trigger_value"], float("nan"))),
            float(data.get(keys["grip_value"], float("nan"))),
        ]
        joy.buttons = [
            int(bool(data.get(keys["primary_press"], False))),
            int(bool(data.get(keys["secondary_press"], False))),
            int(bool(data.get(keys["menu_press"], False))),
            int(bool(data.get(keys["thumb_click"], False))),
            int(bool(data.get(keys["trigger_press"], False))),
            int(bool(data.get(keys["grip_press"], False))),
            int(bool(data.get(keys["primary_touch"], False))),
            int(bool(data.get(keys["secondary_touch"], False))),
            int(bool(data.get(keys["thumb_touch"], False))),
            int(bool(data.get(keys["trigger_touch"], False))),
            0,
        ]
        publisher.publish(joy)

    # ----------------------------------------------------------------------
    # Shutdown.
    # ----------------------------------------------------------------------

    def destroy_node(self):
        try:
            self._transport.stop()
        finally:
            super().destroy_node()


def main():
    rclpy.init()
    node = PicoBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
