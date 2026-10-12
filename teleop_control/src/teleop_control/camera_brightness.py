"""Hardware brightness controls shared by preview and recorded camera frames."""
from __future__ import annotations

import re
import subprocess


def _control(device: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--device", device, *arguments],
            capture_output=True, text=True, check=False, timeout=2.0,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("亮度调节需要 v4l2-ctl，请检查软件运行环境") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("相机亮度操作未完成，请检查相机连接") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"相机亮度操作失败：{detail or '设备不可用'}")
    return result.stdout


def read_brightness(device: str) -> dict:
    """Discover the real driver range; never assume every camera uses -64..64."""
    output = _control(device, "--list-ctrls")
    line = next((line for line in output.splitlines()
                 if re.match(r"^\s*brightness\s+", line)), "")
    fields = {key: int(value) for key, value in re.findall(
        r"\b(min|max|step|default|value)=(-?\d+)", line
    )}
    if (set(fields) != {"min", "max", "step", "default", "value"}
            or fields["step"] <= 0 or fields["min"] >= fields["max"]
            or any(flag in line for flag in ("inactive", "read-only", "disabled"))):
        return {"supported": False, "message": "该相机暂不支持亮度调节"}
    return {"supported": True, **fields}


def set_brightness(device: str, value: int) -> dict:
    control = read_brightness(device)
    if not control["supported"]:
        raise RuntimeError(control["message"])
    if (isinstance(value, bool) or not isinstance(value, int)
            or not control["min"] <= value <= control["max"]
            or (value - control["min"]) % control["step"]):
        raise ValueError(
            f"亮度须为 {control['min']} 到 {control['max']} 之间、步长为 {control['step']} 的整数"
        )
    _control(device, "--set-ctrl", f"brightness={value}")
    result = _control(device, "--get-ctrl=brightness")
    match = re.search(r"(?:^|\n)\s*brightness\s*:\s*(-?\d+)\s*(?:\n|$)", result)
    if match is None:
        raise RuntimeError("相机未返回亮度确认值，请重新读取")
    actual = int(match.group(1))
    if actual != value:
        raise RuntimeError(f"相机实际亮度为 {actual}，未能应用请求值 {value}")
    return {**control, "value": actual}
