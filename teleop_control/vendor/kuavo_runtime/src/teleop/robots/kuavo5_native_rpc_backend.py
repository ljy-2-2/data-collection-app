"""Translate a deliberately injected FK/IK RPC into the native-IK solver protocol.

Importing or constructing this class never starts ROS, SSH or a robot controller.
The RPC object must expose only `fk` and `ik` compute operations.
"""

import math
from collections.abc import Mapping

from teleop.contracts.intent import CartesianPose
from teleop.robots.kuavo5_diagnostics import ComputeResponseRejected


class KuavoNativeRPCBackend:
    def __init__(self, rpc, *, frame_id: str):
        if not callable(getattr(rpc, "call", None)):
            raise ValueError("an explicit FK/IK RPC is required")
        if not isinstance(frame_id, str) or not frame_id.strip():
            raise ValueError("the audited FK/IK frame_id is required")
        self._rpc = rpc
        self._frame_id = frame_id

    def forward(self, q_arm: tuple[float, ...]):
        values = tuple(q_arm)
        if len(values) != 14 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                    or not math.isfinite(v) for v in values):
            raise ValueError("FK requires 14 finite arm radians")
        response = self._rpc.call("fk", {"q": list(map(float, values))})
        if not isinstance(response, Mapping) or response.get("success") is not True:
            raise ValueError("official FK failed")
        hands = response.get("hand_poses")
        if not isinstance(hands, Mapping) or set(hands) != {"left_pose", "right_pose"}:
            raise ValueError("official FK must return both hand poses")
        try:
            for hand in hands.values():
                quaternion = hand['quat_xyzw']
                if len(quaternion) != 4 or any(
                        isinstance(v, bool) or not isinstance(v, (int, float))
                        or not math.isfinite(v) for v in quaternion) \
                        or abs(sum(v*v for v in quaternion)-1.) > 1e-5:
                    raise ValueError('official FK requires unit quaternion')
            return {side: CartesianPose(self._frame_id, hands[side + "_pose"]["pos_xyz"],
                                        hands[side + "_pose"]["quat_xyzw"])
                    for side in ("left", "right")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("official FK returned an invalid hand pose") from exc

    def inverse(self, request: dict):
        if not isinstance(request, dict):
            raise ValueError("official IK request must be a mapping")
        response = self._rpc.call("ik", request)
        if not isinstance(response, Mapping):
            raise ComputeResponseRejected("official IK returned an invalid response", response)
        return dict(response)
