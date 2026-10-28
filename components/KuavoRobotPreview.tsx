'use client';
import { MeshRobotPreview } from './MeshRobotPreview';
import { kuavoPreviewAngles, type KuavoGrippers } from '../lib/kuavoPreview';
const VIEW={yaw:Math.PI,pitch:0,zoom:.95};
export function KuavoRobotPreview({joints,grippers,darkMode}:{joints:Record<string,number>;grippers?:KuavoGrippers;darkMode:boolean}) {
  return <MeshRobotPreview modelUrl="/kuavo-model.json" title="乐聚 KUAVO 5-W" darkMode={darkMode} initialView={VIEW}
    anglesForModel={model=>kuavoPreviewAngles(model,joints,grippers)}/>;
}
