"""Retreat from an existing clearance warning without bypassing collisions.

Only separated nonadjacent housings already inside the configured reserve may
start below that reserve. Their separation must recover, never decrease beyond
0.1 mm feedback tolerance. All other pairs keep the normal collision margin.
"""
from copy import copy


class RecoveryClearance:
    tolerance_m = 0.0001

    def __init__(self, models, joints, margin):
        self.models, self.margin = models, margin
        gaps = models.self_clearances(joints)
        self.initial = {key: gap for key, gap in gaps.items() if 0 < gap < margin}
        # Keep a positive floor even for a separation smaller than tolerance.
        self.floors = {key: max(gap / 2, gap - self.tolerance_m) for key, gap in self.initial.items()}

    def fork(self):
        guard = copy(self)
        guard.floors = self.floors.copy()
        return guard

    def reason(self, joints):
        reason = self.models.collision_reason(joints, self.margin, self_clearance_floors=self.floors)
        if reason:
            gaps = self.models.self_clearances(joints)
            for (side, i, j), floor in self.floors.items():
                gap = gaps[(side, i, j)]
                if gap <= floor:
                    return (f'回零会继续挤近：{side} 连杆 {i}/{j} 的保守间距 '
                            f'{gap * 1000:.3f} mm，小于当前恢复下限 {floor * 1000:.3f} mm')
            return '回零路径未通过碰撞余量检查：' + reason
        return None

    def advance(self, joints):
        gaps = self.models.self_clearances(joints)
        for key in self.floors:
            self.floors[key] = max(self.floors[key], min(self.margin, gaps[key] - self.tolerance_m))

    def priority(self, side):
        return min((gap for key, gap in self.initial.items() if key[0] == side), default=self.margin)

    def status(self):
        return [dict(side=side, links=[i, j], initial_clearance_mm=gap * 1000,
                     required_clearance_mm=self.floors[(side, i, j)] * 1000)
                for (side, i, j), gap in sorted(self.initial.items())]
