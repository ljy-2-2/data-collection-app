"""Kuavo read-only state boundary; importing this module never starts ROS/SDK.

The caller supplies an audited schema and joint names from robot metadata.
accept_sdk_joint_state consumes KuavoJointData-shaped snapshots at RECEIVE time,
not repeatedly polled SDK caches (which contain neither names nor timestamps).
The receive callback is in kuavo5_state_receiver; physical version audit and
real ROS wiring remain a separate stage.
"""

import math
from numbers import Real
from threading import Lock
import time
from typing import Iterable, Protocol, Sequence
import xml.etree.ElementTree as ET

from teleop.contracts.capabilities import RobotCapabilities
from teleop.contracts.command import ValidatedJointCommand
from teleop.contracts.robot_state import RobotState
from teleop.contracts.schema import JointInfo, JointSchema
from teleop.robots.base import IKSolver, RobotAdapter, UnsupportedCapabilityError


class SDKJointState(Protocol):
    position: Sequence[float]


class KuavoStateUnavailable(RuntimeError):
    """No usable, fresh Kuavo joint feedback is available."""


def _names(names: Iterable[str]) -> tuple[str, ...]:
    if isinstance(names, str):
        raise ValueError("joint names must be a sequence, not a string")
    result = tuple(names)
    if not result or any(not isinstance(n, str) or not n.strip() for n in result):
        raise ValueError("joint names must be non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError("duplicate joint names")
    return result


def _timestamp(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("timestamp must be a non-negative monotonic integer")
    return value


def schema_from_urdf(urdf_xml: str, joint_names: Iterable[str], *, require_velocity=False) -> JointSchema:
    """Read limits for explicitly named revolute joints; XML order is NOT SDK order.

    This is an observation schema, not permission to command these joints.
    Preserve optional velocity limits for offline/read-only fixtures. Live
    execution requires them explicitly; malformed supplied limits always fail.
    Continuous/prismatic/fixed joints are rejected rather than inventing limits
    or mixing metres into the framework's radian-only joint contract.
    """
    names = _names(joint_names)
    root = ET.fromstring(urdf_xml)
    elements = root.findall("joint")
    by_name = {element.get("name"): element for element in elements}
    if len(by_name) != len(elements):
        raise ValueError("duplicate URDF joint names")
    joints = []
    for name in names:
        element = by_name.get(name)
        if element is None or element.get("type") != "revolute":
            raise ValueError(f"missing or unsupported revolute joint: {name}")
        limit = element.find("limit")
        if limit is None or "lower" not in limit.attrib or "upper" not in limit.attrib:
            raise ValueError(f"missing URDF position limits: {name}")
        if require_velocity and "velocity" not in limit.attrib:
            raise ValueError(f"missing URDF velocity limit: {name}")
        velocity = float(limit.get("velocity")) if "velocity" in limit.attrib else None
        joints.append(JointInfo(name, float(limit.get("lower")), float(limit.get("upper")),
                                max_step=0.0, independently_commandable=False,
                                max_velocity=velocity))
    return JointSchema(tuple(joints))


def v6_candidate_schema(urdf_xml: str) -> JointSchema:
    """Build a read-only schema in the official v6-family child-link order.

    The order is shared by the SDK's v6 branch; the selected URDF still has to
    match the explicit physical robot version.  This is not live-state proof.
    """
    links = ("knee_link", "leg_link", "waist_link", "waist_yaw_link") + tuple(
        f"zarm_{side}{index}_link" for side in ("l", "r") for index in range(1, 8)
    ) + ("zhead_1_link", "zhead_2_link")
    elements = ET.fromstring(urdf_xml).findall("joint")
    names = []
    for link in links:
        matches = [j.get("name") for j in elements
                   if j.find("child") is not None and j.find("child").get("link") == link]
        if len(matches) != 1:
            raise ValueError(f"expected exactly one joint for child link {link}")
        names.append(matches[0])
    return schema_from_urdf(urdf_xml, names)


def v62_candidate_schema(urdf_xml: str) -> JointSchema:
    """Backward-compatible name for the former v62-only offline helper."""
    return v6_candidate_schema(urdf_xml)


class Kuavo5Adapter(RobotAdapter):
    """Read-only, injected-feedback adapter. No SDK control object is constructed."""

    def __init__(self, schema: JointSchema, *, robot_version: str,
                 max_state_age_ms: int = 250, ik_solver: IKSolver | None = None) -> None:
        if not isinstance(schema, JointSchema) or not schema.joints:
            raise ValueError("a non-empty JointSchema is required")
        if schema.commandable_names:
            raise ValueError("read-only schema must not mark joints commandable")
        if not isinstance(robot_version, str) or not robot_version.strip():
            raise ValueError("explicit robot_version is required")
        if type(max_state_age_ms) is not int or max_state_age_ms <= 0:
            raise ValueError("max_state_age_ms must be a positive integer")
        if ik_solver is not None and getattr(ik_solver, "schema", None) is not schema:
            raise ValueError("Kuavo adapter and IK solver must use the same schema instance")
        super().__init__(ik_solver)
        self._schema = schema
        self._version = robot_version
        self._max_age_ns = max_state_age_ms * 1_000_000
        self._lock = Lock()
        self._state: RobotState | None = None
        self._last_receive_ns = -1
        self._stopped = False

    def get_capabilities(self) -> RobotCapabilities:
        return RobotCapabilities(joint_state_read=True)

    def get_joint_schema(self) -> JointSchema:
        return self._schema

    @property
    def robot_version(self) -> str:
        return self._version

    def invalidate_state(self) -> None:
        """Discard unusable input without resetting the receive-time watermark."""
        with self._lock:
            self._state = None

    def accept_sdk_joint_state(self, data: SDKJointState, *,
                               joint_names: Iterable[str], robot_version: str,
                               received_monotonic_ns: int) -> None:
        """Ingest one NEW SDK-shaped observation in radians, copying its positions.

        The receive callback must supply the actual local receive timestamp
        and audited SDK metadata. Never refresh a cached frame by polling it.
        Rejected input invalidates the cache; a later valid frame recovers it.
        """
        with self._lock:
            if self._stopped:
                raise RuntimeError("Kuavo read-only adapter is stopped")
            try:
                stamp = _timestamp(received_monotonic_ns)
                if stamp <= self._last_receive_ns:
                    raise ValueError("receive timestamp must increase")
                if robot_version != self._version:
                    raise ValueError("robot version does not match configured model")
                names = _names(joint_names)
                if set(names) != set(self._schema.names):
                    raise ValueError("SDK joint names do not match model schema")
                values = tuple(data.position)
                if len(values) != len(names):
                    raise ValueError("SDK position count does not match joint names")
                if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v)
                       for v in values):
                    raise ValueError("SDK positions must be finite numeric radians")
                positions = dict(zip(names, map(float, values)))
                self._state = RobotState(
                    {name: positions[name] for name in self._schema.names}, stamp)
                self._last_receive_ns = stamp
            except (ValueError, TypeError, AttributeError, OverflowError) as exc:
                self._state = None
                raise ValueError(f"invalid Kuavo feedback: {exc}") from exc

    def read_state(self, *, now_ns: int | None = None) -> RobotState:
        now = _timestamp(time.monotonic_ns() if now_ns is None else now_ns)
        with self._lock:
            if self._stopped or self._state is None:
                raise KuavoStateUnavailable("no received Kuavo joint state or adapter stopped")
            age = now - self._state.timestamp_ns
            if age < 0 or age > self._max_age_ns:
                raise KuavoStateUnavailable("Kuavo joint state is stale or timestamp is in future")
            return self._state

    def send_joint_command(self, command: ValidatedJointCommand, *,
                           now_ns: int | None = None) -> None:
        raise UnsupportedCapabilityError("Kuavo5Adapter is read-only; motion is unavailable")

    def stop(self) -> None:
        """Stop local observation only. This is NOT a physical emergency stop."""
        with self._lock:
            self._stopped = True
            self._state = None
