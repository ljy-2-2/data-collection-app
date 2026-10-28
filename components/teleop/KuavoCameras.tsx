'use client';
import {useRef,useState} from 'react';
import {ActionButton} from '../CollectionUi';
import {requestJson} from '../../lib/http';
import {TianjiCameraSettings,type CameraSettings} from './TianjiCameraSettings';

type Profile={width:number;height:number;brightness?:number|null};
type Config={transport:'network'|'auto'|'ros'|'ssh';host:string;user:string;ros_master:string;ros_setup:string;roles:Record<string,string>;profiles?:Record<string,Profile>};
export type KuavoCameraStatus={config:Config;sources:{id:string;label:string;role_hint?:string;kind?:string;capabilities?:{resolutions:Profile[];brightness?:{min:number;max:number;step:number;default?:number;value:number}}}[];warnings:string[];error:string;reconnecting?:boolean;stream_id?:string;cameras:{id:string;label:string;connected:boolean;state:string;error:string;width?:number;height?:number;settings?:Profile}[]};
const defaults:Config={transport:'network',host:'192.168.26.22',user:'jt',ros_master:'http://192.168.26.1:11311',ros_setup:'/opt/ros/noetic/setup.bash',roles:{}};
const roles=[{id:'head',label:'主相机'},{id:'left',label:'左相机'},{id:'right',label:'右相机'}];
function cameraSettings(status:KuavoCameraStatus,role:string,readback=false):CameraSettings {
  const caps=status.sources.find(s=>s.id===status.config.roles[role])?.capabilities;
  const camera=status.cameras.find(c=>c.id===role),profile=status.config.profiles?.[role];
  const width=camera?.width||profile?.width||640,height=camera?.height||profile?.height||480;
  const selected={id:`${width}x${height}`,label:`${width} × ${height}`};
  const options=caps?.resolutions.map(s=>({id:`${s.width}x${s.height}`,label:`${s.width} × ${s.height}`}))||[];
  return {resolution:{options:options.length?options:[selected],selected,editable:!!options.length},
    brightness:caps?.brightness?{...caps.brightness,supported:true,default:caps.brightness.default??0,
      value:readback?(camera?.settings?.brightness??caps.brightness.value):caps.brightness.value}:{supported:false}};
}
export function KuavoCameras({baseUrl,status,onChange,editable=true}:{baseUrl:string;status?:KuavoCameraStatus;onChange:(s:KuavoCameraStatus)=>void;editable?:boolean}) {
  const [opened,setOpened]=useState(false),[error,setError]=useState('');
  const [operations,setOperations]=useState<Record<string,boolean>>({});
  const inventory=useRef<Promise<KuavoCameraStatus>|null>(null);
  const current=useRef(status);current.current=status;
  const busy=!!status?.reconnecting||Object.values(operations).some(Boolean);
  async function load(role:string,signal:AbortSignal) {
    if(!inventory.current)inventory.current=requestJson<KuavoCameraStatus>(baseUrl,'/kuavo/cameras/discover',{
      method:'POST',body:JSON.stringify(current.current?.config||defaults),signal,
    }).then(value=>{onChange(value);return value});
    return cameraSettings(await inventory.current,role);
  }
  async function apply(changes:Record<string,{mode_id?:string;brightness?:number}>) {
    const source=await inventory.current;
    if(!source)throw new Error('请先读取相机参数');
    const profiles=Object.fromEntries(Object.entries(changes).map(([role,change])=>{
      const settings=cameraSettings(source,role);
      const [width,height]=(change.mode_id||settings.resolution.selected.id).split('x').map(Number);
      return [role,{width,height,brightness:change.brightness??(settings.brightness.supported?settings.brightness.value:null)}];
    }));
    const result=await requestJson<KuavoCameraStatus>(baseUrl,'/kuavo/cameras/settings',{method:'POST',body:JSON.stringify(profiles)});
    onChange(result);
    return Object.fromEntries(Object.keys(changes).map(role=>[role,cameraSettings(result,role,true)]));
  }
  return <section className="surface teleop-camera">
    <div className="preview-head"><strong>相机画面</strong><div style={{display:'flex',gap:6}}>
      <ActionButton kind="secondary" disabled={busy} onClick={()=>{inventory.current=null;setError('');setOpened(true)}}>相机参数</ActionButton>
    </div></div>
    <div className="kuavo-camera-grid">{roles.map(({id:role,label})=>{const camera=status?.cameras.find(c=>c.id===role);return <div key={role} className="kuavo-camera-tile">
      {camera?.connected?<img alt={label} src={`${baseUrl}/kuavo/cameras/${role}/stream?v=${status?.stream_id||''}`}/>:<span className="kuavo-camera-offline">{status?.reconnecting?'连接中…':camera?.state==='error'?'连接异常':camera?.state==='waiting'?'连接中…':'未连接'}</span>}
      <span className="teleop-camera-name">{role==='head'?'头部主相机':label}</span>
    </div>})}</div>
    {error&&<p role="alert">{error}</p>}
    <TianjiCameraSettings baseUrl={baseUrl} open={opened} onClose={()=>setOpened(false)} cameras={status?.cameras||[]} editable={editable}
      cameraRoles={roles} loadSettings={load} applySettings={apply}
      onBusyChange={(key,value)=>setOperations(current=>({...current,[key]:value}))}
      onApplyStatus={(_message,failure)=>setError(failure)}/>
  </section>;
}
