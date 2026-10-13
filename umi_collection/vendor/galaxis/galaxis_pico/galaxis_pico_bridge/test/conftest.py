"""Shared fixtures for galaxis_pico_bridge unit tests.

Mocks the ROS 2 / message layers at ``sys.modules`` level so ``galaxis_pico_bridge``
modules can be imported without a live ROS 2 installation. Mirrors the pattern
used in ``galaxis-core/galaxis-core/test/conftest.py``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Ensure the package root (containing ``galaxis_pico_bridge/``) is on sys.path so
# that ``import galaxis_pico_bridge.*`` works without pip-installing the package.
_PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)


# ---------------------------------------------------------------------------
# FakeNode -- lightweight stand-in for rclpy.node.Node
# ---------------------------------------------------------------------------


class FakeParameter:
    def __init__(self, value):
        self.value = value


class FakeNode:
    """Minimal replacement for ``rclpy.node.Node``.

    Stores parameters declared via ``declare_parameter`` and returns them from
    ``get_parameter``. Every other ROS helper (publisher, timer, ...) returns a
    ``MagicMock`` so constructor code runs without side effects.
    """

    def __init__(self, name="fake_node"):
        self._name = name
        self._params: dict = {}
        self._logger = MagicMock()
        # Per-publisher captures: topic -> list of published messages.
        self.published: dict = {}
        self._publishers_by_topic: dict = {}

    def declare_parameter(self, name, default=None):
        self._params[name] = FakeParameter(default)
        return self._params[name]

    def get_parameter(self, name):
        return self._params[name]

    def get_logger(self):
        return self._logger

    def get_clock(self):
        clock = MagicMock()
        now_mock = MagicMock()
        now_mock.to_msg.return_value = _FakeStamp(0, 0)
        now_mock.nanoseconds = 0
        clock.now.return_value = now_mock
        return clock

    def create_subscription(self, *a, **kw):
        return MagicMock()

    def create_timer(self, *a, **kw):
        return MagicMock()

    def create_publisher(self, msg_type, topic, qos):
        captured: list = []
        self.published.setdefault(topic, captured)

        pub = MagicMock()

        def _publish(msg):
            captured.append(msg)

        pub.publish.side_effect = _publish
        self._publishers_by_topic[topic] = pub
        return pub

    def create_service(self, *a, **kw):
        return MagicMock()

    def create_client(self, *a, **kw):
        client = MagicMock()
        client.wait_for_service = MagicMock(return_value=True)
        return client

    def destroy_node(self):
        pass


class _FakeStamp:
    def __init__(self, sec=0, nanosec=0):
        self.sec = sec
        self.nanosec = nanosec


# ---------------------------------------------------------------------------
# Helpers to build msg classes that accept arbitrary attribute assignment
# (matches rclpy generated message classes' behavior closely enough for unit
# testing field assignments).
# ---------------------------------------------------------------------------


def _make_struct_class(name, fields):
    """Build a tiny class that stores any of ``fields`` plus arbitrary attrs."""

    def _init(self):
        for f in fields:
            setattr(self, f, _DEFAULTS.get(f, 0.0))

    cls = type(name, (), {"__init__": _init})
    return cls


_DEFAULTS = {
    # Most numeric defaults are 0.0; bools default to False.
    "x": 0.0,
    "y": 0.0,
    "z": 0.0,
    "w": 1.0,
}


# ---------------------------------------------------------------------------
# mock_ros -- inject fake ROS 2 / message modules before importing the bridge
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_ros(monkeypatch):
    """Replace ROS 2 and message packages with lightweight fakes.

    Must be requested *before* importing any ``galaxis_pico_bridge`` module
    that depends on ROS 2.
    """
    # ----- rclpy ------------------------------------------------------------
    rclpy_mod = types.ModuleType("rclpy")
    rclpy_mod.ok = MagicMock(return_value=True)
    rclpy_mod.init = MagicMock()
    rclpy_mod.spin = MagicMock()
    rclpy_mod.shutdown = MagicMock()

    rclpy_node = types.ModuleType("rclpy.node")
    rclpy_node.Node = FakeNode

    rclpy_qos = types.ModuleType("rclpy.qos")
    rclpy_qos.QoSProfile = MagicMock
    rclpy_qos.ReliabilityPolicy = MagicMock()
    rclpy_qos.HistoryPolicy = MagicMock()

    rclpy_time = types.ModuleType("rclpy.time")
    rclpy_time.Time = MagicMock

    rclpy_duration = types.ModuleType("rclpy.duration")
    rclpy_duration.Duration = MagicMock

    for mod_name, mod in [
        ("rclpy", rclpy_mod),
        ("rclpy.node", rclpy_node),
        ("rclpy.qos", rclpy_qos),
        ("rclpy.time", rclpy_time),
        ("rclpy.duration", rclpy_duration),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mod)

    # ----- std_msgs ---------------------------------------------------------
    std_msgs = types.ModuleType("std_msgs")
    std_msgs_msg = types.ModuleType("std_msgs.msg")

    class _Bool:
        def __init__(self, data=False):
            self.data = data

    class _Float32:
        def __init__(self, data=0.0):
            self.data = data

    class _Header:
        def __init__(self):
            self.stamp = None
            self.frame_id = ""

    std_msgs_msg.Bool = _Bool
    std_msgs_msg.Float32 = _Float32
    std_msgs_msg.Header = _Header
    monkeypatch.setitem(sys.modules, "std_msgs", std_msgs)
    monkeypatch.setitem(sys.modules, "std_msgs.msg", std_msgs_msg)

    # ----- sensor_msgs ------------------------------------------------------
    sensor_msgs = types.ModuleType("sensor_msgs")
    sensor_msgs_msg = types.ModuleType("sensor_msgs.msg")

    class _Joy:
        def __init__(self):
            self.header = _Header()
            self.axes = []
            self.buttons = []

    class _BatteryState:
        POWER_SUPPLY_HEALTH_UNKNOWN = 0
        POWER_SUPPLY_TECHNOLOGY_UNKNOWN = 0
        POWER_SUPPLY_STATUS_UNKNOWN = 0
        POWER_SUPPLY_STATUS_CHARGING = 1
        POWER_SUPPLY_STATUS_DISCHARGING = 2

        def __init__(self):
            self.header = _Header()
            self.location = ""
            self.voltage = 0.0
            self.temperature = 0.0
            self.current = 0.0
            self.charge = 0.0
            self.capacity = 0.0
            self.design_capacity = 0.0
            self.percentage = 0.0
            self.present = False
            self.power_supply_status = 0
            self.power_supply_health = 0
            self.power_supply_technology = 0

    sensor_msgs_msg.Joy = _Joy
    sensor_msgs_msg.BatteryState = _BatteryState
    monkeypatch.setitem(sys.modules, "sensor_msgs", sensor_msgs)
    monkeypatch.setitem(sys.modules, "sensor_msgs.msg", sensor_msgs_msg)

    # ----- diagnostic_msgs --------------------------------------------------
    diagnostic_msgs = types.ModuleType("diagnostic_msgs")
    diagnostic_msgs_msg = types.ModuleType("diagnostic_msgs.msg")

    class _DiagnosticStatus:
        OK = 0
        WARN = 1
        ERROR = 2
        STALE = 3

        def __init__(self):
            self.level = 0
            self.name = ""
            self.message = ""
            self.hardware_id = ""
            self.values = []

    class _KeyValue:
        def __init__(self, key="", value=""):
            self.key = key
            self.value = value

    class _DiagnosticArray:
        def __init__(self):
            self.header = _Header()
            self.status = []

    diagnostic_msgs_msg.DiagnosticStatus = _DiagnosticStatus
    diagnostic_msgs_msg.KeyValue = _KeyValue
    diagnostic_msgs_msg.DiagnosticArray = _DiagnosticArray
    monkeypatch.setitem(sys.modules, "diagnostic_msgs", diagnostic_msgs)
    monkeypatch.setitem(sys.modules, "diagnostic_msgs.msg", diagnostic_msgs_msg)

    # ----- geometry_msgs ----------------------------------------------------
    geometry_msgs = types.ModuleType("geometry_msgs")
    geometry_msgs_msg = types.ModuleType("geometry_msgs.msg")

    Vector3 = _make_struct_class("Vector3", ["x", "y", "z"])
    Quaternion = _make_struct_class("Quaternion", ["x", "y", "z", "w"])

    class _Transform:
        def __init__(self):
            self.translation = Vector3()
            self.rotation = Quaternion()

    class _TransformStamped:
        def __init__(self):
            self.header = _Header()
            self.child_frame_id = ""
            self.transform = _Transform()

    geometry_msgs_msg.Vector3 = Vector3
    geometry_msgs_msg.Quaternion = Quaternion
    geometry_msgs_msg.Transform = _Transform
    geometry_msgs_msg.TransformStamped = _TransformStamped
    monkeypatch.setitem(sys.modules, "geometry_msgs", geometry_msgs)
    monkeypatch.setitem(sys.modules, "geometry_msgs.msg", geometry_msgs_msg)

    # ----- tf2_ros ----------------------------------------------------------
    tf2_ros = types.ModuleType("tf2_ros")

    class _LookupException(Exception):
        pass

    class _ConnectivityException(Exception):
        pass

    class _ExtrapolationException(Exception):
        pass

    # Use callable factories rather than ``MagicMock`` (the class) so that
    # passing already-mocked objects (e.g. ``Buffer``) into ``TransformListener``
    # does not trigger the "Cannot spec a Mock object" guard added in newer
    # Pythons.
    tf2_ros.TransformBroadcaster = lambda *a, **kw: MagicMock()
    tf2_ros.Buffer = lambda *a, **kw: MagicMock()
    tf2_ros.TransformListener = lambda *a, **kw: MagicMock()
    tf2_ros.LookupException = _LookupException
    tf2_ros.ConnectivityException = _ConnectivityException
    tf2_ros.ExtrapolationException = _ExtrapolationException
    monkeypatch.setitem(sys.modules, "tf2_ros", tf2_ros)

    # ----- galaxis_pico_msgs -------------------------------------------------
    galaxis_pico_msgs = types.ModuleType("galaxis_pico_msgs")
    galaxis_pico_msgs_msg = types.ModuleType("galaxis_pico_msgs.msg")

    class _PicoHmd:
        def __init__(self):
            self.header = _Header()
            self.device_time_ns = 0
            self.pc_monotonic_ns = 0
            self.seq = 0
            self.pico_id = ""
            self.position = Vector3()
            self.rotation = Quaternion()

    class _PicoController:
        def __init__(self):
            self.header = _Header()
            self.device_time_ns = 0
            self.pc_monotonic_ns = 0
            self.seq = 0
            self.pico_id = ""
            self.position = Vector3()
            self.rotation = Quaternion()
            self.tracked = False
            self.valid = False

    galaxis_pico_msgs_msg.PicoHmd = _PicoHmd
    galaxis_pico_msgs_msg.PicoController = _PicoController
    monkeypatch.setitem(sys.modules, "galaxis_pico_msgs", galaxis_pico_msgs)
    monkeypatch.setitem(sys.modules, "galaxis_pico_msgs.msg", galaxis_pico_msgs_msg)

    # ----- flush cached galaxis_pico_bridge imports --------------------------
    to_remove = [k for k in sys.modules if k.startswith("galaxis_pico_bridge")]
    for key in to_remove:
        monkeypatch.delitem(sys.modules, key, raising=False)

    yield {
        "PicoHmd": _PicoHmd,
        "PicoController": _PicoController,
        "TransformStamped": _TransformStamped,
        "Joy": _Joy,
    }
