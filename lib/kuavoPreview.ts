import type { TianjiModel } from './tianjiModel';

export type KuavoGrippers = Partial<Record<'left'|'right', {closure_fraction:number}>>;

export function kuavoPreviewAngles(model:TianjiModel, joints:Record<string,number>, grippers:KuavoGrippers={}) {
  const names=new Set(model.joints.filter(j=>['revolute','continuous'].includes(j.type)&&!j.name.startsWith('kuavo_tool_')).map(j=>j.name));
  const angles={...model.display_joint_defaults,...Object.fromEntries(Object.entries(joints).filter(([key,value])=>names.has(key)&&Number.isFinite(value)))};
  for (const [side,letter] of [['left','L'],['right','R']] as const) {
    const amount=grippers[side]?.closure_fraction;
    const drive=model.joints.find(j=>j.name===`kuavo_tool_gripper_drive_${letter}`);
    if (typeof amount==='number' && Number.isFinite(amount) && drive?.limit)
      angles[drive.name]=(1-Math.max(0,Math.min(1,amount)))*Number(drive.limit.upper);
  }
  return angles;
}
