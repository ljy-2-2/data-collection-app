'use client';
import { MeshRobotPreview } from './MeshRobotPreview';
const VIEW={yaw:Math.PI,pitch:0,zoom:.95};
export function KuavoRobotPreview({joints,darkMode}:{joints:Record<string,number>;darkMode:boolean}) {
  return <MeshRobotPreview modelUrl="/kuavo-model.json" title="乐聚 KUAVO 5-W" darkMode={darkMode} initialView={VIEW}
    anglesForModel={model=>({...model.display_joint_defaults,...Object.fromEntries(Object.entries(joints).filter(([key,value])=>/^zarm_[lr][1-7]_joint$/.test(key)&&Number.isFinite(value)))})}/>;
}
