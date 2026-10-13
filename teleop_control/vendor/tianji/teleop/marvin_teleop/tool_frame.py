"""Rigid flange-to-TCP geometry. No gripper actuation or vendor tool mutation."""

import numpy as np
from .geometry import transform


def parse_tool_frame(value):
    if value is None:
        return np.eye(4), "bare_flange"
    if not isinstance(value, dict) or set(value) != {"name", "xyz_mm", "rpy_deg"}:
        raise ValueError("tool_tcp 每侧需包含 name、xyz_mm、rpy_deg；未确定时使用 null")
    if not isinstance(value["name"], str) or not value["name"].strip():
        raise ValueError("tool_tcp.name must identify the physical control point")
    for key in ("xyz_mm", "rpy_deg"):
        a = value[key]
        if (
            not isinstance(a, list)
            or len(a) != 3
            or any(type(x) not in (int, float) or not np.isfinite(x) for x in a)
        ):
            raise ValueError(f"tool_tcp.{key} must contain three finite numbers")
    if (
        np.linalg.norm(value["xyz_mm"]) > 500
        or max(abs(x) for x in value["rpy_deg"]) > 180
    ):
        raise ValueError("tool_tcp offset must be <=500 mm and rpy within ±180 degrees")
    return transform(
        np.array(value["xyz_mm"]) / 1000, np.radians(value["rpy_deg"])
    ), value["name"]


def validate_tool_config(config, base_dir=None):
    value = config.get("tool_tcp", {"left": None, "right": None})
    if not isinstance(value, dict) or set(value) != {"left", "right"}:
        raise ValueError("tool_tcp must explicitly contain left and right")
    result={}
    for side,definition in value.items():
        if isinstance(definition,dict) and 'urdf' in definition:
            definition=resolve_urdf_tool(definition,base_dir)
        parse_tool_frame(definition)
        result[side]=definition
    return result


def resolve_urdf_tool(value, base_dir=None):
    """Read a named rigid TCP from URDF. Never resolve through actuated joints."""
    from pathlib import Path
    import xml.etree.ElementTree as ET
    import math
    required={"urdf", "flange_link", "tcp_link"}
    if not isinstance(value,dict) or set(value)!=required:
        raise ValueError('URDF tool requires urdf, flange_link, tcp_link')
    if any(not isinstance(value[k],str) or not value[k].strip() for k in required):
        raise ValueError('URDF tool paths and link names must be nonempty strings')
    path=Path(value['urdf']).expanduser()
    if not path.is_absolute():path=Path(base_dir or Path(__file__).parent)/path
    root=ET.parse(path).getroot()
    links=[n.get('name') for n in root.findall('link')]
    if len(set(links))!=len(links):raise ValueError('Duplicate URDF link names')
    if value['flange_link'] not in links or value['tcp_link'] not in links:
        raise ValueError('URDF does not contain selected flange/TCP links')
    parents={}
    for j in root.findall('joint'):
        child=j.find('child').get('link')
        if child in parents:raise ValueError('URDF link has multiple parent joints')
        parents[child]=j
    current=value['tcp_link'];chain=[];seen=set()
    while current!=value['flange_link']:
        if current in seen or current not in parents:
            raise ValueError('TCP is not a descendant of flange through a valid URDF chain')
        seen.add(current);joint=parents[current]
        if joint.get('type')!='fixed':
            raise ValueError('Selected TCP is not fixed to flange; moving jaw links cannot be fixed tools')
        o=joint.find('origin')
        xyz=np.zeros(3) if o is None else np.fromstring(o.get('xyz','0 0 0'),sep=' ')
        rpy=np.zeros(3) if o is None else np.fromstring(o.get('rpy','0 0 0'),sep=' ')
        if xyz.shape!=(3,) or rpy.shape!=(3,) or not np.isfinite(np.r_[xyz,rpy]).all():
            raise ValueError('Invalid URDF tool origin')
        chain.append(transform(xyz,rpy));current=joint.find('parent').get('link')
    T=np.eye(4)
    for part in reversed(chain):T=T@part
    R=T[:3,:3];pitch=math.asin(float(np.clip(-R[2,0],-1,1)))
    if abs(math.cos(pitch))>1e-8:
        roll=math.atan2(R[2,1],R[2,2]);yaw=math.atan2(R[1,0],R[0,0])
    else:
        roll=0.;yaw=math.atan2(-R[0,1],R[1,1])
    resolved=dict(name=value['tcp_link'],xyz_mm=(T[:3,3]*1000).tolist(),
                  rpy_deg=np.degrees([roll,pitch,yaw]).tolist())
    check,_=parse_tool_frame(resolved)
    if not np.allclose(check,T,atol=1e-9):raise ValueError('URDF tool rotation conversion failed')
    return resolved
