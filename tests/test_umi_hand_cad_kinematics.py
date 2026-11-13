"""Original saved CAD geometry and the existing mirrored recording contract."""
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
    # Reconstruct the datum from raw CAD instance transforms, not Xacro values.
    gears = [np.array(g["transform"]) for g in AUDIT["gear_instances"]]
    centre = np.r_[AUDIT["gear_local_center_m"], 1]
    points = [(g @ centre)[:3] for g in gears]
    root = np.eye(4)
    root[:3, 3] = (points[0] + points[1]) / 2
    z = gears[0][:3, 0]
    y = points[1] - points[0]
    assert np.linalg.norm(y) == pytest.approx(.033, abs=1e-10)
    y /= np.linalg.norm(y)
    root[:3, :3] = np.column_stack((np.cross(y, z), y, z))
    np.testing.assert_allclose(root, AUDIT["assembly_from_hand_root"], atol=1e-12)
    np.testing.assert_allclose(root[:3, :3].T @ root[:3, :3], np.eye(3), atol=1e-10)
    for g in AUDIT["gear_instances"]:
        assert g["local_bounds_m"][0][0] == pytest.approx(0, abs=1e-10)
        assert g["local_bounds_m"][1][0] == pytest.approx(.007, abs=1e-9)
    camera = np.linalg.inv(root) @ AUDIT["camera_instance"]["transform"]
    expected_position = (camera @ np.r_[AUDIT["camera_reference_local_m"], 1])[:3]
    expected_forward = camera[:3, :3] @ AUDIT["camera_forward_local"]
    for hand in ("left", "right"):
        origin = model.find(f"joint[@name='{hand}_camera_joint']/origin")
        np.testing.assert_allclose(vector(origin, "xyz"), expected_position, atol=1e-9)
        rpy = vector(origin, "rpy")
        assert rpy[1] == pytest.approx(math.radians(40), abs=1e-10)
        np.testing.assert_allclose(rotation(rpy)[:, 0], expected_forward, atol=1e-10)
        assert (rotation(rpy) @ [1, 0, 0])[2] < 0  # Downward, not upward.
    for gear in points:
        local = np.linalg.inv(root) @ np.r_[gear, 1]
        np.testing.assert_allclose(local[[0, 2]], [0, 0], atol=1e-10)
        assert abs(local[1]) == pytest.approx(.0165, abs=1e-10)


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
    assert model.find(".//visual") is None
    assert model.find(".//collision") is None


def test_pico_center_uses_quest_grip_alignment_and_retains_aim_direction(model):
    center = AUDIT["pico_center_reference"]
    quest_grip = np.array(center["quest"]["grip_frame_in_root_mm"])
    pico_grip = np.array(center["pico"]["grip_frame_in_root_mm"])
    transfer = pico_grip @ np.linalg.inv(quest_grip)
    np.testing.assert_allclose(transfer[:3, :3].T @ transfer[:3, :3], np.eye(3), atol=1e-12)
    assert np.linalg.det(transfer[:3, :3]) == pytest.approx(1)
    expected_position = (transfer @ np.r_[center["quest_reference_in_yubi_root_mm"], 1])[:3] / 1000
    ray = AUDIT["pico_ray_reference"]
    root_from_controller = np.linalg.inv(AUDIT["assembly_from_hand_root"]) @ ray["assembly_from_mounted_controller"]
    old_rotation = np.array(ray["historical_from_controller"])[:3, :3]
    expected_direction = root_from_controller[:3, :3] @ old_rotation.T @ ray["historical_direction"]
    pico = model.find("joint[@name='right_mount_joint']/origin")
    np.testing.assert_allclose(vector(pico, "xyz"), expected_position, atol=1e-10)
    np.testing.assert_allclose(rotation(vector(pico, "rpy"))[:, 0], expected_direction, atol=1e-10)
    np.testing.assert_allclose(expected_direction, [2**-.5, 0, 2**-.5], atol=1e-10)
    assert vector(pico, "rpy")[1] == pytest.approx(-math.pi / 4, abs=1e-12)
    # The landmark is on the original mounted controller, not a detached model.
    triangle = np.array(ray["nearest_original_triangle_local_m"])
    point = np.array(ray["point_controller_local_m"])
    ab, ac = triangle[1] - triangle[0], triangle[2] - triangle[0]
    uv, *_ = np.linalg.lstsq(np.column_stack((ab, ac)), point - triangle[0], rcond=None)
    projected = triangle[0] + uv[0]*ab + uv[1]*ac
    assert np.linalg.norm(point - projected) < 1e-8
    assert min(uv) >= 0 and uv.sum() <= 1


def test_unidentified_contact_landmark_is_not_replaced_by_cad_origin(model):
    preserved = AUDIT["preserved_uncalibrated"]
    contact = model.find("joint[@name='right_right_finger_tip_joint']/origin")
    np.testing.assert_allclose(vector(contact, "xyz"), preserved["finger_tip_offset_m"])
