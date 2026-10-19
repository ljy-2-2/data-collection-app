"""Vendor FK/IK plus independent URDF verification and conservative mesh bounds."""

from pathlib import Path
import os
import sys
import math
import struct
import xml.etree.ElementTree as ET
import numpy as np
from .geometry import transform, inverse, rotation, angle
from .mapping import (
    robot_world_from_mount,
    robot_from_behind_operator,
    behind_operator_convention,
    installed_base_from_nominal,
    arm_model_sides,
    arm_base_sides,
    arm_mount_orientations,
)

ROOT = Path(
    os.environ.get(
        "MARVIN_SDK_ROOT",
        str(Path(__file__).resolve().parents[2]),
    )
).resolve()
sys.path.insert(0, str(ROOT))
MOUNT = (
    ROOT
    / "双臂底座支架图纸260811/固定件/固定件urdf/Dual_arm_mount-m6/urdf/Dual_arm_mount-m6.urdf"
)
STAND = ROOT / "双臂底座支架图纸260811/底座支架/底座支架urdf/base_m6/urdf/base_m6.urdf"


def origin(element):
    if element is None:
        return np.eye(4)
    return transform(
        [float(x) for x in element.get("xyz", "0 0 0").split()],
        [float(x) for x in element.get("rpy", "0 0 0").split()],
    )


def mesh_vertices(path):
    data = path.read_bytes()
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    if len(data) == 84 + 50 * n:
        dtype = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
        return (
            np.frombuffer(data, dtype=dtype, offset=84)["v"]
            .reshape(-1, 3)
            .astype(float)
        )
    return np.asarray(
        [
            [float(x) for x in line.split()[1:]]
            for line in data.decode().splitlines()
            if line.strip().startswith("vertex ")
        ]
    )


def bounds(urdf, link):
    node = ET.parse(urdf).getroot().find(f"link[@name='{link}']/collision")
    mesh = node.find("geometry/mesh")
    path = urdf.parent.parent / "meshes" / mesh.get("filename").split("/")[-1]
    vertices = mesh_vertices(path) * np.array(
        [float(x) for x in mesh.get("scale", "1 1 1").split()]
    )
    t = origin(node.find("origin"))
    vertices = vertices @ t[:3, :3].T + t[:3, 3]
    lo, hi = vertices.min(axis=0), vertices.max(axis=0)
    return (lo + hi) / 2, (hi - lo) / 2


class ArmModel:
    def __init__(self, side, index, base):
        from SDK_PYTHON.fx_kine import Marvin_Kine

        self.side, self.model_side, self.base = side, side, base
        letter = "L" if side == "left" else "R"
        folder = ROOT / f"Marvin M6-S-{letter}-CCS-696-V4.0 urdf (FUSION)/urdf"
        generated = folder / f"marvin_{side}_with_gripper.urdf"
        original = folder / f"Marvin M6-S-{letter}-CCS-696-V4.0 urdf.urdf"
        # Do not let filesystem glob order choose the arm description.
        self.path = generated if generated.is_file() else original
        tree = ET.parse(self.path).getroot()
        self.joints = [
            tree.find(f"joint[@name='Joint{i}_{letter}']") for i in range(1, 8)
        ]
        self.origins = [origin(j.find("origin")) for j in self.joints]
        self.axes = [
            np.array([float(x) for x in j.find("axis").get("xyz").split()])
            for j in self.joints
        ]
        self.link_bounds = [bounds(self.path, f"Link{i}_{letter}") for i in range(1, 8)]
        self.base_bound = (self.base, *bounds(self.path, f"Base_{letter}"))
        self.kine = Marvin_Kine()
        cfg = self.kine.load_config(index, str(ROOT / "CommonConfig/ccs_m6_40.MvKDCfg"))
        if not cfg or not self.kine.initial_kine(
            cfg["TYPE"][index], cfg["DH"][index], cfg["PNVA"][index], cfg["BD"][index]
        ):
            raise RuntimeError("vendor kinematics initialization failed")
        self.kine.log_switch(0)
        self.gravity = np.asarray(cfg["GRV"][index])
        dh = np.asarray(cfg["DH"][index])
        self.elbow_singularity_deg = -math.degrees(
            math.atan2(dh[3, 1], dh[2, 2]) + math.atan2(dh[3, 1], dh[4, 2])
        )
        self.kine.remove_tool_kine()
        self.kine.remove_user_frame_kine()
        # LOADMvCfg outputs [positive, negative, velocity, acceleration], unlike file column order.
        pn = np.array(cfg["PNVA"][index])
        self.velocity_limits = pn[:, 2].copy()
        self.acceleration_limits = pn[:, 3].copy()
        self.lower = np.maximum(
            pn[:, 1],
            np.degrees([float(j.find("limit").get("lower")) for j in self.joints]),
        )
        self.upper = np.minimum(
            pn[:, 0],
            np.degrees([float(j.find("limit").get("upper")) for j in self.joints]),
        )
        self.bd = cfg["BD"][index]
        # The stopping envelope requires a convex admissible wrist polygon.
        bd = np.asarray(self.bd)
        if not (
            np.allclose(bd[:, 0], 0)
            and np.allclose(
                bd[:, 1], [-abs(bd[0, 1]), abs(bd[0, 1]), -abs(bd[0, 1]), abs(bd[0, 1])]
            )
            and np.allclose(bd[:, 2], [bd[0, 2], bd[0, 2], -bd[0, 2], -bd[0, 2]])
        ):
            raise ValueError("unsupported non-convex vendor wrist constraints")

        # MDH flange row: Rx(90deg) Rz(90deg), translation [0,-95,0] in Link7.
        self.flange = transform((0, -0.095, 0))
        self.flange[:3, :3] = rotation([1, 0, 0], math.pi / 2) @ rotation(
            [0, 0, 1], math.pi / 2
        )
        self.tool = np.eye(4)
        self.control_point_name = "bare_flange"
        self.tool_bound = None

    def configure_tool(self, definition):
        from .tool_frame import parse_tool_frame

        self.tool, self.control_point_name = parse_tool_frame(definition)

    def configure_tool_collision(self, definition):
        if definition is None:
            self.tool_bound = None
            return
        from .tool_frame import parse_tool_frame

        if not isinstance(definition, dict) or set(definition) != {
            "path",
            "scale",
            "xyz_mm",
            "rpy_deg",
        }:
            raise ValueError("tool_collision requires path, scale, xyz_mm, rpy_deg")
        scale = definition["scale"]
        if (
            type(scale) not in (int, float)
            or not math.isfinite(scale)
            or not 0 < scale <= 1
        ):
            raise ValueError("invalid tool mesh unit scale")
        path = (ROOT / definition["path"]).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError("tool mesh must be inside the Marvin SDK folder")
        frame, _ = parse_tool_frame(
            {
                "name": "mesh_in_flange",
                "xyz_mm": definition["xyz_mm"],
                "rpy_deg": definition["rpy_deg"],
            }
        )
        vertices = mesh_vertices(path) * scale
        vertices = vertices @ frame[:3, :3].T + frame[:3, 3]
        if not np.isfinite(vertices).all() or max(np.ptp(vertices, axis=0)) > 1:
            raise ValueError("invalid tool mesh dimensions")
        low, high = vertices.min(axis=0), vertices.max(axis=0)
        self.tool_bound = ((low + high) / 2, (high - low) / 2)

    def chain(self, q):
        t = self.base.copy()
        result = []
        for o, a, v in zip(self.origins, self.axes, np.radians(q)):
            r = np.eye(4)
            r[:3, :3] = rotation(a, v)
            t = t @ o @ r
            result.append(t.copy())
        return result

    def fk(self, q):
        """Selected TCP pose in the shared robot world (metres)."""
        return self.flange_fk(q) @ self.tool

    def flange_fk(self, q):
        """Physical bare flange pose; independent of the selected TCP."""
        m = self.kine.fk(list(map(float, q)))
        if m is False:
            raise ValueError("vendor FK failed")
        t = np.asarray(m)
        t[:3, 3] /= 1000
        return self.base @ t

    def world_jacobian(self, q, point):
        """Manufacturer analytic Jacobian, shifted from flange to selected TCP.

        FX_Jacb_ROT_7 already returns metres/radian in the vendor base frame.
        The vendor tool/user frames remain identity; the physical model is not
        mutated when changing a Python TCP definition.
        """
        raw = self.kine.joints2JacobMatrix(list(map(float, q)))
        if raw is False:
            raise ValueError(f"{self.side}: vendor Jacobian failed")
        j = np.asarray(raw, dtype=float)
        if j.shape != (6, 7) or not np.isfinite(j).all():
            raise ValueError("invalid vendor Jacobian")
        r = self.base[:3, :3]
        linear, angular = r @ j[:3], r @ j[3:]
        lever = point[:3, 3] - self.flange_fk(q)[:3, 3]
        linear += np.cross(angular.T, lever).T
        return np.vstack((linear, angular))

    def valid_joints(self, q, margin=0.2):
        q = np.asarray(q)
        if (
            q.shape != (7,)
            or not np.isfinite(q).all()
            or np.any(q < self.lower + margin)
            or np.any(q > self.upper - margin)
        ):
            return False
        j6, j7 = q[5:]
        quadrant = (
            0
            if j6 >= 0 and j7 >= 0
            else 1
            if j6 < 0 <= j7
            else 2
            if j6 < 0 and j7 < 0
            else 3
        )
        a, b, c = self.bd[quadrant]
        boundary = a * j6 * j6 + b * j6 + c
        return bool(j7 <= boundary - margin if j7 >= 0 else j7 >= boundary + margin)

    def differential_ik(
        self,
        target,
        reference,
        dt,
        max_speed_deg_s,
        velocity=None,
        max_accel=None,
        **quality,
    ):
        from .differential import step

        return step(
            self,
            target,
            reference,
            dt,
            max_speed_deg_s,
            velocity,
            max_accel,
            **quality,
        )

    def joint_halfspaces(self, margin=0.2):
        """A q <= b, in degrees: joint boxes and all four vendor wrist faces."""
        wrist = np.zeros((4, 7))
        slope = abs(self.bd[0][1])
        wrist[:, 5:] = [[slope, 1], [-slope, 1], [slope, -1], [-slope, -1]]
        return (
            np.vstack((np.eye(7), -np.eye(7), wrist)),
            np.r_[
                self.upper - margin,
                -self.lower - margin,
                np.full(4, self.bd[0][2] - margin),
            ],
        )

    def tracking_ik(
        self, target, reference, dt, max_speed_deg_s, velocity=None, max_accel=None
    ):
        from .vendor_tracking import step

        return step(self, target, reference, dt, max_speed_deg_s, velocity, max_accel)

    def ik(self, target, reference, allow_redundant_singular=False):
        from SDK_PYTHON.fx_kine import FX_InvKineSolvePara

        # Vendor library remains in its bare-flange configuration. Remove the
        # fixed tool transform from the target, not from measured joint angles.
        local = inverse(self.base) @ target @ inverse(self.tool)
        local[:3, 3] *= 1000
        sp = FX_InvKineSolvePara()
        sp.set_input_ik_target_tcp(local.reshape(-1).tolist())
        sp.set_input_ik_ref_joint(list(map(float, reference)))
        out = self.kine.ik(sp)
        self.last_vendor_flags = {
            "out_of_range": bool(sp.m_Output_IsOutRange),
            "singular_joints": [i + 1 for i, v in enumerate(sp.m_Output_IsDeg) if v],
            "joint_limit": bool(sp.m_Output_IsJntExd),
        }
        if (
            out is False
            or out.m_Output_IsOutRange
            or out.m_Output_IsJntExd
            or (
                bool(out.m_Output_IsDeg[3])
                if allow_redundant_singular
                else any(out.m_Output_IsDeg)
            )
        ):
            raise ValueError(f"{self.side}: IK unreachable, singular or at joint limit")
        q = np.asarray(out.m_Output_RetJoint.to_list())
        if not self.valid_joints(q):
            raise ValueError(f"{self.side}: joint or coupled wrist limit")
        actual = self.fk(q)
        if np.linalg.norm(actual[:3, 3] - target[:3, 3]) > 0.0005 or angle(
            actual[:3, :3] @ target[:3, :3].T
        ) > math.radians(0.2):
            raise ValueError(f"{self.side}: IK residual too large")
        return q


class Models:
    def __init__(self, config):
        from .tool_frame import validate_tool_config

        tools = validate_tool_config(config)
        tool_collision = config.get("tool_collision", {"left": None, "right": None})
        if not isinstance(tool_collision, dict) or set(tool_collision) != {
            "left",
            "right",
        }:
            raise ValueError("tool_collision must contain left and right")
        mount = ET.parse(MOUNT).getroot()
        self.arms = {}
        for index, side in enumerate(("left", "right")):
            letter = "L" if side == "left" else "R"
            base = origin(mount.find(f"joint[@name='Joint0_{letter}']/origin"))
            self.arms[side] = ArmModel(side, index, base)
        # First load vendor gravity and fixed URDF bases in fixture coordinates;
        # derive the anatomical world frame instead of assuming a CAD yaw angle.
        world_mount = robot_world_from_mount(
            {s: a.base for s, a in self.arms.items()},
            {s: a.gravity for s, a in self.arms.items()},
        )
        self.world_mount = world_mount
        for arm in self.arms.values():
            arm.base = world_mount @ arm.base
            arm.nominal_base = arm.base.copy()
            arm.manufacturer_base = arm.base.copy()
            arm.base_bound = (arm.base, *arm.base_bound[1:])
        self.robot_from_operator = robot_from_behind_operator(self.arms)
        stand = ET.parse(STAND).getroot()
        world_stand = self.arms["left"].base @ inverse(
            origin(stand.find("joint[@name='Joint0_L']/origin"))
        )
        right_check = world_stand @ origin(stand.find("joint[@name='Joint0_R']/origin"))
        delta = inverse(self.arms["right"].base) @ right_check
        if np.linalg.norm(delta[:3, 3]) > 0.001 or angle(delta[:3, :3]) > 0.001:
            raise ValueError("stand and mount assembly transforms disagree")
        model_sides = arm_model_sides(config)
        base_sides = arm_base_sides(config)
        nominal_bases = {side: arm.nominal_base.copy() for side, arm in self.arms.items()}
        self.nominal_bases = nominal_bases
        manufacturer_arms = self.arms
        self.arms = {side: manufacturer_arms[model_side] for side, model_side in model_sides.items()}
        for side, arm in self.arms.items():
            arm.side = side
            arm.nominal_base = nominal_bases[base_sides[side]]
            arm.configure_tool(tools[side])
            arm.configure_tool_collision(tool_collision[side])
        # Register static CAD first. A measured arm mounting rotation must not
        # rotate the stand with it, nor redefine the operator's forward axis.
        mount_orientation = config.get("mount_orientation", "cad")
        mount_by_side = arm_mount_orientations(config)
        for side, arm in self.arms.items():
            arm.base = installed_base_from_nominal(arm.nominal_base, mount_by_side[side])
            arm.base_bound = (arm.base, *arm.base_bound[1:])
        installation = None
        if config.get("installation_calibration"):
            from .installation import resolve_calibration

            bases, installation = resolve_calibration(
                config["installation_calibration"], self.arms
            )
            for side, arm in self.arms.items():
                arm.base = bases[side]
                arm.base_bound = (arm.base, *arm.base_bound[1:])
        self.mapping_report = {
            "installation_id": config.get("installation_id", "nominal"),
            "arm_models": model_sides,
            "arm_base_sides": base_sides,
            "arm_channels": config.get("arm_channels"),
            "arm_model_sources": {s: str(a.path) for s, a in self.arms.items()},
            "operator_alignment": "same_facing",
            "operator_convention": behind_operator_convention(),
            "robot_from_operator_rotation": self.robot_from_operator.tolist(),
            "world_axes": ["forward", "left", "up"],
            "world_from_mount": world_mount.tolist(),
            "world_from_bases": {s: a.base.tolist() for s, a in self.arms.items()},
            "installation_calibration": installation,
            "mount_orientation": mount_orientation,
            "mount_orientation_by_side": mount_by_side,
            "active_mount_source": "measured_calibration"
            if installation
            else ("per_arm_mount_orientation" if config.get('mount_orientation_overrides') else mount_orientation),
            "installed_gravity_in_base": {
                s: (a.base[:3, :3].T @ np.array([0.0, 0.0, -9.81])).tolist()
                for s, a in self.arms.items()
            },
            "nominal_world_from_bases": {
                s: a.nominal_base.tolist() for s, a in self.arms.items()
            },
            "control_points": {
                s: {"name": a.control_point_name, "flange_to_tcp": a.tool.tolist()}
                for s, a in self.arms.items()
            },
            "base_spacing_m": float(
                np.linalg.norm(
                    self.arms["left"].base[:3, 3] - self.arms["right"].base[:3, 3]
                )
            ),
            "stand_registration_position_error_m": float(np.linalg.norm(delta[:3, 3])),
            "stand_registration_rotation_error_rad": angle(delta[:3, :3]),
        }
        self.obstacles = []
        for path, t in ((MOUNT, world_mount), (STAND, world_stand)):
            root = ET.parse(path).getroot()
            col = root.find("link[@name='base_link']/collision")
            mesh = col.find("geometry/mesh")
            vertices = mesh_vertices(
                path.parent.parent / "meshes" / mesh.get("filename").split("/")[-1]
            )
            mt = t @ origin(col.find("origin"))
            vertices = vertices @ mt[:3, :3].T + mt[:3, 3]
            triangles = vertices.reshape(-1, 3, 3)
            self.obstacles.append(StaticMesh(triangles))

    def validate(self):
        from .differential import jacobian

        rng = np.random.default_rng(696)
        report = {}
        for side, arm in self.arms.items():
            max_p = max_r = max_j = 0.0
            for _ in range(40):
                q = rng.uniform(
                    [-40, -70, -50, -100, -40, -30, -30], [40, -15, 50, -25, 40, 30, 30]
                )
                vendor = arm.fk(q)
                urdf = arm.chain(q)[-1] @ arm.flange @ arm.tool
                max_p = max(max_p, np.linalg.norm(vendor[:3, 3] - urdf[:3, 3]))
                max_r = max(max_r, angle(vendor[:3, :3] @ urdf[:3, :3].T))
                max_j = max(
                    max_j,
                    float(
                        np.max(
                            np.abs(
                                arm.world_jacobian(q, vendor) - jacobian(arm, q, vendor)
                            )
                        )
                    ),
                )
                solved = arm.ik(vendor, q)
                if np.max(np.abs(solved - q)) > 0.1:
                    raise ValueError("IK does not reproduce reference configuration")
            if max_p > 0.0001 or max_r > 0.001 or max_j > 0.0001:
                raise ValueError(
                    f"{side}: URDF/vendor mismatch: {max_p} m, {max_r} rad"
                )
            report[side] = {
                "samples": 40,
                "max_position_error_m": max_p,
                "max_rotation_error_rad": max_r,
                "max_jacobian_error": max_j,
            }
        from .mirror import validate_mirror
        from copy import copy

        # Test intrinsic manufacturer symmetry in the nominal mounting; actual
        # independently measured installations need not be mirror symmetric.
        nominal = {arm.model_side: copy(arm) for arm in self.arms.values()}
        for arm in nominal.values():
            arm.base = arm.manufacturer_base
        self.mapping_report["mirror_validation"] = validate_mirror(nominal)
        return report

    def collision_reason(self, joints, margin=0.015):
        # Mesh AABBs in their own link frames; OBB SAT is conservative, no mesh dependencies.
        boxes = {}
        prepared = {}

        def check_overlap(a, b, clearance):
            # Each link participates in many pairs. Transform its bound once
            # per checked configuration; never cache across changing joints.
            for box in (a, b):
                key = id(box)
                if key not in prepared:
                    prepared[key] = prepare_box(box)
            return prepared_overlap(prepared[id(a)], prepared[id(b)], clearance)

        tool_boxes = {}
        for side, arm in self.arms.items():
            boxes[side] = [(t, *bound) for t, bound in zip(arm.chain(joints[side]), arm.link_bounds)]
            if arm.tool_bound is not None:
                tool_boxes[side] = (arm.flange_fk(joints[side]), *arm.tool_bound)
        mesh_boxes = [box for lane in boxes.values() for box in lane] + list(tool_boxes.values())
        mesh_hits = {id(box): False for box in mesh_boxes}
        for obstacle in self.obstacles:
            for box, hit in zip(mesh_boxes, obstacle.intersects_many(mesh_boxes, margin)):
                mesh_hits[id(box)] |= bool(hit)
        for side, arm in self.arms.items():
            # Adjacent links and the short coaxial wrist share housing by design.
            for i in range(len(boxes[side])):
                for j in range(i + 3, len(boxes[side])):
                    if i >= 4 and j >= 4:
                        continue
                    if check_overlap(boxes[side][i], boxes[side][j], margin):
                        return f"{side}: nonadjacent links {i + 1}/{j + 1} too close"
            for i, box in enumerate(boxes[side]):
                for base_side, base_arm in self.arms.items():
                    if base_side == side and i < 2:
                        continue  # The two coaxial shoulder housings share the fixed base envelope.
                    if check_overlap(box, base_arm.base_bound, margin):
                        return f"{side}: link {i + 1} near {base_side} fixed arm base"
                if mesh_hits[id(box)]:
                    return f"{side}: link {i + 1} near mount/stand"
            if arm.tool_bound is not None:
                tool_box = tool_boxes[side]
                # The tool intentionally touches its mounting parent Link7.
                for i, box in enumerate(boxes[side][:-1]):
                    if check_overlap(tool_box, box, margin):
                        return f"{side}: tool near link {i + 1}"
                for base_side, base_arm in self.arms.items():
                    if check_overlap(tool_box, base_arm.base_bound, margin):
                        return f"{side}: tool near {base_side} base"
                if mesh_hits[id(tool_box)]:
                    return f"{side}: tool near mount/stand"
                boxes[side].append(tool_box)
        for i, left in enumerate(boxes["left"]):
            for j, right in enumerate(boxes["right"]):
                if check_overlap(left, right, margin):
                    return f"arms too close: L{i + 1}/R{j + 1}"
        return None


def prepare_box(box):
    t, center, half = box
    rotation = t[:3, :3]
    return rotation, t[:3, 3] + rotation @ center, half, np.abs(rotation) @ half


def prepared_overlap(a, b, margin):
    ra, ca, ha, ea = a
    rb, cb, hb, eb = b
    d = cb - ca
    if np.any(np.abs(d) > ea + eb + margin):
        return False
    axes = np.concatenate(
        (ra.T, rb.T, np.cross(ra.T[:, None, :], rb.T[None, :, :]).reshape(-1, 3))
    )
    lengths = np.linalg.norm(axes, axis=1)
    valid = lengths >= 1e-8
    axes = axes[valid] / lengths[valid, None]
    radii = np.abs(axes @ ra) @ ha + np.abs(axes @ rb) @ hb + margin
    return not bool(np.any(np.abs(axes @ d) > radii))


def overlap(a, b, margin):
    return prepared_overlap(prepare_box(a), prepare_box(b), margin)


class StaticMesh:
    """Triangle surface against an expanded link OBB; broad phase uses vectorized AABBs."""

    def __init__(self, triangles):
        self.triangles = triangles
        self.lower = triangles.min(axis=1)
        self.upper = triangles.max(axis=1)

        def build(indices):
            low, high = self.lower[indices].min(axis=0), self.upper[indices].max(axis=0)
            if len(indices) <= 32:
                return (low, high, indices, None)
            axis = int(np.argmax(high - low))
            order = indices[
                np.argsort((self.lower[indices, axis] + self.upper[indices, axis]) / 2)
            ]
            mid = len(order) // 2
            return (low, high, None, (build(order[:mid]), build(order[mid:])))

        self.tree = build(np.arange(len(triangles)))

    def intersects(self, box, margin):
        return bool(self.intersects_many([box], margin)[0])

    def intersects_many(self, boxes, margin):
        """Same BVH and 13-axis SAT, batched across all links of one pose."""
        result = np.zeros(len(boxes), dtype=bool)
        if not boxes:
            return result
        rotations = np.asarray([box[0][:3, :3] for box in boxes])
        centers = np.asarray([box[0][:3, :3] @ box[1] + box[0][:3, 3] for box in boxes])
        halves = np.asarray([box[2] + margin for box in boxes])
        extents = np.einsum('nij,nj->ni', np.abs(rotations), halves)
        triangle_indices, box_indices = [], []
        for index, (lower, upper) in enumerate(zip(centers-extents, centers+extents)):
            x0, y0, z0 = lower
            x1, y1, z1 = upper
            selected = []
            stack = [self.tree]
            while stack:
                low, high, indices, children = stack.pop()
                if (high[0] < x0 or high[1] < y0 or high[2] < z0
                        or low[0] > x1 or low[1] > y1 or low[2] > z1):
                    continue
                if children is not None:
                    stack.extend(children)
                else:
                    selected.extend(indices)
            triangle_indices.extend(selected)
            box_indices.extend([index] * len(selected))
        if not triangle_indices:
            return result
        bi = np.asarray(box_indices)
        v = np.einsum('nvc,ncd->nvd', self.triangles[triangle_indices]-centers[bi, None, :], rotations[bi])
        edges = np.roll(v, -1, axis=1) - v
        axes = np.concatenate((
            np.broadcast_to(np.eye(3), (len(v), 3, 3)),
            np.cross(edges[:, 0], edges[:, 1])[:, None, :],
            np.cross(edges[:, :, None, :], np.eye(3)[None, None, :, :]).reshape(-1, 9, 3),
        ), axis=1)
        projection = np.einsum('nvc,nac->nav', v, axes)
        radius = np.einsum('nac,nc->na', np.abs(axes), halves[bi])
        separating = (projection.min(axis=2) > radius) | (projection.max(axis=2) < -radius)
        separating &= np.linalg.norm(axes, axis=2) >= 1e-10
        result[bi[~np.any(separating, axis=1)]] = True
        return result
