"""URDF gripper/camera reserves and directional joint-command checks.

Jaw boxes live in their moving URDF links. Fresh motor feedback drives those
links; absent feedback uses the full swept enclosure as a conservative fallback.
Only tools are padded; arm housings retain their CAD bounds.
"""
import math
import xml.etree.ElementTree as ET

import numpy as np

PADDING_M = 0.035
PREFIX = "safety_35mm_"


def normalized_opening(motor, position):
    """Same calibrated physical endpoints as the preview: 0 closed, 1 open."""
    opened, closed = motor["open_rad"], motor["closed_rad"]
    if not all(math.isfinite(v) for v in (opened, closed, position)) or abs(opened - closed) < .01:
        raise ValueError("夹爪开度标定或反馈无效")
    return min(1.0, max(0.0, (position - closed) / (opened - closed)))


def swept_bounds(vertices, joint):
    """Exact coordinate extrema of vertices rotating over a bounded joint arc."""
    from .model import origin
    t = origin(joint.find("origin"))
    if joint.get("type") == "fixed":
        points = vertices @ t[:3, :3].T + t[:3, 3]
        return points.min(axis=0), points.max(axis=0)
    if joint.get("type") != "revolute":
        raise ValueError("unsupported gripper protection joint")
    axis = np.fromstring(joint.find("axis").get("xyz"), sep=" ")
    axis /= np.linalg.norm(axis)
    axial = (vertices @ axis)[:, None] * axis
    a = (vertices - axial) @ t[:3, :3].T
    b = np.cross(axis, vertices) @ t[:3, :3].T
    c = axial @ t[:3, :3].T + t[:3, 3]
    limit = joint.find("limit")
    lo, hi = float(limit.get("lower")), float(limit.get("upper"))
    values = [a * np.cos(v) + b * np.sin(v) + c for v in (lo, hi)]
    low, high = np.minimum(*values), np.maximum(*values)
    phase = np.arctan2(b, a)
    for k in range(-3, 4):
        theta = phase + k * np.pi
        valid = (theta >= lo) & (theta <= hi)
        value = a * np.cos(theta) + b * np.sin(theta) + c
        low = np.minimum(low, np.where(valid, value, np.inf))
        high = np.maximum(high, np.where(valid, value, -np.inf))
    return low.min(axis=0), high.max(axis=0)


def protection_bounds(path, letter, *, swept=False):
    """Local link bounds, or full-travel bounds in the gripper root."""
    from .model import mesh_vertices, origin
    root = ET.parse(path).getroot()
    name = f"gripper_{letter}"
    children = {j.find("child").get("link"): j for j in root.findall("joint")
                if j.find("parent").get("link") == name}
    groups = {}
    for link in root.findall("link"):
        link_name = link.get("name")
        if not link_name.startswith(name):
            continue
        for visual in link.findall("visual"):
            mesh = visual.find("geometry/mesh")
            if mesh is None:
                raise ValueError("gripper protection requires mesh geometry")
            filename = mesh.get("filename")
            vertices = mesh_vertices(path.parent / filename)
            vertices *= np.fromstring(mesh.get("scale", "1 1 1"), sep=" ")
            t = origin(visual.find("origin"))
            vertices = vertices @ t[:3, :3].T + t[:3, 3]
            if link_name == name or not swept:
                low, high = vertices.min(axis=0), vertices.max(axis=0)
            elif link_name in children:
                low, high = swept_bounds(vertices, children[link_name])
            else:
                raise ValueError("unmodelled gripper articulation")
            # CAD manifest identifies part_08 as 摄像头-1@组装2.
            group = f"camera_{letter}" if filename.endswith("/part_08.stl") else link_name
            if group in groups:
                old_low, old_high = groups[group]
                low, high = np.minimum(low, old_low), np.maximum(high, old_high)
            groups[group] = low, high
    if f"camera_{letter}" not in groups or len(groups) != 5:
        raise ValueError("incomplete gripper/camera geometry")
    return {key: ((lo + hi) / 2, (hi - lo) / 2 + PADDING_M)
            for key, (lo, hi) in groups.items()}


def load_protection(path, letter):
    """Fail closed if generated URDF reserves are absent or stale."""
    from .model import origin
    root = ET.parse(path).getroot()
    expected = protection_bounds(path, letter)
    nodes = root.findall("link/collision")
    nodes = {n.get("name"): n for n in nodes if n.get("name", "").startswith(PREFIX)}
    if set(nodes) != {PREFIX + key for key in expected}:
        raise ValueError("夹爪/相机保护体缺失，请运行 scripts/build_tianji_protection.py")
    boxes = []
    for key, (center, half) in expected.items():
        node = nodes[PREFIX + key]
        t = origin(node.find("origin"))
        size = np.fromstring(node.find("geometry/box").get("size"), sep=" ")
        if (size.shape != (3,) or not np.allclose(size / 2, half, atol=1e-9, rtol=0)
                or not np.allclose(t[:3, 3], center, atol=1e-9, rtol=0)
                or not np.allclose(t[:3, :3], np.eye(3), atol=1e-12)):
            raise ValueError("夹爪/相机保护体与当前 CAD 不一致，请重新生成")
        parent = f"gripper_{letter}" if key.startswith("camera_") else key
        if root.find(f"link[@name='{parent}']/collision[@name='{PREFIX + key}']") is None:
            raise ValueError("夹爪保护体必须挂在对应的活动 link 上")
        boxes.append((key, center, half))
    mount = root.find(f"joint[@name='gripper_mount_{letter}']")
    flange = root.find(f"joint[@name='flange_fixed_{letter}']")
    return origin(flange.find("origin")) @ origin(mount.find("origin")), boxes


def pair_clearances(boxes, pairs):
    """Vectorized OBB SAT signed gaps; conservative, not exact mesh distances."""
    from .model import prepare_box
    prepared = [prepare_box(box) for box in boxes]
    r = np.asarray([p[0] for p in prepared])
    c = np.asarray([p[1] for p in prepared])
    h = np.asarray([p[2] for p in prepared])
    ia, ib = np.asarray(pairs).T
    ra, rb = r[ia], r[ib]
    axes = np.concatenate((ra.transpose(0, 2, 1), rb.transpose(0, 2, 1),
        np.cross(ra.transpose(0, 2, 1)[:, :, None, :],
                 rb.transpose(0, 2, 1)[:, None, :, :]).reshape(-1, 9, 3)), axis=1)
    norms = np.linalg.norm(axes, axis=2)
    valid = norms > 1e-8
    axes /= np.where(valid, norms, 1)[:, :, None]
    gap = (np.abs(np.einsum("nki,ni->nk", axes, c[ib] - c[ia]))
           - np.einsum("nki,ni->nk", np.abs(axes @ ra), h[ia])
           - np.einsum("nki,ni->nk", np.abs(axes @ rb), h[ib]))
    return np.max(np.where(valid, gap, -np.inf), axis=1)


class GripperCollisionGuard:
    def __init__(self, arms):
        self.arms = arms
        self.tools = {s: load_protection(a.path, "L" if s == "left" else "R")
                      for s, a in arms.items()}
        self.articulation, self.sweeps, self.drive_limits = {}, {}, {}
        for side, arm in arms.items():
            from .model import origin
            letter = "L" if side == "left" else "R"
            root = ET.parse(arm.path).getroot()
            self.sweeps[side] = {}
            drive = root.find(f"joint[@name='gripper_drive_{letter}']/limit")
            self.drive_limits[side] = float(drive.get("upper"))
            joints = {}
            for node in root.findall("joint"):
                if node.find("parent").get("link") != f"gripper_{letter}":
                    continue
                if node.get("type") == "fixed":
                    continue
                axis = np.fromstring(node.find("axis").get("xyz"), sep=" ")
                mimic = node.find("mimic")
                multiplier = 1 if mimic is None else float(mimic.get("multiplier", "1"))
                offset = 0 if mimic is None else float(mimic.get("offset", "0"))
                joints[node.find("child").get("link")] = (origin(node.find("origin")), axis,
                                                           multiplier, offset)
                name = node.find("child").get("link")
                _, center, half = next(box for box in self.tools[side][1] if box[0] == name)
                signs = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)])
                lo, hi = swept_bounds(center + signs * half, node)
                self.sweeps[side][name] = ((lo + hi) / 2, (hi - lo) / 2)
            self.articulation[side] = joints
        self.names, self.pairs, self.labels = [], [], []
        tools, links, bases = {}, {}, {}
        for side, arm in arms.items():
            links[side] = []
            for i in range(len(arm.link_bounds)):
                links[side].append(len(self.names))
                self.names.append(f"{side}/Link{i + 1}")
            bases[side] = len(self.names)
            self.names.append(f"{side}/base")
            tools[side] = []
            for name, _, _ in self.tools[side][1]:
                tools[side].append(len(self.names))
                self.names.append(name)
        for side in arms:
            peer = "right" if side == "left" else "left"
            # Only the directly attached Link7 is excluded, not the wrist chain.
            obstacles = links[side][:-1] + links[peer] + list(bases.values())
            self.pairs.extend((a, b) for a in tools[side] for b in obstacles)
        self.pairs.extend((a, b) for a in tools["left"] for b in tools["right"])
        self.labels = [f"{self.names[a]} / {self.names[b]}" for a, b in self.pairs]
        self.box_sides = [side for side in arms
                          for _ in range(len(arms[side].link_bounds) + 1 + len(self.tools[side][1]))]
        # Triangle inequality bounds every protected corner's distance from
        # any joint axis, including its flange and tool mounting offsets.
        self.reach = {}
        for side, arm in arms.items():
            mount, tools = self.tools[side]
            outer = max(np.linalg.norm(center) + np.linalg.norm(half)
                        for _, center, half in tools)
            outer = max(outer, *(np.linalg.norm(center) + np.linalg.norm(half)
                                  for center, half in self.sweeps[side].values()))
            self.reach[side] = (sum(np.linalg.norm(t[:3, 3]) for t in arm.origins)
                                + np.linalg.norm(mount[:3, 3]) + outer
                                + max(np.linalg.norm(c) + np.linalg.norm(h)
                                      for c, h in arm.link_bounds))

    def clearances(self, joints, openings=None):
        if set(joints) != set(self.arms):
            raise ValueError("夹爪防碰撞需要双臂最新状态")
        boxes = []
        for side, arm in self.arms.items():
            q = np.asarray(joints[side], dtype=float)
            if q.shape != (7,) or not np.isfinite(q).all():
                raise ValueError("夹爪防碰撞关节状态无效")
            chain = arm.chain(q)
            boxes.extend((t, *bound) for t, bound in zip(chain, arm.link_bounds))
            boxes.append(arm.base_bound)
            mount, tools = self.tools[side]
            opening = (openings or {}).get(side)
            if opening is not None and (not np.isfinite(opening) or not 0 <= opening <= 1):
                raise ValueError("夹爪开度反馈无效")
            for name, center, half in tools:
                t = chain[-1] @ mount
                if name in self.articulation[side]:
                    if opening is None:
                        center, half = self.sweeps[side][name]
                    else:
                        from .geometry import rotation
                        origin, axis, multiplier, offset = self.articulation[side][name]
                        r = np.eye(4)
                        r[:3, :3] = rotation(axis, opening * self.drive_limits[side] * multiplier + offset)
                        t = t @ origin @ r
                boxes.append((t, center, half))
        gaps = pair_clearances(boxes, self.pairs)
        if not np.isfinite(gaps).all():
            raise ValueError("夹爪防碰撞计算无效")
        return gaps

    def reason(self, start, goal, openings=None, goal_openings=None):
        """Check the intervening path, allowing hold or monotonically outward escape.

        Far-separated paths use a conservative travel bound. Near reserves we
        subdivide to at most 0.1 degree per joint; every pair keeps its own floor
        so moving away from one obstacle cannot conceal approach to another.
        """
        openings = dict(openings or {})
        goal_openings = openings if goal_openings is None else goal_openings
        previous = self.clearances(start, openings)
        escaping = previous <= 1e-7
        delta = {s: np.asarray(goal[s]) - np.asarray(start[s]) for s in start}
        opening_delta = {s: goal_openings.get(s, openings.get(s)) - openings[s]
                         for s in openings if openings[s] is not None}
        if any(goal_openings.get(s) is not None and openings.get(s) is None for s in start):
            raise ValueError("夹爪运动缺少当前开度反馈")
        largest = max(float(np.max(np.abs(v))) for v in delta.values())
        largest = max(largest, *(abs(v) * math.degrees(self.drive_limits[s])
                                for s, v in opening_delta.items())) if opening_delta else largest
        if largest == 0:
            return None  # A measured hold is always available, even inside a reserve.
        # Two moving objects can belong to the same arm, hence factor two.
        object_travel = []
        for name, side in zip(self.names, self.box_sides):
            travel = self.reach[side] * float(np.sum(np.abs(np.radians(delta[side]))))
            if name in self.articulation[side]:
                _, axis, multiplier, _ = self.articulation[side][name]
                _, center, half = next(t for t in self.tools[side][1] if t[0] == name)
                travel += ((np.linalg.norm(center) + np.linalg.norm(half))
                           * abs(opening_delta.get(side, 0) * self.drive_limits[side] * multiplier))
            object_travel.append(travel)
        travel = np.asarray([object_travel[a] + object_travel[b] for a, b in self.pairs])
        if np.all((previous > travel) | (travel == 0)):
            return None
        count = max(1, math.ceil(largest / 0.1))
        if count > 3600:
            raise ValueError("夹爪防碰撞指令步长过大")
        def at(u):
            o = {s: value + u * opening_delta[s] if value is not None else None
                 for s, value in openings.items()}
            return self.clearances({s: np.asarray(start[s]) + u * delta[s] for s in start}, o)

        def interval(a, b, previous, current, depth=0):
            # Keep an escape monotonic for the entire command, including the
            # instant it crosses out of the reserve. Otherwise certification
            # near a zero gap can incorrectly block a valid outward move.
            floors = np.where(escaping, previous, np.minimum(previous, 0.0))
            bad = current < floors - 1e-9
            if np.any(bad):
                index = int(np.flatnonzero(bad)[0])
                return f"夹爪/相机 3.5 cm 防碰撞：{self.labels[index]}，只能远离，不能继续靠近"
            # Certify separated pairs cannot tunnel through between samples:
            # an endpoint's separating plane remains valid if its gap exceeds
            # the maximum travel of both objects over this interval.
            uncertain = (~escaping & (previous > 0) & (current > 0)
                         & (np.maximum(previous, current) <= travel * (b - a)))
            if np.any(uncertain):
                if depth >= 20:
                    return "夹爪/相机已到 3.5 cm 保护边界，请向远离方向移动"
                mid = (a + b) / 2
                middle = at(mid)
                return (interval(a, mid, previous, middle, depth + 1)
                        or interval(mid, b, middle, current, depth + 1))
            return None

        for index in range(count):
            a, b = index / count, (index + 1) / count
            current = at(b)
            reason = interval(a, b, previous, current)
            if reason:
                return reason
            previous = current
        return None


def motion_reason(models, start, goal):
    """Shared by manual, preparation, recovery and teleoperation planners."""
    return models.gripper_guard.reason(start, goal, getattr(models, "gripper_openings", None))
