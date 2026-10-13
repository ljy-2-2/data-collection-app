"""Pure-Python gate evaluation logic (no ROS 2 dependency).

Contains the escalation model, condition base class, config loading,
and the pure ``evaluate_gate()`` function used by the ROS node.
"""

import ast
import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum

import yaml


# ---------------------------------------------------------------------------
# Escalation levels
# ---------------------------------------------------------------------------


class EscalationLevel(IntEnum):
    OK = 0  # condition passes
    BLOCK_START = 1  # cannot start new recording
    HARD_STOP = 2  # ongoing recording must be stopped


# ---------------------------------------------------------------------------
# Condition result
# ---------------------------------------------------------------------------


@dataclass
class ConditionResult:
    name: str
    passed: bool
    reason: str
    escalation: int = field(default=EscalationLevel.HARD_STOP)
    settle_exempt: bool = False


# ---------------------------------------------------------------------------
# Condition checker base class
# ---------------------------------------------------------------------------


class ConditionChecker(ABC):
    """Base class for a single gate condition."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.escalation = EscalationLevel(config.get("escalation", 2))

    @abstractmethod
    def setup(self, node) -> None:
        """Create subscriptions / state.  Called once after node init."""

    @abstractmethod
    def evaluate(self) -> ConditionResult:
        """Return the current pass/fail state."""

    def invalidate(self) -> None:
        """Reset subscription state for re-subscription. Called by invalidate service."""

    def _no_message_result(self) -> ConditionResult:
        return ConditionResult(
            self.name, False, "no message received yet", escalation=self.escalation
        )

    def _timeout_result(self, age: float, timeout: float) -> ConditionResult:
        return ConditionResult(
            self.name,
            False,
            f"timeout ({age:.1f}s > {timeout}s)",
            escalation=self.escalation,
        )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_gate_config(default_path: str, override_path: str = "") -> dict:
    """Load and merge gate configuration files.

    Returns the merged dict.  If *override_path* is empty or the file
    does not exist, only the default is returned.
    """
    with open(default_path, "r") as fh:
        base = yaml.safe_load(fh) or {}

    if override_path:
        try:
            with open(override_path, "r") as fh:
                override = yaml.safe_load(fh) or {}
            base = _deep_merge(base, override)
        except FileNotFoundError:
            pass

    return base


# ---------------------------------------------------------------------------
# Expression compiler
# ---------------------------------------------------------------------------


_SAFE_NODES = frozenset(
    {
        ast.Expression,
        ast.Compare,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.Not,
        ast.UnaryOp,
        ast.Attribute,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Eq,
        ast.NotEq,
        ast.In,
        ast.NotIn,
        ast.Is,
        ast.IsNot,
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.USub,
        ast.UAdd,
        ast.Subscript,
        ast.Slice,
        ast.List,
        ast.Tuple,
    }
)


def compile_condition(expr: str):
    """Validate and compile a condition expression.

    Only attribute access on ``msg``, comparisons, boolean ops,
    basic arithmetic, and subscripts are allowed.  Returns compiled
    bytecode for use with
    ``eval(code, {"__builtins__": {}}, {"msg": msg})``.

    Raises ``ValueError`` on unsafe or invalid expressions.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid expression syntax: {exc}") from exc

    for node in ast.walk(tree):
        if type(node) not in _SAFE_NODES:
            raise ValueError(f"Unsafe node type in expression: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"Private attribute access not allowed: {node.attr}")
        if isinstance(node, ast.Name) and node.id != "msg":
            raise ValueError(f"Unknown variable '{node.id}' (only 'msg' is allowed)")

    return compile(tree, "<condition>", "eval")


def _node_to_str(node: ast.AST) -> str | None:
    """Convert an AST node rooted at ``msg`` to its source string."""
    if isinstance(node, ast.Name) and node.id == "msg":
        return "msg"
    if isinstance(node, ast.Attribute):
        parent = _node_to_str(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    if isinstance(node, ast.Subscript):
        parent = _node_to_str(node.value)
        if parent and isinstance(node.slice, ast.Constant):
            return f"{parent}[{node.slice.value!r}]"
    return None


def extract_msg_refs(expr: str) -> list[str]:
    """Extract unique ``msg.*`` reference paths from a condition expression.

    Returns source strings like ``['msg.data', 'msg.pose.position.x']``.
    Only leaf references are returned (``msg.pose.position.x``, not
    ``msg.pose`` or ``msg``).
    """
    tree = ast.parse(expr, mode="eval")
    refs: list[str] = []
    seen: set[str] = set()

    for node in ast.walk(tree):
        # Only consider Attribute and Subscript nodes whose root is 'msg'
        if not isinstance(node, (ast.Attribute, ast.Subscript)):
            continue
        s = _node_to_str(node)
        if s and s != "msg" and s not in seen:
            seen.add(s)
            refs.append(s)

    # Remove prefixes: keep only leaf refs (e.g. drop msg.pose if msg.pose.position.x exists)
    leaf_refs = [
        r
        for r in refs
        if not any(
            other.startswith(r + ".") or other.startswith(r + "[")
            for other in refs
            if other != r
        )
    ]
    return leaf_refs


# ---------------------------------------------------------------------------
# Group state
# ---------------------------------------------------------------------------


@dataclass
class GroupState:
    """Per-group evaluation state for the recording gate."""

    name: str
    checkers: list[ConditionChecker]
    level: int = EscalationLevel.HARD_STOP
    settled: bool = False
    settle_is_initial: bool = True
    settle_start_time: float = 0.0
    settle_sec: float = 30.0
    recovery_sec: float = 5.0
    failing: list[ConditionResult] = field(default_factory=list)
    results: list[ConditionResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure evaluation function
# ---------------------------------------------------------------------------


def evaluate_gate(
    checkers: list,
    gate_level: int,
    settled: bool,
    settle_is_initial: bool,
    settle_start_time: float,
    settle_sec: float,
    recovery_sec: float,
    now: float,
) -> tuple[int, bool, bool, float, list[ConditionResult], list[ConditionResult]]:
    """Pure evaluation step.

    Returns ``(new_level, settled, settle_is_initial, settle_start_time,
    failing_reasons, all_results)``.
    """
    results = [c.evaluate() for c in checkers]
    failing = [r for r in results if not r.passed]
    all_pass = len(failing) == 0
    max_level = max((r.escalation for r in failing), default=0)

    # Settle-exempt results (e.g. single_shot) don't participate in settle
    # timing.  They independently pass/fail and affect the gate level but
    # never reset or block the settle timer.
    settle_failing = [r for r in failing if not r.settle_exempt]
    settle_all_pass = len(settle_failing) == 0

    # Re-settle: when gate was clear (level 0) and a settle-bound condition
    # just failed, require a fresh settle before re-enabling.
    if gate_level == EscalationLevel.OK and not settle_all_pass:
        settled = False
        settle_start_time = now
        settle_is_initial = False

    # Settle period: hold gate elevated until settle-bound conditions
    # pass continuously for the applicable timeout.
    # If every condition is settle-exempt, there is nothing to settle.
    has_settle_bound = any(not r.settle_exempt for r in results)
    if not settled:
        if not has_settle_bound:
            settled = True
        else:
            timeout = settle_sec if settle_is_initial else recovery_sec
            elapsed = now - settle_start_time
            if settle_all_pass and elapsed >= timeout:
                settled = True
            elif elapsed >= timeout and not settle_all_pass:
                settled = True

    # Derive new gate level
    if settled and all_pass:
        new_level = EscalationLevel.OK
    elif settled:
        new_level = max_level
    else:
        # During settle: at minimum block starts
        new_level = max(max_level, EscalationLevel.BLOCK_START)

    return new_level, settled, settle_is_initial, settle_start_time, failing, results
