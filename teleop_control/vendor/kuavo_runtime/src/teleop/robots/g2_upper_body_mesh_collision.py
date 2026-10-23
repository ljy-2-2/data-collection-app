"""Offline full-mesh posture and sampled-path checks for the G2 upper body.

This module deliberately has no GDK imports and no command-sending surface.  It
resolves two audited local geometry bundles, computes link poses with Pinocchio,
and asks Coal for distances between explicitly reviewed collision pairs.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol
import xml.etree.ElementTree as ET

import numpy as np

from teleop.robots.g2 import make_g2_joint_schema
from teleop.robots.g2_omnipicker_geometry import G2OmniPickerGeometryAudit


_BODY_BUNDLE = "geniesim-09ed792f-candidate-20260910"
_TOOL_BUNDLE = "geniesim-09ed792f-omnipicker-20260910"
_GRIPPER_OPEN_RAD = -0.785
_GRIPPER_CLOSED_RAD = 0.0
_DEFAULT_MARGIN_M = 0.005
_MOUNT_EXCLUSIONS = frozenset(
    {
        ("arm_l_link7", "gripper_l_base_link"),
        ("arm_l_link7", "gripper_l_camera_link"),
        ("arm_r_link7", "gripper_r_base_link"),
        ("arm_r_link7", "gripper_r_camera_link"),
    }
)
_AUDITED_DOCUMENT_HASHES = {
    f"{_BODY_BUNDLE}/source-manifest.json": "e180ec86c1bf15dd91219f81e431e5215765a5b2ae03db4f1d633ef1f931f0e4",
    f"{_BODY_BUNDLE}/G2.urdf": "0edf202353363752c2e6b3eef1f270c87aeb4b1f4e3a76f8a00dc0b5daeed5be",
    f"{_BODY_BUNDLE}/G2.srdf": "61db8bb29caaa7f74c221bcba664e2e8ced223a4faaa2ff6ce93099ac2a32cf8",
    f"{_TOOL_BUNDLE}/source-mesh-map.json": "451c4bf2df241d89a14c443c84d28ce9ab26ccf7253060340a143c7f70d8b169",
    f"{_TOOL_BUNDLE}/G2_omnipicker_fixed_dual.urdf": "eb782de0b6f1faf1fcddfb5e2f7004a39e350c4f297ca51a56003ce8f9ad8319",
    f"{_TOOL_BUNDLE}/evidence/g2-calibration-20260810-194410-619745883.json": "55d75fa76a4207a9d839abc49acd471fd8fd7e9e6ca4b182f71d07260b320102",
    f"{_TOOL_BUNDLE}/evidence/g2-calibration-20260810-194730-041110452.json": "81d39847040b31ed9aa89cfdab6cd9c8b26d13679884a9c9a2291d82ab6d9f9e",
}


@dataclass(frozen=True)
class G2CollisionInventory:
    geometry_instances: int
    mesh_instances: int
    body_mesh_instances: int
    gripper_mesh_instances: int
    primitive_instances: int
    checked_pairs: int
    unresolved_meshes: tuple[str, ...] = ()


@dataclass(frozen=True)
class G2CollisionViolation:
    first_geometry: str
    second_geometry: str
    first_link: str
    second_link: str
    distance_m: float


@dataclass(frozen=True)
class G2ConfigurationCollisionResult:
    checked_pairs: int
    minimum_distance_m: float
    violations: tuple[G2CollisionViolation, ...]
    margin_m: float
    upper_body_live_ready: bool = False
    physical_robot_command_sent: bool = False

    @property
    def clear(self) -> bool:
        return self.checked_pairs > 0 and not self.violations


@dataclass(frozen=True)
class G2PathCollisionResult:
    sample_count: int
    checked_pairs_per_sample: int
    minimum_distance_m: float
    first_violation_fraction: float | None
    first_violation: G2CollisionViolation | None
    upper_body_live_ready: bool = False
    physical_robot_command_sent: bool = False

    @property
    def clear(self) -> bool:
        return self.sample_count > 0 and self.first_violation is None


@dataclass(frozen=True)
class G2RecordedFKResult:
    frames_checked: int
    max_translation_error_m: float
    max_rotation_error_rad: float
    matches_evidence: bool
    upper_body_live_ready: bool = False
    physical_robot_command_sent: bool = False


class G2UpperBodyCollisionModel(Protocol):
    """Small read-only interface used by the per-frame collision gate."""

    def check_configuration(
        self,
        joint_positions: Mapping[str, float],
        *,
        gripper_positions: Mapping[str, float] | None = None,
        margin_m: float = _DEFAULT_MARGIN_M,
    ) -> G2ConfigurationCollisionResult: ...

    def check_path(
        self,
        start_positions: Mapping[str, float],
        end_positions: Mapping[str, float],
        *,
        start_grippers: Mapping[str, float] | None = None,
        end_grippers: Mapping[str, float] | None = None,
        max_step_rad: float = 0.01,
        max_gripper_step_rad: float | None = None,
        margin_m: float = _DEFAULT_MARGIN_M,
    ) -> G2PathCollisionResult: ...


@dataclass(frozen=True)
class G2UpperBodyCollisionGateResult:
    clear: bool
    phase: Literal["clear", "posture", "path"]
    posture: G2ConfigurationCollisionResult
    path: G2PathCollisionResult | None
    upper_body_live_ready: bool = False
    physical_robot_command_sent: bool = False

    @property
    def minimum_distance_m(self) -> float:
        if self.path is None:
            return self.posture.minimum_distance_m
        return min(self.posture.minimum_distance_m, self.path.minimum_distance_m)

    @property
    def first_violation(self) -> G2CollisionViolation | None:
        if self.posture.violations:
            return self.posture.violations[0]
        if self.path is not None:
            return self.path.first_violation
        return None

    @property
    def path_sample_count(self) -> int:
        return 0 if self.path is None else self.path.sample_count


class G2UpperBodyCollisionGate:
    """Check a candidate and the path from the last collision-clear candidate."""

    def __init__(
        self,
        model: G2UpperBodyCollisionModel,
        *,
        margin_m: float = _DEFAULT_MARGIN_M,
        max_step_rad: float = 0.01,
        max_gripper_step_rad: float = 0.10,
    ) -> None:
        if (
            isinstance(margin_m, bool)
            or not isinstance(margin_m, (int, float))
            or not math.isfinite(margin_m)
            or margin_m < 0.0
        ):
            raise ValueError("margin_m must be finite and nonnegative")
        if (
            isinstance(max_step_rad, bool)
            or not isinstance(max_step_rad, (int, float))
            or not math.isfinite(max_step_rad)
            or max_step_rad <= 0.0
        ):
            raise ValueError("max_step_rad must be finite and positive")
        if (
            isinstance(max_gripper_step_rad, bool)
            or not isinstance(max_gripper_step_rad, (int, float))
            or not math.isfinite(max_gripper_step_rad)
            or max_gripper_step_rad <= 0.0
        ):
            raise ValueError("max_gripper_step_rad must be finite and positive")
        self._model = model
        self.margin_m = float(margin_m)
        self.max_step_rad = float(max_step_rad)
        self.max_gripper_step_rad = float(max_gripper_step_rad)
        self._schema_names = frozenset(make_g2_joint_schema().names)
        self._previous_positions: Mapping[str, float] | None = None
        self._previous_grippers: Mapping[str, float] | None = None

    def reset(self) -> None:
        self._previous_positions = None
        self._previous_grippers = None

    @staticmethod
    def _gripper_positions(fractions: Mapping[str, float]) -> dict[str, float]:
        if set(fractions) != {"left", "right"}:
            raise ValueError("gripper_fractions must define left and right")
        positions: dict[str, float] = {}
        for side in ("left", "right"):
            fraction = fractions[side]
            if (
                isinstance(fraction, bool)
                or not isinstance(fraction, (int, float))
                or not math.isfinite(fraction)
                or not 0.0 <= fraction <= 1.0
            ):
                raise ValueError(f"{side} gripper fraction must be within [0, 1]")
            positions[side] = _GRIPPER_OPEN_RAD + float(fraction) * (
                _GRIPPER_CLOSED_RAD - _GRIPPER_OPEN_RAD
            )
        return positions

    def evaluate(
        self,
        *,
        observed_positions: Mapping[str, float],
        candidate_targets: Mapping[str, float],
        gripper_fractions: Mapping[str, float],
    ) -> G2UpperBodyCollisionGateResult:
        if set(observed_positions) != self._schema_names:
            raise ValueError("observed_positions must contain exactly the 22 G2 joints")
        unknown = set(candidate_targets) - self._schema_names
        if unknown:
            raise ValueError(
                f"candidate_targets contain unknown G2 joints: {sorted(unknown)}"
            )
        candidate = {name: observed_positions[name] for name in self._schema_names}
        candidate.update(candidate_targets)
        grippers = self._gripper_positions(gripper_fractions)
        posture = self._model.check_configuration(
            candidate,
            gripper_positions=grippers,
            margin_m=self.margin_m,
        )
        if not posture.clear:
            return G2UpperBodyCollisionGateResult(False, "posture", posture, None)

        start_positions = self._previous_positions or observed_positions
        start_grippers = self._previous_grippers or grippers
        path = self._model.check_path(
            start_positions,
            candidate,
            start_grippers=start_grippers,
            end_grippers=grippers,
            max_step_rad=self.max_step_rad,
            max_gripper_step_rad=self.max_gripper_step_rad,
            margin_m=self.margin_m,
        )
        if not path.clear:
            return G2UpperBodyCollisionGateResult(False, "path", posture, path)

        self._previous_positions = MappingProxyType(dict(candidate))
        self._previous_grippers = MappingProxyType(dict(grippers))
        return G2UpperBodyCollisionGateResult(True, "clear", posture, path)


def _runtime_root(runtime_root: str | Path | None) -> Path:
    if runtime_root is not None:
        return Path(runtime_root).resolve()
    return Path(__file__).resolve().parents[3]


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read audited geometry metadata {path}: {exc}") from exc


def _verify_audited_documents(runtime: Path) -> None:
    geometry_root = runtime / "artifacts/g2-geometry"
    for relative, expected in _AUDITED_DOCUMENT_HASHES.items():
        path = geometry_root / relative
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError(f"missing audited geometry document: {relative}") from exc
        if actual != expected:
            raise ValueError(f"audited geometry document changed: {relative}")


def _verified_files(root: Path, records: object, *, hash_key: str, size_key: str) -> dict[str, Path]:
    if not isinstance(records, list):
        raise ValueError(f"invalid geometry manifest in {root}")
    resolved: dict[str, Path] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"invalid geometry record in {root}")
        filename = record.get("filename", record.get("cache_file"))
        expected_hash = record.get(hash_key)
        expected_size = record.get(size_key)
        if not isinstance(filename, str) or not isinstance(expected_hash, str) or type(expected_size) is not int:
            raise ValueError(f"incomplete geometry record in {root}")
        path = root / filename
        if not path.is_file() and (root / "meshes" / filename).is_file():
            path = root / "meshes" / filename
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"missing audited mesh {filename}") from exc
        if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ValueError(f"audited mesh changed: {filename}")
        if filename in resolved:
            raise ValueError(f"duplicate mesh filename in geometry manifests: {filename}")
        resolved[filename] = path.resolve()
    return resolved


def _resolved_urdf(runtime: Path) -> tuple[str, set[str], set[str]]:
    _verify_audited_documents(runtime)
    body = runtime / "artifacts/g2-geometry" / _BODY_BUNDLE
    tool = runtime / "artifacts/g2-geometry" / _TOOL_BUNDLE
    body_manifest = _load_json(body / "source-manifest.json")
    tool_manifest = _load_json(tool / "source-mesh-map.json")
    if not isinstance(body_manifest, dict) or not isinstance(tool_manifest, dict):
        raise ValueError("invalid G2 geometry bundle manifests")
    body_files = _verified_files(
        body,
        body_manifest.get("files"),
        hash_key="oid",
        size_key="size",
    )
    tool_records = tool_manifest.get("meshes")
    if not isinstance(tool_records, list):
        raise ValueError("invalid OmniPicker source mesh map")
    tool_records = [
        record for record in tool_records
        if isinstance(record, dict)
        and isinstance(record.get("link"), str)
        and record["link"].startswith("gripper_r_")
    ]
    tool_files = _verified_files(
        tool,
        tool_records,
        hash_key="lfs_oid",
        size_key="bytes",
    )
    tool_uri_files = {
        str(record["urdf_uri"]): tool_files[str(record["cache_file"])]
        for record in tool_records
    }
    if len(body_files) != 25 or len(tool_files) != 10:
        raise ValueError("expected 25 body meshes and 10 unique OmniPicker meshes")
    audit = G2OmniPickerGeometryAudit.inspect(tool)
    if not audit.gripper_geometry_ready:
        raise ValueError(f"OmniPicker evidence audit failed: {audit.issues}")

    urdf = tool / "G2_omnipicker_fixed_dual.urdf"
    try:
        root = ET.parse(urdf).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"cannot parse audited dual OmniPicker URDF: {exc}") from exc

    # Runtime TF evidence proves these two fixed offsets on both sides.  The
    # copied reference URDF differs by 1.0 mm at the base and 7.9 mm at center.
    for joint_name in ("idx51_ee_l_joint", "idx91_ee_r_joint"):
        joint = root.find(f"./joint[@name='{joint_name}']/origin")
        if joint is None:
            raise ValueError(f"reference URDF is missing {joint_name}")
        joint.set("xyz", "0 0 0.0051")
    for joint_name in ("idx52_gripper_l_center_joint", "idx92_gripper_r_center_joint"):
        joint = root.find(f"./joint[@name='{joint_name}']/origin")
        if joint is None:
            raise ValueError(f"reference URDF is missing {joint_name}")
        joint.set("xyz", "0 0 0.13798")

    body_used: set[str] = set()
    tool_used: set[str] = set()
    unresolved: list[str] = []
    for mesh in root.findall(".//collision/geometry/mesh"):
        original = mesh.get("filename", "")
        filename = Path(original).name
        if filename in body_files:
            mesh.set("filename", str(body_files[filename]))
            body_used.add(filename)
        elif original in tool_uri_files:
            resolved = tool_uri_files[original]
            mesh.set("filename", str(resolved))
            tool_used.add(resolved.name)
        else:
            unresolved.append(original)
    if unresolved:
        raise ValueError(f"unresolved collision meshes: {sorted(unresolved)}")
    if body_used != set(body_files) or tool_used != set(tool_files):
        raise ValueError("the resolved URDF does not use every audited mesh")
    return ET.tostring(root, encoding="unicode"), body_used, tool_used


def _srdf_exclusions(runtime: Path) -> set[tuple[str, str]]:
    path = runtime / "artifacts/g2-geometry" / _BODY_BUNDLE / "G2.srdf"
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"cannot parse audited G2 SRDF: {exc}") from exc
    result: set[tuple[str, str]] = set()
    for item in root.findall("disable_collisions"):
        # `Default` means collision-free only in one sampled default posture;
        # it is intentionally not trusted as a general exclusion.
        if item.get("reason") not in {"Adjacent", "Always"}:
            continue
        first, second = item.get("link1"), item.get("link2")
        if first and second:
            result.add(tuple(sorted((first, second))))
    return result


def load_recorded_g2_snapshot(runtime_root: str | Path | None = None) -> Mapping[str, float]:
    runtime = _runtime_root(runtime_root)
    evidence = runtime / "artifacts/g2-geometry" / _TOOL_BUNDLE / "evidence"
    snapshots = sorted(evidence.glob("g2-calibration-*.json"))
    if len(snapshots) != 2:
        raise ValueError("expected exactly two recorded G2 calibration snapshots")
    document = _load_json(snapshots[0])
    try:
        positions = document["joint_states"]["positions_rad"]
    except (KeyError, TypeError) as exc:
        raise ValueError("recorded G2 snapshot has no joint positions") from exc
    schema_names = set(make_g2_joint_schema().names)
    if not isinstance(positions, dict) or set(positions) != schema_names:
        raise ValueError("recorded G2 snapshot does not contain exactly the 22 joints")
    return MappingProxyType({name: float(positions[name]) for name in sorted(schema_names)})


class G2UpperBodyMeshCollisionModel:
    """Audited offline collision model; it cannot send physical commands."""

    def __init__(self, runtime: Path, model: object, geometry_model: object, inventory: G2CollisionInventory):
        self._runtime = runtime
        self._model = model
        self._geometry_model = geometry_model
        self._data = model.createData()
        self._geometry_data = geometry_model.createData()
        self.inventory = inventory
        schema = make_g2_joint_schema()
        self._schema_names = tuple(schema.names)
        self._q_indexes = {
            name: model.joints[model.getJointId(name)].idx_q for name in self._schema_names
        }
        # This geometry URDF rounds a few limits more narrowly than the
        # installed GDK configuration.  RobotAdapter's audited GDK limits are
        # authoritative for commands; the URDF supplies kinematics/geometry.
        self._limits = {
            joint.name: joint.position_limits for joint in schema.joints
        }
        self._mimic = self._read_mimic_relations()

    @classmethod
    def load_default(cls, runtime_root: str | Path | None = None) -> "G2UpperBodyMeshCollisionModel":
        try:
            import pinocchio as pin
        except ImportError as exc:
            raise RuntimeError("Pinocchio/Coal environment is required for mesh collision checks") from exc
        runtime = _runtime_root(runtime_root)
        xml, body_meshes, tool_meshes = _resolved_urdf(runtime)
        model = pin.buildModelFromXML(xml)
        geometry = pin.buildGeomFromUrdfString(model, xml, pin.GeometryType.COLLISION)
        links = [model.frames[item.parentFrame].name for item in geometry.geometryObjects]
        srdf = _srdf_exclusions(runtime)
        for first in range(len(links)):
            for second in range(first + 1, len(links)):
                link_a, link_b = links[first], links[second]
                pair = tuple(sorted((link_a, link_b)))
                same_gripper = any(
                    link_a.startswith(f"gripper_{side}_")
                    and link_b.startswith(f"gripper_{side}_")
                    for side in ("l", "r")
                )
                if link_a == link_b or pair in srdf or pair in _MOUNT_EXCLUSIONS or same_gripper:
                    continue
                geometry.addCollisionPair(pin.CollisionPair(first, second))

        mesh_count = sum(
            1 for item in geometry.geometryObjects
            if Path(getattr(item, "meshPath", "")).suffix.lower() in {".stl", ".dae", ".obj"}
        )
        gripper_mesh_count = sum(
            1 for item, link in zip(geometry.geometryObjects, links)
            if link.startswith("gripper_") and getattr(item, "meshPath", "")
        )
        inventory = G2CollisionInventory(
            geometry_instances=len(geometry.geometryObjects),
            mesh_instances=mesh_count,
            body_mesh_instances=mesh_count - gripper_mesh_count,
            gripper_mesh_instances=gripper_mesh_count,
            primitive_instances=len(geometry.geometryObjects) - mesh_count,
            checked_pairs=len(geometry.collisionPairs),
        )
        expected = (53, 45, 25, 20, 8)
        actual = (
            inventory.geometry_instances,
            inventory.mesh_instances,
            inventory.body_mesh_instances,
            inventory.gripper_mesh_instances,
            inventory.primitive_instances,
        )
        if actual != expected or len(body_meshes) != 25 or len(tool_meshes) != 10:
            raise ValueError(f"unexpected resolved geometry inventory: {actual}")
        return cls(runtime, model, geometry, inventory)

    def _read_mimic_relations(self) -> Mapping[str, tuple[str, float, float]]:
        # The archived simulator-expanded URDF drops most custom loop mimic
        # tags.  These seven ratios per side are copied from the separately
        # hash-verified `gripper.omnipicker.urdf.xacro` in the same bundle.
        relations: dict[str, tuple[str, float, float]] = {}
        for base, side in ((30, "l"), (70, "r")):
            active = f"idx{base + 1}_gripper_{side}_inner_joint1"
            for offset, suffix, multiplier in (
                (2, "inner_joint3", 0.1),
                (3, "inner_joint4", 0.25),
                (9, "inner_joint0", -0.7),
                (11, "outer_joint1", -1.0),
                (12, "outer_joint3", 0.1),
                (13, "outer_joint4", -0.25),
                (19, "outer_joint0", 0.7),
            ):
                name = f"idx{base + offset}_gripper_{side}_{suffix}"
                if self._model.getJointId(name) == 0:
                    raise ValueError(f"dual OmniPicker model is missing {name}")
                relations[name] = (active, multiplier, 0.0)
        return MappingProxyType(relations)

    @staticmethod
    def _validate_grippers(gripper_positions: Mapping[str, float] | None) -> dict[str, float]:
        values = {"left": _GRIPPER_OPEN_RAD, "right": _GRIPPER_OPEN_RAD}
        if gripper_positions is not None:
            if set(gripper_positions) != {"left", "right"}:
                raise ValueError("gripper_positions must define left and right")
            values = dict(gripper_positions)
        for side, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{side} gripper position must be finite")
            if not _GRIPPER_OPEN_RAD <= value <= _GRIPPER_CLOSED_RAD:
                raise ValueError(f"{side} gripper position is outside [-0.785, 0.0] rad")
            values[side] = float(value)
        return values

    def _configuration(self, positions: Mapping[str, float], grippers: Mapping[str, float]) -> np.ndarray:
        if set(positions) != set(self._schema_names):
            raise ValueError("joint positions must contain exactly the 22 G2 joints")
        q = np.zeros(self._model.nq)
        for name in self._schema_names:
            value = positions[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"joint {name} must be finite")
            lower, upper = self._limits[name]
            if not lower - 1e-12 <= value <= upper + 1e-12:
                raise ValueError(
                    f"joint {name} is outside audited GDK limits [{lower}, {upper}]"
                )
            q[self._q_indexes[name]] = float(value)

        active = {
            "idx31_gripper_l_inner_joint1": grippers["left"],
            "idx71_gripper_r_inner_joint1": grippers["right"],
        }
        for name, value in active.items():
            q[self._model.joints[self._model.getJointId(name)].idx_q] = value
        for name, (source, multiplier, offset) in self._mimic.items():
            value = active[source] * multiplier + offset
            q[self._model.joints[self._model.getJointId(name)].idx_q] = value
        return q

    def check_configuration(
        self,
        joint_positions: Mapping[str, float],
        *,
        gripper_positions: Mapping[str, float] | None = None,
        margin_m: float = _DEFAULT_MARGIN_M,
    ) -> G2ConfigurationCollisionResult:
        try:
            import pinocchio as pin
        except ImportError as exc:
            raise RuntimeError("Pinocchio/Coal environment is required for mesh collision checks") from exc
        if isinstance(margin_m, bool) or not isinstance(margin_m, (int, float)) or not math.isfinite(margin_m) or margin_m < 0:
            raise ValueError("margin_m must be finite and nonnegative")
        grippers = self._validate_grippers(gripper_positions)
        q = self._configuration(joint_positions, grippers)
        pin.computeDistances(self._model, self._data, self._geometry_model, self._geometry_data, q)
        minimum = math.inf
        violations: list[G2CollisionViolation] = []
        objects = self._geometry_model.geometryObjects
        frames = self._model.frames
        for pair, result in zip(self._geometry_model.collisionPairs, self._geometry_data.distanceResults):
            distance = float(result.min_distance)
            if not math.isfinite(distance):
                raise ValueError("Coal returned a non-finite geometry distance")
            minimum = min(minimum, distance)
            if distance <= float(margin_m) + 1e-12:
                first = objects[pair.first]
                second = objects[pair.second]
                violations.append(
                    G2CollisionViolation(
                        first_geometry=first.name,
                        second_geometry=second.name,
                        first_link=frames[first.parentFrame].name,
                        second_link=frames[second.parentFrame].name,
                        distance_m=distance,
                    )
                )
        return G2ConfigurationCollisionResult(
            checked_pairs=len(self._geometry_model.collisionPairs),
            minimum_distance_m=minimum,
            violations=tuple(violations),
            margin_m=float(margin_m),
        )

    def check_path(
        self,
        start_positions: Mapping[str, float],
        end_positions: Mapping[str, float],
        *,
        start_grippers: Mapping[str, float] | None = None,
        end_grippers: Mapping[str, float] | None = None,
        max_step_rad: float = 0.01,
        max_gripper_step_rad: float | None = None,
        margin_m: float = _DEFAULT_MARGIN_M,
    ) -> G2PathCollisionResult:
        if isinstance(max_step_rad, bool) or not isinstance(max_step_rad, (int, float)) or not math.isfinite(max_step_rad) or max_step_rad <= 0:
            raise ValueError("max_step_rad must be finite and positive")
        if max_gripper_step_rad is None:
            effective_gripper_step_rad = float(max_step_rad)
        elif (
            isinstance(max_gripper_step_rad, bool)
            or not isinstance(max_gripper_step_rad, (int, float))
            or not math.isfinite(max_gripper_step_rad)
            or max_gripper_step_rad <= 0.0
        ):
            raise ValueError("max_gripper_step_rad must be finite and positive")
        else:
            effective_gripper_step_rad = float(max_gripper_step_rad)
        start_grip = self._validate_grippers(start_grippers)
        end_grip = self._validate_grippers(end_grippers if end_grippers is not None else start_grip)
        # Validate endpoints before interpolation, including exact key sets and limits.
        self._configuration(start_positions, start_grip)
        self._configuration(end_positions, end_grip)
        maximum_joint_delta = max(
            abs(float(end_positions[name]) - float(start_positions[name]))
            for name in self._schema_names
        )
        maximum_gripper_delta = max(
            abs(end_grip[side] - start_grip[side]) for side in ("left", "right")
        )
        segments = max(
            1,
            math.ceil(maximum_joint_delta / float(max_step_rad)),
            math.ceil(maximum_gripper_delta / effective_gripper_step_rad),
        )
        minimum = math.inf
        for sample in range(segments + 1):
            fraction = sample / segments
            positions = {
                name: float(start_positions[name])
                + fraction * (float(end_positions[name]) - float(start_positions[name]))
                for name in self._schema_names
            }
            grippers = {
                side: start_grip[side] + fraction * (end_grip[side] - start_grip[side])
                for side in ("left", "right")
            }
            result = self.check_configuration(positions, gripper_positions=grippers, margin_m=margin_m)
            minimum = min(minimum, result.minimum_distance_m)
            if result.violations:
                return G2PathCollisionResult(
                    sample_count=sample + 1,
                    checked_pairs_per_sample=result.checked_pairs,
                    minimum_distance_m=minimum,
                    first_violation_fraction=fraction,
                    first_violation=result.violations[0],
                )
        return G2PathCollisionResult(
            sample_count=segments + 1,
            checked_pairs_per_sample=self.inventory.checked_pairs,
            minimum_distance_m=minimum,
            first_violation_fraction=None,
            first_violation=None,
        )

    def validate_recorded_fk(self) -> G2RecordedFKResult:
        try:
            import pinocchio as pin
        except ImportError as exc:
            raise RuntimeError("Pinocchio is required for FK evidence validation") from exc
        evidence_dir = self._runtime / "artifacts/g2-geometry" / _TOOL_BUNDLE / "evidence"
        snapshots = sorted(evidence_dir.glob("g2-calibration-*.json"))
        if len(snapshots) != 2:
            raise ValueError("expected exactly two recorded G2 calibration snapshots")
        max_translation = 0.0
        max_rotation = 0.0
        frames_checked = 0
        expected_frames = {
            f"gripper_{side}_{suffix}"
            for side in ("l", "r")
            for suffix in (
                "base_link", "camera_link", "center_link",
                "inner_link1", "inner_link2", "inner_link3", "inner_link4",
                "outer_link1", "outer_link2", "outer_link3", "outer_link4",
            )
        }
        for snapshot_path in snapshots:
            document = _load_json(snapshot_path)
            positions = document.get("joint_states", {}).get("positions_rad", {})
            records = {
                row.get("child_frame"): row
                for row in document.get("tf_from_base_link", [])
                if isinstance(row, dict) and row.get("child_frame") in expected_frames
            }
            if set(records) != expected_frames:
                raise ValueError(f"incomplete gripper TF evidence in {snapshot_path.name}")

            # GDK's 22-joint snapshot does not expose gripper aperture. Infer
            # the one active scalar per side from its 11 observed link poses,
            # then validate the whole linkage against those same observations.
            fitted: dict[str, float] = {}
            for side, label in (("l", "left"), ("r", "right")):
                side_names = sorted(name for name in expected_frames if name.startswith(f"gripper_{side}_"))

                def score(active: float) -> float:
                    trial = {"left": _GRIPPER_OPEN_RAD, "right": _GRIPPER_OPEN_RAD}
                    trial[label] = active
                    q_trial = self._configuration(positions, trial)
                    data_trial = self._model.createData()
                    pin.forwardKinematics(self._model, data_trial, q_trial)
                    pin.updateFramePlacements(self._model, data_trial)
                    total = 0.0
                    for frame_name in side_names:
                        placement = data_trial.oMf[self._model.getFrameId(frame_name)]
                        record = records[frame_name]
                        observed_translation = np.asarray(record["translation_m"], dtype=float)
                        xyzw = record["rotation_xyzw"]
                        observed_rotation = pin.Quaternion(xyzw[3], xyzw[0], xyzw[1], xyzw[2]).matrix()
                        translation_error = float(np.linalg.norm(placement.translation - observed_translation))
                        rotation_error = float(np.linalg.norm(pin.log3(placement.rotation.T @ observed_rotation)))
                        total += translation_error * translation_error + (0.05 * rotation_error) ** 2
                    return total

                low, high = _GRIPPER_OPEN_RAD, _GRIPPER_CLOSED_RAD
                for _ in range(48):
                    first = low + (high - low) / 3.0
                    second = high - (high - low) / 3.0
                    if score(first) <= score(second):
                        high = second
                    else:
                        low = first
                fitted[label] = (low + high) / 2.0

            q = self._configuration(positions, fitted)
            data = self._model.createData()
            pin.forwardKinematics(self._model, data, q)
            pin.updateFramePlacements(self._model, data)
            for name, record in records.items():
                placement = data.oMf[self._model.getFrameId(name)]
                observed_translation = np.asarray(record["translation_m"], dtype=float)
                xyzw = record["rotation_xyzw"]
                observed_rotation = pin.Quaternion(xyzw[3], xyzw[0], xyzw[1], xyzw[2]).matrix()
                translation_error = float(np.linalg.norm(placement.translation - observed_translation))
                rotation_error = float(np.linalg.norm(pin.log3(placement.rotation.T @ observed_rotation)))
                max_translation = max(max_translation, translation_error)
                max_rotation = max(max_rotation, rotation_error)
                frames_checked += 1
        # Report unique bilateral frames, while using both snapshots to compute maxima.
        return G2RecordedFKResult(
            frames_checked=len(expected_frames),
            max_translation_error_m=max_translation,
            max_rotation_error_rad=max_rotation,
            matches_evidence=(
                frames_checked == len(expected_frames) * len(snapshots)
                and max_translation < 0.0001
                and max_rotation < math.radians(0.2)
            ),
        )
