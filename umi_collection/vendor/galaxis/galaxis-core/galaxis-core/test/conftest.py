"""Shared fixtures for unit tests.

Mocks the ROS2 / message layers at sys.modules level so that
``galaxis_core`` modules can be imported without a live
ROS2 installation.
"""

import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: tests requiring live external services"
    )


# Ensure the package root (containing galaxis_core/) is on sys.path
# so that ``import galaxis_core`` works without pip-installing.
_PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)


# ---------------------------------------------------------------------------
# Fake schema for jsonschema / airoa_metadata mocks
# ---------------------------------------------------------------------------

_FAKE_SCHEMA = {
    "$defs": {
        "File": {"type": "object"},
        "Robot": {"type": "object"},
        "Environment": {"type": "object"},
        "Runner": {"type": "object"},
        "Device": {"type": "object"},
        "Program": {"type": "object"},
        "Episode": {"type": "object"},
        "Segment": {"type": "object"},
    },
    "properties": {
        "devices": {"type": "array", "items": {"$ref": "#/$defs/Device"}},
        "programs": {"type": "array", "items": {"$ref": "#/$defs/Program"}},
    },
}

# ---------------------------------------------------------------------------
# FakeNode – lightweight stand-in for rclpy.node.Node
# ---------------------------------------------------------------------------


class FakeParameterValue:
    """Stand-in for ``rclpy.Parameter.Value`` (returned by ``get_parameter_value()``)."""

    def __init__(self, value):
        self._value = value

    @property
    def string_value(self):
        return str(self._value) if self._value is not None else ""

    @property
    def double_value(self):
        return float(self._value) if self._value is not None else 0.0

    @property
    def integer_value(self):
        return int(self._value) if self._value is not None else 0


class FakeParameter:
    def __init__(self, value):
        self.value = value

    def get_parameter_value(self):
        return FakeParameterValue(self.value)


class FakeNode:
    """Minimal replacement for ``rclpy.node.Node``.

    Stores parameters declared via ``declare_parameter`` and returns them
    from ``get_parameter``.  Every other ROS helper (publisher, timer, …)
    returns a MagicMock so constructor code runs without side-effects.
    """

    def __init__(self, name="fake_node"):
        self._name = name
        self._params: dict[str, FakeParameter] = {}
        self._logger = MagicMock()

    # -- parameter helpers --------------------------------------------------
    def declare_parameter(self, name, default=None):
        self._params[name] = FakeParameter(default)
        return self._params[name]

    def get_parameter(self, name):
        return self._params[name]

    # -- logging ------------------------------------------------------------
    def get_logger(self):
        return self._logger

    # -- clock --------------------------------------------------------------
    def get_clock(self):
        clock = MagicMock()
        now_mock = MagicMock()
        now_mock.nanoseconds = 0
        now_mock.seconds_nanoseconds.return_value = (0, 0)
        clock.now.return_value = now_mock
        return clock

    def set_parameters(self, params):
        for p in params:
            self._params[p.name] = FakeParameter(p.value)

    # -- ROS2 entity creation (all no-ops) ----------------------------------
    def create_subscription(self, *a, **kw):
        return MagicMock()

    def create_timer(self, *a, **kw):
        return MagicMock()

    def create_publisher(self, *a, **kw):
        return MagicMock()

    def create_service(self, *a, **kw):
        return MagicMock()

    def create_client(self, *a, **kw):
        client = MagicMock()
        client.wait_for_service = MagicMock(return_value=True)
        return client

    def destroy_node(self):
        pass


# ---------------------------------------------------------------------------
# mock_rclpy – inject fake ROS2 / message modules before importing nodes
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_rclpy(monkeypatch):
    """Replace ROS2 and message packages with lightweight fakes.

    Must be requested *before* importing any ``galaxis_core``
    module that depends on ROS2.
    """

    # -- rclpy --------------------------------------------------------------
    rclpy_mod = types.ModuleType("rclpy")
    rclpy_mod.ok = MagicMock(return_value=True)
    rclpy_mod.init = MagicMock()
    rclpy_mod.spin = MagicMock()
    rclpy_mod.try_shutdown = MagicMock()
    rclpy_mod.Future = MagicMock

    class _FakeRclpyParameter:
        def __init__(self, name="", value=None):
            self.name = name
            self.value = value

    rclpy_mod.Parameter = _FakeRclpyParameter

    rclpy_node = types.ModuleType("rclpy.node")
    rclpy_node.Node = FakeNode

    rclpy_qos = types.ModuleType("rclpy.qos")
    rclpy_qos.QoSProfile = MagicMock
    rclpy_qos.ReliabilityPolicy = MagicMock()
    rclpy_qos.DurabilityPolicy = MagicMock()
    rclpy_qos.QoSHistoryPolicy = MagicMock()
    rclpy_qos.QoSReliabilityPolicy = MagicMock()

    rclpy_cb = types.ModuleType("rclpy.callback_groups")
    rclpy_cb.ReentrantCallbackGroup = MagicMock

    rclpy_exec = types.ModuleType("rclpy.executors")
    rclpy_exec.MultiThreadedExecutor = MagicMock

    rclpy_time = types.ModuleType("rclpy.time")
    rclpy_time.Time = MagicMock

    rclpy_duration = types.ModuleType("rclpy.duration")
    rclpy_duration.Duration = MagicMock

    for mod_name, mod in [
        ("rclpy", rclpy_mod),
        ("rclpy.node", rclpy_node),
        ("rclpy.qos", rclpy_qos),
        ("rclpy.callback_groups", rclpy_cb),
        ("rclpy.executors", rclpy_exec),
        ("rclpy.time", rclpy_time),
        ("rclpy.duration", rclpy_duration),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mod)

    # -- std_msgs -----------------------------------------------------------
    std_msgs = types.ModuleType("std_msgs")
    std_msgs_msg = types.ModuleType("std_msgs.msg")

    class _String:
        def __init__(self, data=""):
            self.data = data

    class _Bool:
        def __init__(self):
            self.data = False

    class _Int64:
        def __init__(self):
            self.data = 0

    class _UInt8:
        def __init__(self):
            self.data = 0

    class _Float64:
        def __init__(self):
            self.data = 0.0

    std_msgs_msg.String = _String
    std_msgs_msg.Bool = _Bool
    std_msgs_msg.Int64 = _Int64
    std_msgs_msg.UInt8 = _UInt8
    std_msgs_msg.Float64 = _Float64
    monkeypatch.setitem(sys.modules, "std_msgs", std_msgs)
    monkeypatch.setitem(sys.modules, "std_msgs.msg", std_msgs_msg)

    # -- std_srvs -----------------------------------------------------------
    std_srvs = types.ModuleType("std_srvs")
    std_srvs_srv = types.ModuleType("std_srvs.srv")

    trigger_req = type("Request", (), {})
    trigger_resp = type("Response", (), {"success": False, "message": ""})
    trigger = MagicMock()
    trigger.Request = trigger_req
    trigger.Response = trigger_resp
    std_srvs_srv.Trigger = trigger

    monkeypatch.setitem(sys.modules, "std_srvs", std_srvs)
    monkeypatch.setitem(sys.modules, "std_srvs.srv", std_srvs_srv)

    # -- airoa_data_msgs ----------------------------------------------------
    airoa_data_msgs = types.ModuleType("airoa_data_msgs")
    airoa_data_msgs_srv = types.ModuleType("airoa_data_msgs.srv")

    class _STRequest:
        def __init__(self, **kwargs):
            self.message = kwargs.get("message", "")

    class _STResponse:
        def __init__(self, **kwargs):
            self.success = kwargs.get("success", False)
            self.message = kwargs.get("message", "")

    st_req = _STRequest
    st_resp = _STResponse
    string_trigger = MagicMock()
    string_trigger.Request = st_req
    string_trigger.Response = st_resp
    airoa_data_msgs_srv.StringTrigger = string_trigger

    monkeypatch.setitem(sys.modules, "airoa_data_msgs", airoa_data_msgs)
    monkeypatch.setitem(sys.modules, "airoa_data_msgs.srv", airoa_data_msgs_srv)

    # -- diagnostic_msgs ----------------------------------------------------
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

    class _Header:
        def __init__(self):
            self.stamp = None

    class _DiagnosticArray:
        def __init__(self):
            self.header = _Header()
            self.status = []

    diagnostic_msgs_msg.DiagnosticArray = _DiagnosticArray
    diagnostic_msgs_msg.DiagnosticStatus = _DiagnosticStatus
    diagnostic_msgs_msg.KeyValue = _KeyValue
    monkeypatch.setitem(sys.modules, "diagnostic_msgs", diagnostic_msgs)
    monkeypatch.setitem(sys.modules, "diagnostic_msgs.msg", diagnostic_msgs_msg)

    # -- rosgraph_msgs ------------------------------------------------------
    rosgraph_msgs = types.ModuleType("rosgraph_msgs")
    rosgraph_msgs_msg = types.ModuleType("rosgraph_msgs.msg")
    rosgraph_msgs_msg.Clock = MagicMock
    monkeypatch.setitem(sys.modules, "rosgraph_msgs", rosgraph_msgs)
    monkeypatch.setitem(sys.modules, "rosgraph_msgs.msg", rosgraph_msgs_msg)

    # -- sensor_msgs --------------------------------------------------------
    sensor_msgs = types.ModuleType("sensor_msgs")
    sensor_msgs_msg = types.ModuleType("sensor_msgs.msg")

    class _Joy:
        def __init__(self):
            self.buttons = []
            self.axes = []

    class _BatteryState:
        POWER_SUPPLY_STATUS_CHARGING = 1

        def __init__(self):
            self.percentage = 0.0
            self.power_supply_status = 0

    sensor_msgs_msg.Joy = _Joy
    sensor_msgs_msg.BatteryState = _BatteryState
    monkeypatch.setitem(sys.modules, "sensor_msgs", sensor_msgs)
    monkeypatch.setitem(sys.modules, "sensor_msgs.msg", sensor_msgs_msg)

    # -- rosidl_runtime_py --------------------------------------------------
    rosidl_mod = types.ModuleType("rosidl_runtime_py")
    rosidl_util = types.ModuleType("rosidl_runtime_py.utilities")

    def _fake_get_message(type_str):
        """Return mock class from already-mocked message modules."""
        parts = type_str.split("/")
        mod = sys.modules.get(f"{parts[0]}.{parts[1]}")
        if mod:
            return getattr(mod, parts[2], MagicMock)
        return MagicMock

    rosidl_util.get_message = _fake_get_message
    monkeypatch.setitem(sys.modules, "rosidl_runtime_py", rosidl_mod)
    monkeypatch.setitem(sys.modules, "rosidl_runtime_py.utilities", rosidl_util)

    # -- jsonschema ---------------------------------------------------------
    jsonschema_mod = types.ModuleType("jsonschema")
    jsonschema_exc = types.ModuleType("jsonschema.exceptions")

    class _ValidationError(Exception):
        def __init__(self, message="", *args, **kwargs):
            self.message = message
            super().__init__(message)

    class _FakeDraft7Validator:
        def __init__(self, schema, resolver=None):
            self.schema = schema

        def validate(self, instance):
            pass  # no-op by default

    class _FakeRefResolver:
        def __init__(self, *a, **kw):
            pass

        @classmethod
        def from_schema(cls, schema):
            return cls()

    jsonschema_mod.Draft7Validator = _FakeDraft7Validator
    jsonschema_mod.RefResolver = _FakeRefResolver
    jsonschema_mod.exceptions = jsonschema_exc
    jsonschema_exc.ValidationError = _ValidationError
    monkeypatch.setitem(sys.modules, "jsonschema", jsonschema_mod)
    monkeypatch.setitem(sys.modules, "jsonschema.exceptions", jsonschema_exc)

    # -- airoa_metadata (only mock load_schema; real dataclasses are used) ----
    import airoa_metadata
    import airoa_metadata.schemas
    import airoa_metadata.versions
    import airoa_metadata.versions.v2_0

    airoa_metadata.schemas.load_schema = MagicMock(return_value=_FAKE_SCHEMA)

    # -- flush cached galaxis_core imports -------------------------
    to_remove = [k for k in sys.modules if k.startswith("galaxis_core")]
    for key in to_remove:
        monkeypatch.delitem(sys.modules, key, raising=False)

    yield rclpy_mod


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_record_dir(tmp_path):
    """Return an empty temp directory to use as ``record_base_dir``."""
    d = tmp_path / "recordings"
    d.mkdir()
    return d


@pytest.fixture()
def sample_recording(tmp_record_dir):
    """Create a sample recording directory with meta.json + a .mcap file."""
    rec = tmp_record_dir / "rec_001"
    rec.mkdir()
    (rec / "meta.json").write_text('{"task": "test"}')
    (rec / "data_0.mcap").write_bytes(b"\x00" * 128)
    return rec


@pytest.fixture()
def uploaded_recording(sample_recording):
    """Return the sample recording (upload state is now tracked in the DB,
    not via a local marker file)."""
    return sample_recording


# ---------------------------------------------------------------------------
# Mock S3 (minio) client
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_s3_client():
    """Return a MagicMock mimicking ``minio.Minio``."""
    client = MagicMock()

    upload_result = MagicMock()
    upload_result.etag = "abc123"
    client.fput_object.return_value = upload_result

    stat_result = MagicMock()
    stat_result.etag = "abc123"
    client.stat_object.return_value = stat_result

    client.put_object.return_value = MagicMock()

    return client


def make_test_gate(gate_module, checkers, *, settle_sec=0.0, recovery_sec=0.0):
    """Build a RecordingGateNode-like object with manual checkers for testing."""
    node = MagicMock()
    node.get_logger.return_value = MagicMock()

    group = gate_module.GroupState(
        name="test",
        checkers=checkers,
        settle_sec=settle_sec,
        recovery_sec=recovery_sec,
        settle_start_time=time.monotonic(),
    )

    gate = gate_module.RecordingGateNode.__new__(gate_module.RecordingGateNode)
    gate._groups = [group]
    gate._checkers = checkers
    gate._gate_level = gate_module.EscalationLevel.HARD_STOP
    gate._pub = MagicMock()
    gate._diag_pub = MagicMock()
    gate.get_logger = node.get_logger
    gate.get_clock = MagicMock()
    return gate
