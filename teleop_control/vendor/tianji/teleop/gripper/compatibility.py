"""Common DM-J4310-2EC protocol: capabilities come from replies, not version labels."""
import math

# Exact CAN identity captured in diagnostics/gripper_versions_20260921.json.
# The user supplied a UART capture for this V3.0 / 5017 / 005 board with
# Imax=10.261194 A. Do not generalize this zero-register workaround to every
# motor sold as V1.1, or to unrecognized hardware/firmware combinations.
IMAX_PROFILES = {
    (0x56303033, 0x37313035, 0x00000035): {
        "imax_a": 10.261194,
        "name": "DMBOT V3.0 / firmware 5017 / sub 005",
        "source": "CAN identity matched; UART-confirmed Imax=10.261194 A",
    },
}


def current_limit(motor, raw_imax, identity=None):
    """Resolve current scale by telemetry and exact device identity, never side."""
    imax, source = raw_imax, "CAN register 0x3B"
    profile = IMAX_PROFILES.get(tuple(identity)) if identity is not None else None
    if raw_imax == 0 and profile is not None:
        imax, source = profile["imax_a"], profile["source"]
    if type(imax) not in (int, float) or not math.isfinite(imax) or imax <= 0:
        raise ValueError(
            f"invalid Imax: register 0x3B returned {raw_imax!r} A; "
            f"identity={identity!r}; cannot enforce phase-current limit. "
            "未识别到可用的电流量程；请保留只读检查结果以补充驱动兼容信息"
        )
    fraction = min(motor["current_fraction"], motor["max_current_a"] / imax)
    if fraction < 0.0001:
        raise ValueError("current limit below protocol resolution")
    return {"raw_imax_a": raw_imax, "imax_a": imax, "imax_source": source,
            "identity_registers": dict(zip(("hardware", "firmware", "sub_version"), identity))
            if identity is not None else None,
            "matched_profile": profile["name"] if raw_imax == 0 and profile else None,
            "current_fraction": fraction,
            "effective_current_limit_a": int(fraction * 10000) / 10000 * imax}


def voltage_telemetry(raw):
    """Zero is observed on communicating units: unknown telemetry, never 'healthy 0V'.

    Motor state feedback is independently required before enable and throughout
    motion. Invalid/nonzero out-of-range readings remain errors for this 24V setup.
    """
    if raw == 0:
        return {'raw_v': raw, 'voltage_v': None, 'status': 'unavailable',
                'warning': 'VBus(0x3C)返回0，电压遥测不可用；不代表实际电压为0或供电已验证。运动依赖有效电机状态反馈及硬件故障保护。'}
    if not math.isfinite(raw) or not 15 < raw < 32:
        raise RuntimeError(f'voltage register 0x3C returned invalid/out-of-range {raw!r} V (24V setup requires 15<VBus<32)')
    return {'raw_v': raw, 'voltage_v': raw, 'status': 'available', 'warning': None}
