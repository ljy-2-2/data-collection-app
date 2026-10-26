'use client';
import { useEffect, useRef, useState } from 'react';
import { ActionButton } from '../CollectionUi';
import { KuavoRobotPreview } from '../KuavoRobotPreview';
import { requestJson } from '../../lib/http';

type GripperValues = {open_rad:number;closed_rad:number;speed_rad_s:number;accel_rad_s2:number};
type Grippers = Record<'left'|'right',GripperValues>;
const gripperFields = [
  {key:'open_rad',label:'张开位置',unit:'rad',min:-3.12,max:3.12,step:.01},
  {key:'closed_rad',label:'闭合位置',unit:'rad',min:-3.12,max:3.12,step:.01},
  {key:'speed_rad_s',label:'速度',unit:'rad/s',min:.01,max:15,step:.1},
  {key:'accel_rad_s2',label:'加速度',unit:'rad/s²',min:.01,max:200,step:1},
] as const;
function gripperDraft(source?:Record<string,Record<string,number>>): Grippers {
  return Object.fromEntries(['left','right'].map(side=>[side,Object.fromEntries(gripperFields.map(f=>[f.key,source?.[side]?.[f.key]??0]))])) as Grippers;
}
type Motion = {position_scale:number;max_velocity_rad_s:number;planning_acceleration_rad_s2:number};
const defaultMotion:Motion = {position_scale:1,max_velocity_rad_s:.7,planning_acceleration_rad_s2:5};
type Status = {
  motion_settings?:{values:Motion;saved:boolean;supported:boolean;message:string;file:string;error?:string};
  gripper_settings?:{values:Grippers;saved:boolean;matches_service:boolean;message:string;file:string};
  state: string; active?: boolean; session_id?: string; error?: string;
  service?: { management_ready?: boolean; service?: { ready_to_start?: boolean } };
  reference?: {max_velocity_rad_s:number;planning_acceleration_rad_s2:number;grippers:Record<string,Record<string,number>>};
  telemetry?: {joints?:Record<string,number>}; pose_live?: boolean; pose_available?: boolean; pico_live?: boolean;
  vr_checked?:boolean;vr_ready?:boolean;pico_ready?:boolean;vr_detail?:string;pico_detail?:string;
  pico_sdk_available?: boolean; preparation?: Record<string, unknown>;
  log_directory?: string; limitations?: string[]; logs?: string[];
};
export function KuavoTeleopWorkspace({baseUrl, task, onBusyChange, darkMode=false}: {
  darkMode?:boolean; baseUrl: string; task: string; onBusyChange?: (busy: boolean) => void;
}) {
  const [status, setStatus] = useState<Status>({state:'idle'});
  const [action, setAction] = useState('');
  const [error, setError] = useState('');
  const [armMode,setArmMode] = useState('dual');
  const [motion,setMotion] = useState<Motion>(defaultMotion);
  const motionLoaded=useRef(false);
  const latestMotion=useRef(motion);latestMotion.current=motion;
  const savedMotion=status.motion_settings?.values||defaultMotion;
  const motionDirty=(Object.keys(defaultMotion) as (keyof Motion)[]).some(key=>motion[key]!==savedMotion[key]);
  useEffect(()=>{motionLoaded.current=false;setMotion(defaultMotion)},[baseUrl]);
  useEffect(()=>{if(status.motion_settings&&!motionLoaded.current){setMotion(status.motion_settings.values);motionLoaded.current=true}},[status.motion_settings]);
  const [parametersOpen,setParametersOpen] = useState(false);
  const [gripperDraftValues,setGripperDraftValues]=useState<Grippers>(()=>gripperDraft());
  const [grippers,setGrippers] = useState<Grippers>(()=>gripperDraft());
  const [settingsError,setSettingsError] = useState('');
  const [gripperSaving,setGripperSaving]=useState(false);
  const [gripperEdited,setGripperEdited]=useState(false);
  const gripperInFlight=useRef(false);
  const latestGrippers=useRef(grippers);latestGrippers.current=grippers;
  const [motionSaving,setMotionSaving]=useState(false);
  const [motionFailure,setMotionFailure]=useState('');
  const motionInFlight=useRef(false);
  const revision = useRef(0);
  const active = !!status.active || ['starting','running','stopping','unknown'].includes(status.state);
  const locked = !!action || active || status.state === 'checking';
  const motionValid=Number.isFinite(motion.position_scale)&&motion.position_scale>=.1&&motion.position_scale<=1&&Number.isFinite(motion.max_velocity_rad_s)&&motion.max_velocity_rad_s>=.01&&motion.max_velocity_rad_s<=.7&&Number.isFinite(motion.planning_acceleration_rad_s2)&&motion.planning_acceleration_rad_s2>=.01&&motion.planning_acceleration_rad_s2<=5;
  const savedGrippers=gripperDraft(status.gripper_settings?.values||status.reference?.grippers);
  const gripperDirty=gripperEdited&&JSON.stringify(grippers)!==JSON.stringify(savedGrippers);
  const gripperValid=(['left','right'] as const).every(side=>gripperFields.every(field=>Number.isFinite(grippers[side][field.key])&&grippers[side][field.key]>=field.min&&grippers[side][field.key]<=field.max));
  const gripperDraftValid=(['left','right'] as const).every(side=>gripperFields.every(field=>Number.isFinite(gripperDraftValues[side][field.key])&&gripperDraftValues[side][field.key]>=field.min&&gripperDraftValues[side][field.key]<=field.max));
  const parametersBusy=motionDirty||motionSaving||gripperDirty||gripperSaving;
  useEffect(()=>{
    if(parametersOpen||!gripperDirty||!gripperValid||locked||motionSaving||gripperSaving||settingsError)return;
    const timer=setTimeout(()=>{
      if(motionInFlight.current||gripperInFlight.current)return;
      gripperInFlight.current=true;setGripperSaving(true);++revision.current;
      void requestJson<Status>(baseUrl,'/kuavo/grippers',{method:'POST',body:JSON.stringify(grippers)})
        .then(value=>setStatus(current=>({...current,gripper_settings:value.gripper_settings})))
        .catch(error=>{if(JSON.stringify(latestGrippers.current)===JSON.stringify(grippers))setSettingsError(String(error))})
        .finally(()=>{++revision.current;gripperInFlight.current=false;setGripperSaving(false)});
    },350);
    return ()=>clearTimeout(timer);
  },[baseUrl,grippers,gripperDirty,gripperValid,locked,motionSaving,gripperSaving,settingsError,parametersOpen]);
  useEffect(() => { onBusyChange?.(locked||parametersBusy); }, [locked, parametersBusy, onBusyChange]);
  useEffect(()=>{
    if(!motionLoaded.current||!motionDirty||!motionValid||locked||motionSaving||gripperSaving||motionFailure)return;
    const timer=setTimeout(()=>{
      if(motionInFlight.current||gripperInFlight.current)return;
      motionInFlight.current=true;setMotionSaving(true);++revision.current;
      void requestJson<Status>(baseUrl,'/kuavo/motion',{method:'POST',body:JSON.stringify(motion)})
        .then(value=>setStatus(current=>({...current,motion_settings:value.motion_settings})))
        .catch(error=>{if((Object.keys(defaultMotion) as (keyof Motion)[]).every(key=>latestMotion.current[key]===motion[key]))setMotionFailure(String(error))})
        .finally(()=>{++revision.current;motionInFlight.current=false;setMotionSaving(false)});
    },350);
    return ()=>clearTimeout(timer);
  },[baseUrl,motion,motionDirty,motionValid,locked,motionSaving,gripperSaving,motionFailure]);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const current = revision.current;
      try {
        const value = await requestJson<Status>(baseUrl, '/kuavo/status');
        if (!cancelled && current === revision.current && !motionInFlight.current && !gripperInFlight.current) setStatus(value);
      } catch (e) {
        if (!cancelled && current === revision.current) setStatus(s => ({...s,state:'unknown',error:String(e),pose_live:false,pico_live:false,vr_ready:false,pico_ready:false}));
      } finally { if (!cancelled) timer = setTimeout(poll, 500); }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl]);
  async function command(name: string, preparation?: string) {
    if (name !== 'stop' && locked) return;
    if(name!=='stop'&&parametersBusy){if(name==='check'){setMotionFailure('');setSettingsError('')}return}
    const id = ++revision.current;
    let motorIds = false;
    if (name === 'start' || name === 'prepare') {
      if (!window.confirm(preparation === 'grippers_inspect' ? '读取会发送夹爪查询帧，不改写参数。继续？' : '此操作可能启动控制或产生动作。确认现场已按厂家流程准备，并执行本次操作？')) return;
      if (preparation === 'start') {
        motorIds = window.confirm('已确认左电机 8、右电机 1、反馈编号 0？');
        if (!motorIds) return;
      }
    }
    setAction(name); setError('');
    const body = name === 'stop' ? {session_id:status.session_id} : {
      task, network_namespace:'current', arm_mode:armMode, position_scale:motion.position_scale, confirm_motion:name==='start'||(name==='prepare'&&preparation!=='grippers_inspect'),
      accept_unverified_gripper_bus:true, motor_ids_confirmed:motorIds,
      ...(preparation ? {preparation_action:preparation} : {}),
    };
    try {
      const value = await requestJson<Status>(baseUrl, '/kuavo/'+name, {method:'POST',body:JSON.stringify(body)});
      if (id===revision.current) setStatus(value);
    } catch(e) { if (id===revision.current) setError(String(e)); }
    finally { if(id===revision.current) setAction(''); }
  }
  const sessionLabel = ({idle:'未运行',checking:'检查中',starting:'准备中',running:'运行中',stopping:'结束中',error:'异常',unknown:'状态未知'} as Record<string,string>)[status.state] || status.state;
  const statusKnown = !['unknown','error'].includes(status.state);
  const checking = action==='check' || status.state==='checking';
  const devices = [
    {name:'乐聚 KUAVO 5-W',ready:statusKnown && !!status.service?.management_ready,detail:'最近一次检查的机器人服务连接与版本核验结果，不代表电机已使能。'},
    {name:'Galaxis VR',ready:active?!!status.pico_live:!!status.vr_ready,detail:active?(status.pico_live?'正在接收实时头显输入':'等待新的头显输入'):(status.vr_detail||'尚未检查头显输入')},
    {name:'Pico 设备',ready:active?!!status.pico_live:!!status.pico_ready,detail:status.pico_detail||'尚未检查 PICO USB 和头显应用'},
  ];
  return <div className="teleop-workspace kuavo-workspace">
    {parametersOpen&&<div className="kuavo-modal-backdrop" onClick={e=>{if(e.target===e.currentTarget)setParametersOpen(false)}} onKeyDown={e=>{if(e.key==='Escape')setParametersOpen(false)}}><section className="surface tianji-parameter-dialog kuavo-auto-parameters" role="dialog" aria-modal="true" aria-labelledby="kuavo-parameters-title">
      <div className="tianji-parameter-header"><h3 id="kuavo-parameters-title">其它参数</h3></div>
      <div className="tianji-extra-parameters kuavo-gripper-fields">
        {(['speed_rad_s','accel_rad_s2','open_rad','closed_rad'] as const).flatMap(key=>(['left','right'] as const).map(side=>{
          const field=gripperFields.find(f=>f.key===key)!;
          const label=(side==='left'?'左':'右')+'夹爪'+({speed_rad_s:'速度',accel_rad_s2:'加速度',open_rad:'打开角度',closed_rad:'关闭角度'}[key]);
          return <label key={side+key}>{label}<input aria-label={label} title={field.unit} type="number" min={field.min} max={field.max} step="any" disabled={locked||gripperSaving} value={Number.isNaN(gripperDraftValues[side][key])?'':gripperDraftValues[side][key]} onChange={event=>{setGripperDraftValues(values=>({...values,[side]:{...values[side],[key]:event.target.valueAsNumber}}))}}/><small>{field.unit}</small></label>
        }))}
      </div>
      {!gripperDraftValid&&<p role="alert">参数超出范围</p>}
      <div className="parameter-dialog-actions">
        <ActionButton disabled={locked||gripperSaving||!gripperDraftValid} onClick={()=>{setGrippers(gripperDraftValues);setGripperEdited(true);setSettingsError('');setParametersOpen(false)}}>确认</ActionButton>
        <ActionButton kind="secondary" onClick={()=>setParametersOpen(false)}>取消</ActionButton>
      </div>
    </section></div>}
    {settingsError&&!parametersOpen&&<p role="alert">夹爪参数自动保存失败，请打开其它参数重试：{settingsError}</p>}
    {gripperDirty&&!gripperValid&&!parametersOpen&&<p role="alert">夹爪参数超出范围，请打开其它参数修改后确认保存。</p>}

    <section className="surface teleop-control-panel">
      <div className="teleop-setup-flow kuavo-setup-flow">
        <section className="teleop-setup-section kuavo-settings">
          <div className="teleop-setup-label"><span>01</span><strong>遥操作设置</strong></div>
          <div className="kuavo-primary-settings">
            <select aria-label="操作手臂" value={armMode} disabled={locked} onChange={e=>setArmMode(e.target.value)}><option value="dual">双臂</option><option value="left">左臂</option><option value="right">右臂</option></select>
            <div className="kuavo-settings-row kuavo-parameter-row">
              {([{key:'position_scale',label:'位移倍率',unit:'倍',min:.1,max:1,step:.1},{key:'max_velocity_rad_s',label:'速度',unit:'rad/s',min:.01,max:.7,step:.05},{key:'planning_acceleration_rad_s2',label:'加速度',unit:'rad/s²',min:.01,max:5,step:.1}] as const).map(field=><label key={field.key}>{field.label}<input aria-label={field.label} title={field.unit} type="number" min={field.min} max={field.max} step={field.step} value={Number.isNaN(motion[field.key])?'':motion[field.key]} disabled={locked} onChange={e=>{motionLoaded.current=true;setMotionFailure('');setMotion({...motion,[field.key]:e.target.valueAsNumber})}}/><small>{field.min}–{field.max} {field.unit}</small></label>)}
            </div>
            <ActionButton kind="secondary" disabled={locked||motionDirty||motionSaving||gripperSaving} onClick={()=>{setGripperDraftValues(gripperDirty?grippers:savedGrippers);setParametersOpen(true)}}>其它参数</ActionButton>
          </div>
          {motionFailure&&<p role="alert">参数自动保存失败：{motionFailure}</p>}
          {!motionValid&&<p role="alert">遥操作参数超出范围</p>}
          {!motionDirty&&status.motion_settings?.supported===false&&<p role="status">速度、加速度尚未应用，当前服务仅支持 0.7 rad/s、5 rad/s²</p>}
        </section>
        <section className="teleop-setup-section teleop-devices-section">
          <div className="teleop-setup-label"><span>02</span><strong>所需设备</strong><small>{devices.filter(device=>device.ready).length}/3</small></div>
          <div className="teleop-required-devices">{devices.map(device=><article key={device.name} title={device.detail} className={'teleop-required-device '+(device.ready?'device-connected':'device-offline')}><span>{device.name}</span><strong>{checking?'检测中':device.ready?'已连接':'未连接'}</strong></article>)}</div>
        </section>
      </div>
      {(error||status.error)&&<div className="inline-alert teleop-panel-alert" role="alert">{error||status.error}</div>}
      <div className="teleop-control-footer">
        <span className="compact-status" role="status">会话：{sessionLabel} · 本地日志自动保存</span>
        <div className="teleop-control-actions">
          <ActionButton kind="secondary" disabled={locked||motionSaving||(motionDirty&&!motionFailure)} busy={action==='check'} onClick={()=>void command('check')}>检查设备</ActionButton>
          {active ? <ActionButton danger disabled={!status.session_id || status.state==='stopping'} onClick={()=>void command('stop')}>{status.state==='stopping'?'正在结束…':'结束遥操作'}</ActionButton> :
            <ActionButton disabled={locked || !!(status.gripper_settings?.saved&&!status.gripper_settings.matches_service) || parametersBusy || status.motion_settings?.supported===false || !status.vr_ready || !status.pico_ready || !status.pico_sdk_available || !status.service?.management_ready || !status.service?.service?.ready_to_start} onClick={()=>void command('start')}>开始遥操作</ActionButton>}
        </div>
      </div>
    </section>
    <div className="teleop-observation-grid kuavo-observation-grid">
      <section className="surface teleop-visualizer">
        <div className="preview-head"><strong>机器人姿态</strong>{(status.pose_live||status.pose_available)&&<span>● {status.pose_live?'双臂遥测在线':'反馈已过期'}</span>}</div>
        <div className="kuavo-model-stage"><KuavoRobotPreview joints={status.telemetry?.joints||{}} darkMode={darkMode}/></div>
      </section>
      <section className="surface teleop-camera">
        <div className="preview-head"><strong>相机预览</strong><span>待接入</span></div>
        <div className="kuavo-empty-cameras" aria-label="相机画面预留"><div/><div/><div/></div>
      </section>
    </div>
    <section className="surface teleop-manual">
      <div className="preview-head"><strong>手动控制</strong></div>
      <div className="teleop-manual-body" style={{minHeight:72}} />
    </section>
  </div>;
}
