"""Auditable operating envelope for the G2 upper-body IK mode.

The values in this module are project teleoperation limits, not vendor-rated
limits.  Full self-collision remains unavailable until the URDF collision mesh
package is present and can be loaded.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from types import MappingProxyType
import xml.etree.ElementTree as ET


_BODY_JOINTS = (
    "idx01_body_joint1",
    "idx02_body_joint2",
    "idx03_body_joint3",
    "idx04_body_joint4",
    "idx05_body_joint5",
)
_PITCH_JOINTS = _BODY_JOINTS[:3]


class UpperBodyEnvelopeError(ValueError):
    """A torso candidate does not move safely within/toward the session envelope."""


@dataclass(frozen=True)
class G2UpperBodySafetyProfile:
    """Project-selected normal operating envelope for upper-body teleoperation."""

    body_deviation_limits_rad: Mapping[str, float]
    combined_pitch_deviation_limit_rad: float
    arm_velocity_rad_s: float
    body_velocity_rad_s: float
    control_hz: float

    def __post_init__(self) -> None:
        limits = dict(self.body_deviation_limits_rad)
        if set(limits) != set(_BODY_JOINTS):
            raise ValueError("body deviation limits must define all five G2 body joints")
        values = tuple(limits.values()) + (
            self.combined_pitch_deviation_limit_rad,
            self.arm_velocity_rad_s,
            self.body_velocity_rad_s,
            self.control_hz,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            raise ValueError("upper-body safety profile values must be finite and positive")
        object.__setattr__(
            self,
            "body_deviation_limits_rad",
            MappingProxyType({name: float(limits[name]) for name in _BODY_JOINTS}),
        )

    @classmethod
    def balanced(cls) -> "G2UpperBodySafetyProfile":
        """Return the roomy normal-speed profile selected for offline validation."""

        return cls(
            body_deviation_limits_rad={
                "idx01_body_joint1": math.radians(15.0),
                "idx02_body_joint2": math.radians(20.0),
                "idx03_body_joint3": math.radians(20.0),
                "idx04_body_joint4": math.radians(15.0),
                "idx05_body_joint5": math.radians(35.0),
            },
            combined_pitch_deviation_limit_rad=math.radians(25.0),
            arm_velocity_rad_s=0.65,
            body_velocity_rad_s=0.25,
            control_hz=100.0,
        )

    def validate_body_candidate(
        self,
        anchor_positions: Mapping[str, float],
        candidate_positions: Mapping[str, float],
    ) -> None:
        """Reject candidates outside per-joint or combined torso pitch limits."""

        for label, positions in (
            ("anchor", anchor_positions),
            ("candidate", candidate_positions),
        ):
            missing = set(_BODY_JOINTS) - set(positions)
            if missing:
                raise ValueError(f"{label} is missing G2 body joints: {sorted(missing)}")
            for name in _BODY_JOINTS:
                value = positions[name]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValueError(f"{label} body joint {name} must be finite")

        deltas = {
            name: float(candidate_positions[name]) - float(anchor_positions[name])
            for name in _BODY_JOINTS
        }
        for name, delta in deltas.items():
            if abs(delta) > self.body_deviation_limits_rad[name] + 1e-12:
                raise UpperBodyEnvelopeError(
                    f"body joint {name} exceeds the session envelope"
                )
        combined_pitch = sum(deltas[name] for name in _PITCH_JOINTS)
        if abs(combined_pitch) > self.combined_pitch_deviation_limit_rad + 1e-12:
            raise UpperBodyEnvelopeError(
                "combined pitch exceeds the upper-body session envelope"
            )

    def body_envelope_violation(
        self,
        anchor_positions: Mapping[str, float],
        candidate_positions: Mapping[str, float],
    ) -> float:
        """Return the largest torso-envelope excess in radians, or zero."""
        deltas = {
            name: float(candidate_positions[name]) - float(anchor_positions[name])
            for name in _BODY_JOINTS
        }
        return max(self._body_envelope_excesses(deltas).values())

    def _body_envelope_excesses(self, deltas: Mapping[str, float]) -> dict[str, float]:
        excesses = {
            name: max(
                0.0,
                abs(deltas[name]) - self.body_deviation_limits_rad[name],
            )
            for name in _BODY_JOINTS
        }
        excesses["combined_pitch"] = max(
            0.0,
            abs(sum(deltas[name] for name in _PITCH_JOINTS))
            - self.combined_pitch_deviation_limit_rad,
        )
        return excesses

    def validate_body_transition(
        self,
        anchor_positions: Mapping[str, float],
        observed_positions: Mapping[str, float],
        candidate_positions: Mapping[str, float],
    ) -> None:
        """Allow an out-of-envelope measured posture only while recovering inward."""
        try:
            self.validate_body_candidate(anchor_positions, candidate_positions)
            return
        except UpperBodyEnvelopeError:
            observed_deltas = {
                name: float(observed_positions[name]) - float(anchor_positions[name])
                for name in _BODY_JOINTS
            }
            candidate_deltas = {
                name: float(candidate_positions[name]) - float(anchor_positions[name])
                for name in _BODY_JOINTS
            }
            observed = self._body_envelope_excesses(observed_deltas)
            candidate = self._body_envelope_excesses(candidate_deltas)
            no_constraint_worsened = all(
                candidate[name] <= observed[name] + 1e-12 for name in observed
            )
            at_least_one_improved = any(
                candidate[name] < observed[name] - 1e-12 for name in observed
            )
            if no_constraint_worsened and at_least_one_improved:
                return
            raise


@dataclass(frozen=True)
class G2UpperBodyCollisionAssetAudit:
    """Evidence that URDF collision declarations have resolvable mesh files."""

    collision_elements: int
    collision_meshes: int
    missing_meshes: int
    first_missing_uri: str
    full_self_collision_ready: bool

    @classmethod
    def inspect(
        cls,
        urdf_path: str | Path,
        *,
        package_roots: Sequence[str | Path] = (),
    ) -> "G2UpperBodyCollisionAssetAudit":
        path = Path(urdf_path)
        if not path.is_file():
            raise FileNotFoundError(f"G2 URDF not found: {path}")
        root = ET.parse(path).getroot()
        collisions = root.findall(".//collision")
        mesh_uris = [
            mesh.attrib.get("filename", "")
            for collision in collisions
            for mesh in collision.findall("./geometry/mesh")
        ]
        roots = tuple(Path(item) for item in package_roots)
        missing = []
        for uri in mesh_uris:
            candidates: tuple[Path, ...]
            if uri.startswith("package://"):
                package_relative = uri[len("package://") :]
                candidates = tuple(base / package_relative for base in roots)
            else:
                mesh_path = Path(uri)
                candidates = (
                    (path.parent / mesh_path) if not mesh_path.is_absolute() else mesh_path,
                )
            if not candidates or not any(candidate.is_file() for candidate in candidates):
                missing.append(uri)
        return cls(
            collision_elements=len(collisions),
            collision_meshes=len(mesh_uris),
            missing_meshes=len(missing),
            first_missing_uri=missing[0] if missing else "",
            # Path resolution is necessary but never sufficient. Readiness also
            # requires geometry loading, explicit collision-pair policy, and a
            # successful collision sweep, none of which this asset audit claims.
            full_self_collision_ready=False,
        )
