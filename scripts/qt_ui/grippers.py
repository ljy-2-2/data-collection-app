"""Use the same calibrated endpoints as the native control worker."""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'teleop_control/src'))
from teleop_control.tianji_models import TianjiParameters


def parameter_defaults():
    return TianjiParameters().model_dump()


def endpoints(root: Path):
    path=root/'teleop_control/vendor/tianji/teleop/gripper/config.json'
    try:motors=json.loads(path.read_text())['motors']
    except (OSError,ValueError,KeyError,TypeError):return {}
    result={}
    for side in ('left','right'):
        try:lo,hi=float(motors[side]['open_rad']),float(motors[side]['closed_rad'])
        except (ValueError,KeyError,TypeError):continue
        if math.isfinite(lo) and math.isfinite(hi) and abs(hi-lo)>=.01:result[side]=(lo,hi)
    return result

def closure(status,side,calibration):
    feedback=(status.get('grippers') or {}).get('motors',{}).get(side,{}).get('feedback',{})
    position=feedback.get('position_rad');age=feedback.get('age_s')
    if side not in calibration or not isinstance(position,(float,int)) or not math.isfinite(position) or not isinstance(age,(float,int)) or not 0<=age<.3:return None
    lo,hi=calibration[side]
    return max(0,min(1,(position-lo)/(hi-lo)))
