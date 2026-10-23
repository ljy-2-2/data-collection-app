'use client';
import { MeshRobotPreview } from './MeshRobotPreview';
import type { TianjiSide } from '../lib/tianji';

export function TianjiRobotPreview({joints,darkMode,closure}:{
  joints:Partial<Record<TianjiSide,number[]>>; darkMode:boolean;
  closure:Partial<Record<TianjiSide,number>>;
}) {
  return <MeshRobotPreview modelUrl="/tianji-model.json" title="天机 Marvin M6" darkMode={darkMode}
    anglesForModel={model=>{
      const angles:Record<string,number>={};
      for(const [side,letter] of [['left','L'],['right','R']] as const) {
        (joints[side]??Array(7).fill(0)).forEach((v,i)=>{angles[`Joint${i+1}_${letter}`]=v*Math.PI/180});
        const drive=model.joints.find(j=>j.name===`gripper_drive_${letter}`);
        angles[`gripper_drive_${letter}`]=(1-(closure[side]??.5))*Number(drive?.limit?.upper??1);
      }
      return angles;
    }}/>
}
