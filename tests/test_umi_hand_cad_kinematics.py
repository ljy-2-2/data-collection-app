"""Current Windows CAD transforms and the mirrored recording contract."""
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import pytest
import yaml

xacro = pytest.importorskip("xacro")
GALAXIS = Path(__file__).resolve().parents[1] / "umi_collection/vendor/galaxis"
SOURCE = GALAXIS / "galaxis_description/urdf/galaxis_hand.urdf.xacro"
AUDIT = json.loads((GALAXIS / "docs/galaxis_hand_cad_measurements.json").read_text())

CURRENT = json.loads((GALAXIS / "docs/windows_end_effector_measurements.json").read_text())

@pytest.fixture(scope="module")
def model():
    return ET.fromstring(xacro.process_file(str(SOURCE)).toxml())


def vector(element, field):
    return np.fromstring(element.get(field), sep=" ")


def rotation(rpy):
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array([[cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr],
                     [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr],
                     [-sp, cp*sr, cp*cr]])


def test_camera_reconstructed_from_saved_assembly(model):
    for hand in ("left", "right"):
        data = CURRENT["assemblies"][hand]
        root = np.array(data["assembly_from_root"])
        np.testing.assert_allclose(root[:3, :3], [[0,-1,0],[1,0,0],[0,0,1]], atol=1e-12)
        assert root[2,3] == pytest.approx(.0055, abs=1e-9)
        camera = np.linalg.inv(root) @ data["camera_component_transform"]
        expected = (camera @ np.r_[data["camera_reference_local_m"], 1])[:3]
        origin = model.find(f"joint[@name='{hand}_camera_joint']/origin")
        np.testing.assert_allclose(vector(origin, "xyz"), expected, atol=1e-10)
        rpy = vector(origin, "rpy")
        assert rpy[1] == pytest.approx(math.radians(15), abs=1e-10)
        np.testing.assert_allclose(rotation(rpy), camera[:3,[2,0,1]], atol=1e-10)
        for side, point in data["jaw_pivots_assembly_m"].items():
            joint = model.find(f"joint[@name='{hand}_{side}_finger_joint']/origin")
            np.testing.assert_allclose(vector(joint,"xyz"),
                (np.linalg.inv(root) @ np.r_[point,1])[:3], atol=1e-10)
            assert abs(vector(joint,"xyz")[1]) == pytest.approx(.019875, abs=1e-10)


@pytest.mark.parametrize("joint", ["camera", "mount"])
def test_left_right_pose_reflection(model, joint):
    s = np.diag([1., -1., 1.])
    right = model.find(f"joint[@name='right_{joint}_joint']/origin")
    left = model.find(f"joint[@name='left_{joint}_joint']/origin")
    np.testing.assert_allclose(vector(left, "xyz"), s @ vector(right, "xyz"), atol=1e-12)
    lr, rr = rotation(vector(left, "rpy")), rotation(vector(right, "rpy"))
    np.testing.assert_allclose(lr, s @ rr @ s, atol=1e-12)
    assert np.linalg.det(lr) == pytest.approx(1)


def tip(model, hand, side, q):
    joint = model.find(f"joint[@name='{hand}_{side}_finger_joint']")
    if joint.find("mimic") is not None:
        q *= float(joint.find("mimic").get("multiplier"))
    angle = vector(joint.find("axis"), "xyz")[2] * q
    origin = vector(joint.find("origin"), "xyz")
    offset = vector(model.find(f"joint[@name='{hand}_{side}_finger_tip_joint']/origin"), "xyz")
    return origin + rotation([0, 0, angle]) @ offset


@pytest.mark.parametrize("q", [0, .1, .33, .66])
def test_mirrored_fingers_open_outward(model, q):
    s = np.diag([1., -1., 1.])
    for right_side, left_side in (("left", "right"), ("right", "left")):
        np.testing.assert_allclose(tip(model, "left", left_side, q),
                                   s @ tip(model, "right", right_side, q), atol=1e-12)
    for hand in ("left", "right"):
        if q:
            assert tip(model, hand, "left", q)[1] > tip(model, hand, "left", 0)[1]
            assert tip(model, hand, "right", q)[1] < tip(model, hand, "right", 0)[1]


def test_tree_and_encoder_joint_contract(model):
    links = {x.get("name") for x in model.findall("link")}
    joints = model.findall("joint")
    children = [j.find("child").get("link") for j in joints]
    assert len(links) == 15
    assert len(children) == len(set(children)) == len(links) - 1
    parents = {j.find("child").get("link"): j.find("parent").get("link") for j in joints}
    assert links - set(children) == {"pico_origin"}
    for child in children:
        seen = set()
        while child != "pico_origin":
            assert child not in seen
            seen.add(child)
            child = parents[child]
    config = yaml.safe_load((GALAXIS / "galaxis_bringup/config/galaxis_config.yaml").read_text())
    encoder = config["galaxis_devices"]["encoder_node"]["ros__parameters"]
    for hand, sign in (("left", 1), ("right", -1)):
        assert encoder[f"{hand}_joint_name"] == f"{hand}_{hand}_finger_joint"
        for side in ("left", "right"):
            j = model.find(f"joint[@name='{hand}_{side}_finger_joint']")
            assert j.get("type") == "continuous"
            np.testing.assert_array_equal(vector(j.find("axis"), "xyz"), [0, 0, sign])
            if side == hand:
                assert j.find("mimic") is None
            else:
                assert j.find("mimic").get("joint") == f"{hand}_{hand}_finger_joint"
                assert float(j.find("mimic").get("multiplier")) == -1
    visuals = model.findall(".//visual")
    assert len(visuals) > 60
    for visual in visuals:
        mesh = visual.find("geometry/mesh").get("filename")
        assert mesh.startswith("package://galaxis_description/meshes/windows/")
        path = GALAXIS / mesh.removeprefix("package://")
        assert path.is_file() and path.stat().st_size > 84
    assert model.find(".//collision") is None


def test_pico_reference_moves_rigidly_with_its_controller(model):
    old_root_from_controller = np.linalg.inv(AUDIT["assembly_from_hand_root"]) @ AUDIT["pico_ray_reference"]["assembly_from_mounted_controller"]
    props = CURRENT["old_properties"]
    old_mount = np.eye(4)
    old_mount[:3,3] = [props["mount_"+s] for s in "xyz"]
    old_mount[:3,:3] = rotation([props["mount_"+s] for s in ("roll","pitch","yaw")])
    # Preserve the physical user-selected point in part coordinates. Merely
    # copying its old hand-root coordinates would fail this check.
    local = np.linalg.inv(old_root_from_controller) @ old_mount
    np.testing.assert_allclose(local, CURRENT["pico_local_reference"], atol=1e-12)
    right = CURRENT["assemblies"]["right"]
    expected = np.linalg.inv(right["assembly_from_root"]) @ right["pico_component_transform"] @ local
    origin = model.find("joint[@name='right_mount_joint']/origin")
    np.testing.assert_allclose(vector(origin,"xyz"),expected[:3,3],atol=1e-10)
    np.testing.assert_allclose(rotation(vector(origin,"rpy")),expected[:3,:3],atol=1e-10)


def test_nominal_contact_points_meet_at_closed_midplane(model):
    for hand in ("left", "right"):
        for side in ("left", "right"):
            np.testing.assert_allclose(tip(model, hand, side, 0),
                                       [.13694, 0, 0], atol=1e-10)


def test_contact_landmarks_follow_the_closed_pad_geometry(model):
    # Intersect the actual installed TPU triangles with the gear mid-plane.
    # This catches stale offsets even when their metadata agrees with them.
    dtype = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)),
                      ("attribute", "<u2")])
    for part in CURRENT["hand_visual_parts"]:
        if "脚垫" not in part["name"]:
            continue
        hand, side = part["hand"], part["role"].split("_")[0]
        visual = model.find(f".//visual[@name='{part['visual']}']")
        mesh = GALAXIS / "galaxis_description/meshes/windows" / part["mesh"]
        raw = mesh.read_bytes()
        triangles = np.frombuffer(raw, dtype=dtype, count=int.from_bytes(raw[80:84], "little"),
                                  offset=84)["vertices"].astype(float)
        origin = visual.find("origin")
        triangles = triangles @ rotation(vector(origin, "rpy")).T + vector(origin, "xyz")
        joint = model.find(f"joint[@name='{hand}_{side}_finger_joint']/origin")
        triangles += vector(joint, "xyz")
        sections = []
        for i in range(3):
            a, b = triangles[:, i], triangles[:, (i + 1) % 3]
            mask = (a[:, 2] * b[:, 2] <= 0) & (np.abs(a[:, 2] - b[:, 2]) > 1e-12)
            a, b = a[mask], b[mask]
            sections.append(a + (-a[:, 2] / (b[:, 2] - a[:, 2]))[:, None] * (b - a))
        points = np.concatenate(sections)
        contact = points[np.argmin((1 if side == "left" else -1) * points[:, 1])]
        assert np.linalg.norm(tip(model, hand, side, 0) - contact) < .0003
