"""Pure model-copy limit intersection; no ROS, processes or device I/O."""
import math
from xml.etree import ElementTree as ET
from teleop.robots.kuavo5_native_ik import ARM_NAMES


def _arm_joints(xml):
    root=ET.fromstring(xml);joints={}
    for joint in root.findall('joint'):
        name=joint.get('name')
        if name in joints:raise ValueError('duplicate model joint: '+str(name))
        joints[name]=joint
    for name in ARM_NAMES:
        if name not in joints:raise ValueError('missing arm joint: '+name)
        if joints[name].get('type')!='revolute':raise ValueError('bounded revolute joint required')
        limit=joints[name].find('limit')
        if limit is None:raise ValueError('missing limits')
        lower,upper=float(limit.get('lower')),float(limit.get('upper'))
        if not math.isfinite(lower) or not math.isfinite(upper) or lower>=upper:raise ValueError('invalid limits')
    return root,joints


def intersect_arm_limits(ik_xml,control_xml):
    """Return a NEW model string, changing only 14 joint limit intersections.

    Does not widen either model, alter geometry, clip solved joints, or mutate
    the original inputs. Caller owns artifact-only persistence.
    """
    root,ik=_arm_joints(ik_xml);_,control=_arm_joints(control_xml)
    moving={n for n,j in ik.items() if j.get('type')!='fixed'}
    if moving!=set(ARM_NAMES):raise ValueError('IK model must have exactly fourteen moving arm joints')
    changes=[]
    for name in ARM_NAMES:
        limit=ik[name].find('limit');other=control[name].find('limit')
        old=(float(limit.get('lower')),float(limit.get('upper')))
        new=(max(old[0],float(other.get('lower'))),min(old[1],float(other.get('upper'))))
        if new[0]>=new[1]:raise ValueError('empty limit intersection: '+name)
        if new!=old:
            limit.set('lower',str(new[0]));limit.set('upper',str(new[1]))
            changes.append(dict(joint=name,old_rad=list(old),new_rad=list(new)))
    return ET.tostring(root,encoding='unicode'),changes


