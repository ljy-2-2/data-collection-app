"""Recording gate node — conditionally block/allow recording.

Evaluates a set of configurable conditions (e-stop state, diagnostics
error rate, topic heartbeat, TF availability) and publishes a latched
``UInt8`` on ``~/gate_level``.  The published value is the maximum
escalation level among failing conditions:

- **0** — all conditions pass, recording allowed
- **1** — cannot start new recordings (block only)
- **2** — ongoing recording must be stopped (hard-stop)

Fail-closed: ``gate_level`` starts at **2** and only drops to **0**
once every enabled condition has received a healthy message and the
settle period has elapsed.
"""

import importlib
import time
from collections import deque
from dataclasses import dataclass

import rclpy
import rclpy.duration
import rclpy.time
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from rosidl_runtime_py.utilities import get_message

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from std_msgs.msg import UInt8
from std_srvs.srv import Trigger
from galaxis_core.camera_quality import CameraCadence
from galaxis_core.pico_timing import PicoTiming

from galaxis_core.recording_gate import (
    EscalationLevel,
    ConditionResult,
    ConditionChecker,
    GroupState,
    _deep_merge,  # noqa: F401 — re-exported for tests
    load_gate_config,
    evaluate_gate,
    compile_condition,
    extract_msg_refs,
)


# ---------------------------------------------------------------------------
# Condition checkers (ROS-coupled via subscriptions)
# ---------------------------------------------------------------------------


class DiagnosticsErrorRateChecker(ConditionChecker):
    """Subscribes to ``diagnostic_msgs/DiagnosticArray``.

    Counts ERROR-level statuses in a sliding time window.
    Passes when the count is strictly less than ``max_errors``.
    """

    def setup(self, node: Node) -> None:
        self._node = node
        self._max_errors = self.config.get("max_errors", 3)
        self._window_sec = self.config.get("window_sec", 30.0)
        self._name_filter = self.config.get("name_filter", "")
        self._errors: deque[float] = deque()
        self._last_stamp: float | None = None
        self._topic = self.config.get("topic", "/diagnostics_agg")
        node.get_shared_subscription(self._topic, DiagnosticArray, self._cb)

    def invalidate(self) -> None:
        self._errors.clear()
        self._last_stamp = None
        self._node.get_shared_subscription(self._topic, DiagnosticArray, self._cb)

    def _cb(self, msg: DiagnosticArray) -> None:
        now = time.monotonic()
        self._last_stamp = now
        for status in msg.status:
            if status.level >= DiagnosticStatus.ERROR:
                if self._name_filter and self._name_filter not in status.name:
                    continue
                self._errors.append(now)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._window_sec
        while self._errors and self._errors[0] < cutoff:
            self._errors.popleft()

    def evaluate(self) -> ConditionResult:
        if self._last_stamp is None:
            return self._no_message_result()
        self._prune()
        count = len(self._errors)
        if count >= self._max_errors:
            return ConditionResult(
                self.name,
                False,
                f"{count} errors in last {self._window_sec}s (max {self._max_errors})",
                escalation=self.escalation,
            )
        return ConditionResult(
            self.name,
            True,
            f"{count} errors (< {self._max_errors})",
            escalation=0,
        )


class TopicConditionChecker(ConditionChecker):
    """All-in-one topic checker: presence, freshness, rate, content, and single-shot.

    Discovers message type at runtime,
    then evaluates multiple health facets with per-facet escalation
    overrides.
    """

    def setup(self, node: Node) -> None:
        self._topic = self.config["topic"]
        self._timeout = self.config.get("timeout_sec", 5.0)
        self._expected_type = self.config.get("expected_type", "")
        self._node = node
        self._subscribed = False
        self._first_absent: float | None = None
        self._last_stamp: float | None = None
        self._last_msg = None

        # Absence
        self._absence_timeout = self.config.get("absence_timeout_sec", 0.0)
        self._absence_escalation = self.config.get(
            "absence_escalation", self.escalation
        )

        # Rate
        self._min_rate: float | None = self.config.get("min_rate_hz")
        self._rate_window = self.config.get("rate_window_sec", 5.0)
        self._rate_escalation = self.config.get("rate_escalation", self.escalation)
        self._msg_times: deque[float] = deque()
        self._camera_cadence = (CameraCadence(self._min_rate or 29.0,
                                self.config['max_frame_gap_sec'], self._rate_window,
                                self.config.get('low_rate_hold_sec', 0.0))
                                if 'max_frame_gap_sec' in self.config else None)

        self._pico_timing = PicoTiming() if self.config.get("pico_timing") else None

        # Single-shot: require one message, then always pass
        self._single_shot = self.config.get("single_shot", False)

        # Latch: use transient-local QoS (defaults to True for single_shot)
        self._latch = self.config.get("latch", self._single_shot)

        # Content expression
        self._condition_code = None
        self._condition_expr = ""
        self._condition_ref_codes: list[tuple[str, object]] = []
        self._reason_template = self.config.get("reason", "")
        condition_expr = self.config.get("condition")
        if condition_expr:
            self._condition_code = compile_condition(condition_expr)
            self._condition_expr = condition_expr
            refs = extract_msg_refs(condition_expr)
            self._condition_ref_codes = [
                (ref, compile(ref, "<reason>", "eval")) for ref in refs
            ]

    def _format_reason(self, msg) -> str:
        """Build a human-readable failure reason with actual message values."""
        parts = []
        for ref_str, ref_code in self._condition_ref_codes:
            try:
                val = eval(ref_code, {"__builtins__": {}}, {"msg": msg})  # noqa: S307
                parts.append(f"{ref_str}={val!r}")
            except Exception:
                parts.append(f"{ref_str}=?")
        detail = ", ".join(parts)

        if self._reason_template:
            try:
                return self._reason_template.format(
                    msg=msg,
                    condition=self._condition_expr,
                    name=self.name,
                    detail=detail,
                )
            except Exception:
                pass  # fall through to default

        if detail:
            return f"condition failed: {self._condition_expr} ({detail})"
        return f"condition failed: {self._condition_expr}"

    def invalidate(self) -> None:
        self._subscribed = False
        self._last_stamp = None
        self._last_msg = None
        self._msg_times.clear()
        if self._camera_cadence:
            self._camera_cadence.samples.clear()
            self._camera_cadence.fault = None
            self._camera_cadence.low_since = None
        if self._pico_timing:
            self._pico_timing = PicoTiming()
        self._first_absent = None

    def _try_subscribe(self) -> None:
        if self._subscribed:
            return
        topic_types = self._node.get_topic_names_and_types()
        for name, types in topic_types:
            if name == self._topic and types:
                type_str = types[0]
                if self._expected_type and type_str != self._expected_type:
                    self._node.get_logger().warning(
                        f"[{self.name}] Topic '{self._topic}' type mismatch: "
                        f"expected '{self._expected_type}', got '{type_str}'"
                    )
                    return
                try:
                    msg_class = get_message(type_str)
                except (ModuleNotFoundError, AttributeError):
                    self._node.get_logger().warning(
                        f"[{self.name}] Cannot import type '{type_str}' "
                        f"for '{self._topic}' — staying fail-closed"
                    )
                    return
                qos = 10
                if self.config.get('best_effort'):
                    qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)
                if self._latch:
                    qos = QoSProfile(
                        depth=1,
                        durability=DurabilityPolicy.TRANSIENT_LOCAL,
                        reliability=ReliabilityPolicy.RELIABLE,
                    )
                self._node.get_shared_subscription(
                    self._topic, msg_class, self._cb, qos
                )
                self._subscribed = True
                return

    def _cb(self, msg) -> None:
        now = time.monotonic()
        self._last_stamp = now
        self._last_msg = msg
        self._msg_times.append(now)
        if self._pico_timing:
            self._pico_timing.push(now, msg.device_time_ns, msg.pc_monotonic_ns, msg.seq, msg.pico_id)
        if self._camera_cadence:
            stamp = msg.header.stamp
            self._camera_cadence.push(now, stamp.sec*1_000_000_000+stamp.nanosec)

    def evaluate(self) -> ConditionResult:
        role = self.config.get('camera_selection_role')
        if role:
            import json
            from pathlib import Path
            try:
                selected = json.loads(Path('/opt/camera-selection/roles.json').read_text()).get(role, {})
            except (OSError, ValueError):
                selected = {}
            identity = tuple(selected.get(k) for k in ('vendor','product','serial','usb_path'))
            if identity != getattr(self, '_selection_identity', identity):
                self._selection_changed_at = time.monotonic()
            self._selection_identity = identity
            if time.monotonic()-getattr(self, '_selection_changed_at', -1e9) < 5.0:
                return ConditionResult(self.name, False, '正在连接所选相机并验证画面', escalation=1)
        result = self._evaluate()
        if self.config.get('diagnostic_only', False):
            result.escalation = 0
            result.settle_exempt = True
        return result

    def _evaluate(self) -> ConditionResult:
        self._try_subscribe()
        now = time.monotonic()

        # Inactive-safe mode: timeout_sec < 0 means no-message / not
        # advertised / stale all return PASS.  Used for topics that only
        # exist during recording (e.g. elapsed-time counters).
        inactive_safe = self._timeout < 0

        # Single-shot conditions are settle-exempt: they pass/fail
        # independently and never block or reset the settle timer.
        se = self._single_shot

        # 1. Absence — topic not advertised
        if not self._subscribed:
            if inactive_safe:
                return ConditionResult(
                    self.name, True, "ok (inactive)", escalation=0, settle_exempt=se
                )
            if self._first_absent is None:
                self._first_absent = now
            absent_dur = now - self._first_absent
            if absent_dur >= self._absence_timeout:
                return ConditionResult(
                    self.name,
                    False,
                    f"topic '{self._topic}' not advertised ({absent_dur:.1f}s)",
                    escalation=self._absence_escalation,
                    settle_exempt=se,
                )
            return ConditionResult(
                self.name,
                False,
                f"topic '{self._topic}' not yet advertised ({absent_dur:.1f}s)",
                escalation=self._absence_escalation,
                settle_exempt=se,
            )
        self._first_absent = None

        # 2. No message received
        if self._last_stamp is None:
            if inactive_safe:
                return ConditionResult(
                    self.name, True, "ok (inactive)", escalation=0, settle_exempt=se
                )
            r = self._no_message_result()
            r.settle_exempt = se
            return r

        # 2b. Single-shot: once any message received, skip freshness and rate.
        #     Content expression is still evaluated if configured.
        if self._single_shot:
            if self._condition_code is not None:
                try:
                    result = eval(  # noqa: S307
                        self._condition_code,
                        {"__builtins__": {}},
                        {"msg": self._last_msg},
                    )
                except Exception as exc:
                    return ConditionResult(
                        self.name,
                        False,
                        f"condition error: {exc}",
                        escalation=self.escalation,
                        settle_exempt=True,
                    )
                if not result:
                    return ConditionResult(
                        self.name,
                        False,
                        self._format_reason(self._last_msg),
                        escalation=self.escalation,
                        settle_exempt=True,
                    )
            return ConditionResult(
                self.name, True, "ok", escalation=0, settle_exempt=True
            )

        # 3. Freshness (skipped in inactive-safe mode)
        if not inactive_safe:
            age = now - self._last_stamp
            if age > self._timeout:
                return self._timeout_result(age, self._timeout)

        if self._pico_timing:
            failure = self._pico_timing.failure(now)
            if failure:
                # Block a new round only; never pause a recording on transient jitter.
                return ConditionResult(self.name, False, failure, escalation=1)

        # 4. Content expression
        if self._condition_code is not None:
            try:
                result = eval(  # noqa: S307
                    self._condition_code,
                    {"__builtins__": {}},
                    {"msg": self._last_msg},
                )
            except Exception as exc:
                return ConditionResult(
                    self.name,
                    False,
                    f"condition error: {exc}",
                    escalation=self.escalation,
                )
            if not result:
                return ConditionResult(
                    self.name,
                    False,
                    self._format_reason(self._last_msg),
                    escalation=self.escalation,
                )

        # 5. Rate
        if self._camera_cadence:
            failure = self._camera_cadence.failure(now)
            if failure:
                return ConditionResult(self.name, False, failure, escalation=self._rate_escalation)
        if self._min_rate is not None and self._camera_cadence is None:
            cutoff = now - self._rate_window
            while self._msg_times and self._msg_times[0] < cutoff:
                self._msg_times.popleft()
            rate = len(self._msg_times) / self._rate_window
            if rate < self._min_rate:
                return ConditionResult(
                    self.name,
                    False,
                    f"rate {rate:.1f} Hz < {self._min_rate} Hz",
                    escalation=self._rate_escalation,
                )

        return ConditionResult(self.name, True, "ok", escalation=0)


class TfAvailabilityChecker(ConditionChecker):
    """Checks whether specific TF transforms are available.

    Passes when every configured frame pair can be looked up.
    Optionally verifies that the transform is within ``max_age_sec``.

    Config example::

        frames:
          - source: "odom"
            target: "base_link"
            max_age_sec: -1.0   # -1 = no age check
          - source: "base_link"
            target: "hand_left"
            max_age_sec: 1.0
        timeout_sec: 5.0        # lookup timeout
    """

    def setup(self, node: Node) -> None:
        self._frames = self.config.get("frames", [])
        self._timeout = self.config.get("timeout_sec", 5.0)
        self._node = node
        self._setup_done = False
        self._tf_buffer = None

    def _ensure_tf(self) -> bool:
        """Get shared TF buffer from gate node (tf2_ros may not be available)."""
        if self._setup_done:
            return self._tf_buffer is not None
        self._setup_done = True
        self._tf_buffer = self._node.get_shared_tf_buffer()
        if self._tf_buffer is None:
            self._node.get_logger().error(
                f"tf2_ros not available — condition '{self.name}' will always fail"
            )
        return self._tf_buffer is not None

    def evaluate(self) -> ConditionResult:
        if not self._ensure_tf():
            return ConditionResult(
                self.name,
                False,
                "tf2_ros not available",
                escalation=self.escalation,
            )

        for frame in self._frames:
            source = frame.get("source", "")
            target = frame.get("target", "")
            max_age = frame.get("max_age_sec", -1.0)
            # can_transform returns immediately — no blocking
            if not self._tf_buffer.can_transform(target, source, rclpy.time.Time()):
                return ConditionResult(
                    self.name,
                    False,
                    f"{source}->{target} not available",
                    escalation=self.escalation,
                )
            if max_age > 0:
                try:
                    t = self._tf_buffer.lookup_transform(
                        target,
                        source,
                        rclpy.time.Time(),
                        timeout=rclpy.duration.Duration(seconds=0.0),
                    )
                except Exception as exc:
                    return ConditionResult(
                        self.name,
                        False,
                        f"{source}->{target} lookup failed: {exc}",
                        escalation=self.escalation,
                    )
                now = self._node.get_clock().now()
                stamp = rclpy.time.Time.from_msg(t.header.stamp)
                age_sec = (now - stamp).nanoseconds / 1e9
                if age_sec > max_age:
                    return ConditionResult(
                        self.name,
                        False,
                        f"{source}->{target} too old ({age_sec:.1f}s > {max_age}s)",
                        escalation=self.escalation,
                    )
        return ConditionResult(self.name, True, "ok", escalation=0)


# ---------------------------------------------------------------------------
# Condition type registry
# ---------------------------------------------------------------------------

CONDITION_TYPES: dict[str, type[ConditionChecker]] = {
    "diagnostics_error_rate": DiagnosticsErrorRateChecker,
    "topic_condition": TopicConditionChecker,
    "tf_availability": TfAvailabilityChecker,
}


# ---------------------------------------------------------------------------
# Debounce wrapper
# ---------------------------------------------------------------------------


class DebouncedChecker:
    """Wraps a ConditionChecker to require stable passing for debounce_sec."""

    def __init__(self, checker: ConditionChecker, debounce_sec: float):
        self._checker = checker
        self.name = checker.name
        self.escalation = checker.escalation
        self.config = checker.config
        self._debounce_sec = debounce_sec
        self._passing_since: float | None = None

    def __getattr__(self, name):
        return getattr(self._checker, name)

    def setup(self, node) -> None:
        self._checker.setup(node)

    def invalidate(self) -> None:
        self._checker.invalidate()
        self._passing_since = None

    def evaluate(self) -> ConditionResult:
        result = self._checker.evaluate()
        if self._debounce_sec <= 0:
            return result
        now = time.monotonic()
        if result.passed:
            if self._passing_since is None:
                self._passing_since = now
            elapsed = now - self._passing_since
            if elapsed < self._debounce_sec:
                return ConditionResult(
                    self.name,
                    False,
                    f"debouncing ({elapsed:.1f}s / {self._debounce_sec}s)",
                    escalation=self.escalation,
                )
            return result
        self._passing_since = None
        return result


# ---------------------------------------------------------------------------
# Shared subscription pool
# ---------------------------------------------------------------------------


class _SubscriptionHandle:
    """Fans out a single ROS subscription to multiple callbacks."""

    def __init__(self):
        self.callbacks: list = []

    def add(self, callback) -> None:
        self.callbacks.append(callback)

    def dispatch(self, msg) -> None:
        for cb in self.callbacks:
            cb(msg)


@dataclass
class _PoolEntry:
    """A pooled ROS subscription with fan-out handle."""

    topic: str
    msg_type_name: str
    handle: _SubscriptionHandle
    subscription: object = None
    latched: bool = False

    @property
    def key(self) -> str:
        return f"{self.topic}:{self.msg_type_name}:{'latched' if self.latched else 'default'}"


# ---------------------------------------------------------------------------
# ROS 2 Node
# ---------------------------------------------------------------------------


class RecordingGateNode(Node):
    def __init__(self):
        super().__init__("recording_gate")

        self.declare_parameter("recording_gate_config", "")

        config_path = self.get_parameter("recording_gate_config").value

        if not config_path:
            self.get_logger().warning(
                "No recording_gate_config provided — gate allows recording by default"
            )
            self._cfg: dict = {"eval_rate": 2.0}
        else:
            self._cfg = load_gate_config(config_path)

        self._eval_rate: float = self._cfg.get("eval_rate", 2.0)

        # Latched publisher
        latched_qos = QoSProfile(depth=1)
        latched_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        latched_qos.reliability = ReliabilityPolicy.RELIABLE
        self._pub = self.create_publisher(UInt8, "~/gate_level", latched_qos)
        self._diag_pub = self.create_publisher(DiagnosticArray, "/diagnostics", 10)

        self._gate_level: int = EscalationLevel.HARD_STOP
        self._groups: list[GroupState] = []
        self._checkers: list[ConditionChecker] = []

        # Shared resource pool — deduplicates subscriptions across checkers
        self._sub_pool: dict[str, _PoolEntry] = {}
        self._tf_buffer = None
        self._tf_listener = None

        self._setup_checkers()

        if not self._checkers:
            self._gate_level = EscalationLevel.OK
            self.get_logger().info(
                "No conditions enabled — recording allowed by default"
            )

        self._publish()

        period = 1.0 / self._eval_rate if self._eval_rate > 0 else 1.0
        self.create_timer(period, self._evaluate)

        self.create_service(Trigger, "~/invalidate", self._handle_invalidate)

        total = len(self._checkers)
        groups = len(self._groups)
        self.get_logger().info(
            f"RecordingGateNode started: {total} conditions in {groups} group(s), "
            f"eval_rate={self._eval_rate}Hz"
        )

    def _handle_invalidate(self, request, response):
        """Destroy all subscriptions and reset checkers (soft reboot)."""
        for entry in self._sub_pool.values():
            entry.handle.callbacks.clear()
            self.destroy_subscription(entry.subscription)
        self._sub_pool.clear()

        if self._tf_buffer is not None:
            self._tf_listener = None
            self._tf_buffer = None

        for group in self._groups:
            for checker in group.checkers:
                checker.invalidate()

        self.get_logger().info(
            "Subscriptions invalidated — re-subscribing on next eval"
        )
        response.success = True
        response.message = "Subscriptions invalidated"
        return response

    def get_shared_subscription(self, topic: str, msg_type, callback, qos=10):
        """Return a deduplicated subscription, fanning out to multiple callbacks."""
        latched = not isinstance(qos, int)
        entry = _PoolEntry(
            topic=topic,
            msg_type_name=msg_type.__name__,
            handle=_SubscriptionHandle(),
            latched=latched,
        )
        existing = self._sub_pool.get(entry.key)
        if existing is not None:
            existing.handle.add(callback)
            return
        entry.subscription = self.create_subscription(
            msg_type, topic, entry.handle.dispatch, qos
        )
        entry.handle.add(callback)
        self._sub_pool[entry.key] = entry

    def get_shared_tf_buffer(self):
        """Return a shared tf2_ros.Buffer, creating it on first call."""
        if self._tf_buffer is None:
            try:
                import tf2_ros

                self._tf_buffer = tf2_ros.Buffer()
                self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)
            except ImportError:
                self.get_logger().error(
                    "tf2_ros not available — TF conditions will always fail"
                )
        return self._tf_buffer

    @staticmethod
    def _resolve_checker_class(ctype: str) -> type[ConditionChecker] | None:
        """Look up checker class by name or import path."""
        if ctype in CONDITION_TYPES:
            return CONDITION_TYPES[ctype]
        if ":" in ctype:
            module_path, class_name = ctype.rsplit(":", 1)
            try:
                mod = importlib.import_module(module_path)
                return getattr(mod, class_name)
            except (ImportError, AttributeError):
                return None
        return None

    def _setup_checkers(self) -> None:
        cfg = self._cfg

        # Global defaults (support both old and new naming)
        global_settle = cfg.get("settle_sec", cfg.get("settle_timeout_sec", 30.0))
        global_recovery = cfg.get("recovery_sec", cfg.get("re_settle_timeout_sec", 5.0))
        global_escalation = cfg.get("default_escalation", 2)
        global_latch = cfg.get("latch", False)

        # Detect grouped vs flat config
        groups_cfg = cfg.get("groups", {})
        if not groups_cfg and "conditions" in cfg:
            # Backward compat: wrap flat conditions in a default group
            groups_cfg = {"default": {"conditions": cfg["conditions"]}}

        now = time.monotonic()
        for group_name, group_cfg in groups_cfg.items():
            if not group_cfg.get("enabled", True):
                continue

            settle = group_cfg.get("settle_sec", global_settle)
            recovery = group_cfg.get("recovery_sec", global_recovery)
            default_type = group_cfg.get("default_type", "")
            default_escalation = group_cfg.get("default_escalation", global_escalation)
            default_debounce = group_cfg.get("default_debounce_sec", 0.0)

            # In old flat format, enabled defaults to False;
            # in new grouped format, enabled defaults to True.
            is_legacy = group_name == "default" and "groups" not in cfg
            enabled_default = False if is_legacy else True

            checkers = []
            conditions = group_cfg.get("conditions", {})
            for name, cond_cfg in conditions.items():
                if not cond_cfg.get("enabled", enabled_default):
                    continue
                ctype = cond_cfg.get("type", default_type)

                cls = self._resolve_checker_class(ctype)
                if cls is None:
                    self.get_logger().error(
                        f"Unknown condition type '{ctype}' for "
                        f"'{group_name}/{name}' — skipped"
                    )
                    continue

                # Apply group defaults without mutating original config
                effective_cfg = {
                    **cond_cfg,
                    "escalation": cond_cfg.get("escalation", default_escalation),
                    "latch": cond_cfg.get("latch", global_latch),
                }
                checker = cls(name, effective_cfg)
                if checker.escalation == EscalationLevel.OK:
                    self.get_logger().warning(
                        f"Condition '{group_name}/{name}' has escalation=0 (OK) — "
                        f"failures will not block or stop recording"
                    )
                checker.setup(self)

                debounce = cond_cfg.get("debounce_sec", default_debounce)
                if debounce > 0:
                    checker = DebouncedChecker(checker, debounce)

                checkers.append(checker)
                self.get_logger().info(
                    f"[{group_name}] Condition '{name}' ({ctype}) "
                    f"enabled on {cond_cfg.get('topic', '?')}"
                )

            # Warn about duplicate topics within the same group
            topics = [
                c.config.get("topic", "") for c in checkers if hasattr(c, "config")
            ]
            dupes = [t for t in set(topics) if topics.count(t) > 1 and t]
            if dupes:
                self.get_logger().warning(
                    f"[{group_name}] Duplicate topic(s) in same group: {dupes}. "
                    f"Consider merging conditions or using separate groups."
                )

            if checkers:
                group = GroupState(
                    name=group_name,
                    checkers=checkers,
                    settle_sec=settle,
                    recovery_sec=recovery,
                    settle_start_time=now,
                )
                self._groups.append(group)
                self._checkers.extend(checkers)

    def _evaluate(self) -> None:
        if not self._groups:
            return

        now = time.monotonic()
        prev_gate_level = self._gate_level

        for group in self._groups:
            was_settled = group.settled
            (
                group.level,
                group.settled,
                group.settle_is_initial,
                group.settle_start_time,
                group.failing,
                group.results,
            ) = evaluate_gate(
                group.checkers,
                group.level,
                group.settled,
                group.settle_is_initial,
                group.settle_start_time,
                group.settle_sec,
                group.recovery_sec,
                now,
            )

            # Log per-group settle transitions
            if not was_settled and group.settled:
                elapsed = now - group.settle_start_time
                if not group.failing:
                    self.get_logger().info(
                        f"[{group.name}] Settled after {elapsed:.1f}s "
                        f"— all conditions passing"
                    )
                else:
                    timeout = (
                        group.settle_sec
                        if group.settle_is_initial
                        else group.recovery_sec
                    )
                    reasons = ", ".join(f"{r.name}: {r.reason}" for r in group.failing)
                    self.get_logger().warning(
                        f"[{group.name}] Settle timeout ({timeout}s) reached "
                        f"after {elapsed:.1f}s with failing conditions: {reasons}"
                    )

        new_level = max(g.level for g in self._groups)

        # Log overall level transitions
        if new_level == EscalationLevel.OK and prev_gate_level != EscalationLevel.OK:
            self.get_logger().info("All conditions passed — recording allowed")
        elif new_level != EscalationLevel.OK and prev_gate_level == EscalationLevel.OK:
            all_failing = [r for g in self._groups for r in g.failing]
            reasons = ", ".join(f"{r.name}: {r.reason}" for r in all_failing)
            self.get_logger().warning(
                f"Condition(s) failed — recording blocked (level {new_level}): {reasons}"
            )

        self._gate_level = new_level
        self._publish()
        self._publish_diagnostics(now)

    def _publish(self) -> None:
        msg = UInt8()
        msg.data = int(self._gate_level)
        self._pub.publish(msg)

    @staticmethod
    def _escalation_to_diag_level(escalation: int, passed: bool) -> int:
        if passed:
            return DiagnosticStatus.OK
        if escalation <= 0:
            return DiagnosticStatus.WARN
        if escalation == 1:
            return DiagnosticStatus.WARN
        return DiagnosticStatus.ERROR

    def _publish_diagnostics(self, now: float) -> None:
        statuses = []
        for group in self._groups:
            # Per-condition entries (from cached results)
            for r in group.results:
                level = self._escalation_to_diag_level(r.escalation, r.passed)

                status = DiagnosticStatus()
                status.name = f"recording_gate/{group.name}/{r.name}"
                status.level = level
                status.message = r.reason
                status.hardware_id = "recording_gate"
                status.values = [
                    KeyValue(key="escalation", value=str(r.escalation)),
                    KeyValue(key="group", value=group.name),
                ]
                statuses.append(status)

            # Per-group summary
            group_status = DiagnosticStatus()
            group_status.name = f"recording_gate/{group.name}"
            group_status.level = self._escalation_to_diag_level(
                group.level, group.level == EscalationLevel.OK
            )
            if group.settled:
                group_status.message = (
                    "ok" if not group.failing else "settled with failures"
                )
            else:
                remaining = max(
                    0.0,
                    (
                        group.settle_sec
                        if group.settle_is_initial
                        else group.recovery_sec
                    )
                    - (now - group.settle_start_time),
                )
                group_status.message = f"settling ({remaining:.1f}s remaining)"
            group_status.hardware_id = "recording_gate"
            group_status.values = [
                KeyValue(key="settled", value=str(group.settled)),
                KeyValue(key="level", value=str(int(group.level))),
            ]
            statuses.append(group_status)

        msg = DiagnosticArray()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.status = statuses
        self._diag_pub.publish(msg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    from galaxis_core.sentry_setup import init_sentry

    init_sentry()
    rclpy.init()
    node = RecordingGateNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
