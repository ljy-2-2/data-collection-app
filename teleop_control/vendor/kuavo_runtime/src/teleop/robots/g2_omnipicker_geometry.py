"""Offline evidence audit for the G2 dual OmniPicker geometry bundle."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import xml.etree.ElementTree as ET


_TOOL_SUFFIXES = (
    "base_link", "camera_link", "center_link",
    "inner_link1", "inner_link2", "inner_link3", "inner_link4",
    "outer_link1", "outer_link2", "outer_link3", "outer_link4",
)


@dataclass(frozen=True)
class G2OmniPickerGeometryReport:
    end_models: Mapping[str, str]
    unique_meshes: int
    mesh_bytes: int
    snapshots: int
    frames_per_side: Mapping[str, int]
    bilateral_xacro: bool
    passive_mimic_relations: int
    issues: tuple[str, ...]
    gripper_geometry_ready: bool
    upper_body_live_ready: bool = False
    physical_robot_command_sent: bool = False


class G2OmniPickerGeometryAudit:
    """Validate copied model evidence without importing GDK or contacting hardware."""

    @classmethod
    def inspect(cls, bundle: str | Path) -> G2OmniPickerGeometryReport:
        root = Path(bundle)
        issues: list[str] = []
        end_models = {"left": "unknown", "right": "unknown"}
        frames_per_side = {"left": 0, "right": 0}
        bilateral_xacro = False
        passive_mimic_relations = 0

        probe = cls._load_json(root / "evidence/gdk-readonly-20260723-121927.json", issues)
        status = probe.get("whole_body_status", {}) if isinstance(probe, dict) else {}
        for side in end_models:
            value = status.get(f"{side}_end_model") if isinstance(status, dict) else None
            if isinstance(value, str):
                end_models[side] = value.lower()
            if end_models[side] != "omnipicker":
                issues.append(f"{side} end model is not proven as omnipicker")

        mesh_map = cls._load_json(root / "source-mesh-map.json", issues)
        mesh_entries: dict[str, dict] = {}
        if isinstance(mesh_map, dict):
            for record in mesh_map.get("meshes", []):
                if not isinstance(record, dict):
                    continue
                cache_file = record.get("cache_file")
                link = record.get("link", "")
                if isinstance(cache_file, str) and isinstance(link, str) and link.startswith("gripper_r_"):
                    mesh_entries[cache_file] = record

        expected_meshes = {
            "camera_link.STL", "gripper_base_link.STL",
            "inner_link1.STL", "inner_link2.STL", "inner_link3.STL", "inner_link4.STL",
            "outer_link1.STL", "outer_link2.STL", "outer_link3.STL", "outer_link4.STL",
        }
        if set(mesh_entries) != expected_meshes:
            issues.append("source mesh map does not define exactly the 10 OmniPicker meshes")

        valid_meshes = 0
        mesh_bytes = 0
        for filename in sorted(expected_meshes):
            record = mesh_entries.get(filename)
            path = root / "meshes" / filename
            if record is None:
                continue
            if not path.is_file():
                issues.append(f"missing mesh: {filename}")
                continue
            payload = path.read_bytes()
            if len(payload) != record.get("bytes"):
                issues.append(f"mesh size mismatch: {filename}")
                continue
            if hashlib.sha256(payload).hexdigest() != record.get("lfs_oid"):
                issues.append(f"mesh sha256 mismatch: {filename}")
                continue
            valid_meshes += 1
            mesh_bytes += len(payload)

        urdf_path = root / "G2_omnipicker_fixed_dual.urdf"
        if not urdf_path.is_file():
            issues.append("missing dual OmniPicker reference URDF")
        else:
            try:
                links = {link.get("name") for link in ET.parse(urdf_path).getroot().findall("link")}
            except (ET.ParseError, OSError) as exc:
                issues.append(f"invalid dual OmniPicker reference URDF: {exc}")
            else:
                for side in ("left", "right"):
                    prefix = f"gripper_{side[0]}_"
                    for suffix in _TOOL_SUFFIXES:
                        if prefix + suffix not in links:
                            issues.append(f"URDF missing {prefix + suffix}")

        xacro_path = root / "gripper.omnipicker.urdf.xacro"
        dispatcher_path = root / "G2.gripper.urdf.xacro"
        expected_xacro_sha = "391bc63823b9b84cd68f7b32a711ba413614c0d41b5fdf9001998a8051ae48fd"
        expected_dispatcher_sha = "8d7541315faba868e6ca83d7eb09a6f54c33a3cc5db127bd66823138b5daa477"
        try:
            xacro_payload = xacro_path.read_bytes()
            dispatcher_payload = dispatcher_path.read_bytes()
        except OSError as exc:
            issues.append(f"cannot read OmniPicker Xacro sources: {exc}")
        else:
            if hashlib.sha256(xacro_payload).hexdigest() != expected_xacro_sha:
                issues.append("OmniPicker Xacro sha256 mismatch")
            if hashlib.sha256(dispatcher_payload).hexdigest() != expected_dispatcher_sha:
                issues.append("G2 gripper dispatcher Xacro sha256 mismatch")
            xacro_text = xacro_payload.decode("utf-8")
            dispatcher_text = dispatcher_payload.decode("utf-8")
            passive_mimic_relations = xacro_text.count("<mimic ")
            bilateral_xacro = (
                'params="side parent_link' in xacro_text
                and 'side="l"' in dispatcher_text
                and 'side="r"' in dispatcher_text
            )
            if passive_mimic_relations != 7:
                issues.append("OmniPicker Xacro does not define 7 passive mimic relations")
            if not bilateral_xacro:
                issues.append("OmniPicker Xacro is not wired for both sides")

        snapshots = sorted((root / "evidence").glob("g2-calibration-*.json"))
        if len(snapshots) != 2:
            issues.append(f"expected 2 GDK TF snapshots, found {len(snapshots)}")
        for path in snapshots:
            document = cls._load_json(path, issues)
            transforms = document.get("tf_from_base_link", []) if isinstance(document, dict) else []
            for side in ("left", "right"):
                prefix = f"gripper_{side[0]}_"
                found = {
                    record.get("child_frame") for record in transforms
                    if isinstance(record, dict)
                    and isinstance(record.get("child_frame"), str)
                    and record["child_frame"].startswith(prefix)
                }
                expected = {prefix + suffix for suffix in _TOOL_SUFFIXES}
                if found != expected:
                    issues.append(f"{path.name} has incomplete {side} OmniPicker TF frames")
                else:
                    frames_per_side[side] = len(found)

        ready = (
            not issues and valid_meshes == len(expected_meshes)
            and len(snapshots) == 2
            and all(value == "omnipicker" for value in end_models.values())
            and bilateral_xacro and passive_mimic_relations == 7
        )
        return G2OmniPickerGeometryReport(
            end_models=MappingProxyType(end_models),
            unique_meshes=valid_meshes,
            mesh_bytes=mesh_bytes,
            snapshots=len(snapshots),
            frames_per_side=MappingProxyType(frames_per_side),
            bilateral_xacro=bilateral_xacro,
            passive_mimic_relations=passive_mimic_relations,
            issues=tuple(issues),
            gripper_geometry_ready=ready,
        )

    @staticmethod
    def _load_json(path: Path, issues: list[str]) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"cannot read {path.name}: {exc}")
            return {}
