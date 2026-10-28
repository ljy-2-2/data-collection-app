'use client';
import { useEffect, useRef, useState } from 'react';
import { ActionButton, StatusDot } from '../CollectionUi';
import { KuavoRobotPreview } from '../KuavoRobotPreview';
import { KuavoCameras, type KuavoCameraStatus } from './KuavoCameras';
import {KuavoSessionSettingsDialog, type SessionSettings} from './KuavoSessionSettingsDialog';
import {KuavoManualControl, type KuavoManualStatus} from './KuavoManualControl';
import { requestJson } from '../../lib/http';

type GripperValues = {open_rad:number;closed_rad:number;speed_rad_s:number;accel_rad_s2:number};
type CollectionStatus={state:string;armed:boolean;busy:boolean;recording:boolean;completed:number;elapsed_s:number;error:string;upload?:{pending?:number;uploaded?:number;error?:string};settings:{enabled:boolean;start_button:'A'|'X';save_button:'B'}};
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
  manual?:KuavoManualStatus;
  collection?:CollectionStatus;
  camera_preview?:KuavoCameraStatus;
  service_block_detail?:string;prestart_editable?:boolean;control_ready?:boolean;can_restart?:boolean;base_supported?:boolean;pose_detail?:string;hardware_detail?:string;preparation_progress?:string;execution_notice?:string;
  motion_controls?:Record<string,{values:Motion;schema:Record<keyof Motion,{min:number;max:number;min_exclusive?:boolean}>;mixed:string[];targets:Record<keyof Motion,string[]>}>;
  parameter_mode?:string;session_settings?:SessionSettings;settings_preview?:unknown;start_blocked_reason?:string;
  motion_settings?:{values:Motion;saved:boolean;supported:boolean;message:string;file:string;error?:string};
  gripper_settings?:{values:Grippers;saved:boolean;matches_service:boolean;message:string;file:string};
  state: string; active?: boolean; session_id?: string; error?: string;
  service?: { management_ready?: boolean; service?: { ready_to_start?: boolean } };
  reference?: {max_velocity_rad_s:number;planning_acceleration_rad_s2:number;grippers:Record<string,Record<string,number>>};
  telemetry?: {joints?:Record<string,number>;grippers?:import("../../lib/kuavoPreview").KuavoGrippers}; pose_live?: boolean; pose_available?: boolean; pico_live?: boolean;
  vr_checked?:boolean;vr_ready?:boolean;pico_ready?:boolean;vr_detail?:string;pico_detail?:string;
  pico_sdk_available?: boolean; preparation?: Record<string, unknown>;
  log_directory?: string; limitations?: string[]; logs?: string[];
};
export function KuavoTeleopWorkspace({baseUrl, task, onBusyChange, darkMode=false}: {
  darkMode?:boolean; baseUrl: string; task: string; onBusyChange?: (busy: boolean) => void;
}) {
  const [status, setStatus] = useState<Status>({state:'idle'});
  const [action, setAction] = useState('');
  const serviceParameters=status.parameter_mode==='service';
  const [serviceParametersOpen,setServiceParametersOpen]=useState(false);
  const settingsInFlight=useRef(false);
  const [error, setError] = useState('');
  const [armMode,setArmMode] = useState('dual');
  const [motion,setMotion] = useState<Motion>(defaultMotion);
  const motionLoaded=useRef(false);
  const latestMotion=useRef(motion);latestMotion.current=motion;
  const controls=status.motion_controls?.[armMode];
  const [motionEdits,setMotionEdits]=useState<(keyof Motion)[]>([]);
  const motionBase=useRef<SessionSettings|undefined>(undefined);
  const savedMotion=(serviceParameters?controls?.values:status.motion_settings?.values)||defaultMotion;
  const motionDirty=serviceParameters?motionEdits.length>0:(Object.keys(defaultMotion) as (keyof Motion)[]).some(key=>motion[key]!==savedMotion[key]);
  useEffect(()=>{motionLoaded.current=false;setMotion(defaultMotion)},[baseUrl]);
  useEffect(()=>{if(!serviceParameters&&status.motion_settings&&!motionLoaded.current){setMotion(status.motion_settings.values);motionLoaded.current=true}},[status.motion_settings,serviceParameters]);
  useEffect(()=>{if(serviceParameters&&controls&&!motionEdits.length){setMotion(controls.values);motionLoaded.current=true}},[serviceParameters,armMode,controls?.values.position_scale,controls?.values.max_velocity_rad_s,controls?.values.planning_acceleration_rad_s2,motionEdits.length]);
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
  const active = (!!status.active && !['checking','preparing','ready'].includes(status.state)) || ['starting','running','stopping','unknown'].includes(status.state);
  const prestartEditable=status.prestart_editable??!['starting','running','stopping','unknown'].includes(status.state);
  const editLocked=!prestartEditable||!['','check'].includes(action);
  const locked = !!action || active || ['checking','preparing','ready'].includes(status.state);
  function bounds(key:keyof Motion) {
    return serviceParameters?controls?.schema[key]:{min:key==='position_scale'?.1:.01,max:key==='position_scale'?1:key==='max_velocity_rad_s'?.7:5,min_exclusive:false};
  }
  const motionValid=(Object.keys(defaultMotion) as (keyof Motion)[]).every(key=>{const spec=bounds(key),v=motion[key];return spec&&Number.isFinite(v)&&v>=spec.min&&v<=spec.max&&!(spec.min_exclusive&&v===spec.min)});
  function editMotion(key:keyof Motion,value:number) {
    if(serviceParameters){
      if(!status.session_settings?.editable||motionSaving)return;
      if(!motionBase.current)motionBase.current=structuredClone(status.session_settings);
      setMotionEdits(current=>current.includes(key)?current:[...current,key]);
    }
    motionLoaded.current=true;setMotionFailure('');setMotion({...motion,[key]:value});
  }
  const savedGrippers=gripperDraft(status.gripper_settings?.values||status.reference?.grippers);
  const gripperDirty=!serviceParameters&&gripperEdited&&JSON.stringify(grippers)!==JSON.stringify(savedGrippers);
  const gripperValid=(['left','right'] as const).every(side=>gripperFields.every(field=>Number.isFinite(grippers[side][field.key])&&grippers[side][field.key]>=field.min&&grippers[side][field.key]<=field.max));
  const gripperDraftValid=(['left','right'] as const).every(side=>gripperFields.every(field=>Number.isFinite(gripperDraftValues[side][field.key])&&gripperDraftValues[side][field.key]>=field.min&&gripperDraftValues[side][field.key]<=field.max));
  const parametersBusy=motionDirty||motionSaving||gripperDirty||gripperSaving;
  useEffect(()=>{
    if(parametersOpen||!gripperDirty||!gripperValid||editLocked||motionSaving||gripperSaving||settingsError)return;
    const timer=setTimeout(()=>{
      if(motionInFlight.current||gripperInFlight.current)return;
      gripperInFlight.current=true;setGripperSaving(true);++revision.current;
      void requestJson<Status>(baseUrl,'/kuavo/grippers',{method:'POST',body:JSON.stringify(grippers)})
        .then(value=>setStatus(current=>({...current,gripper_settings:value.gripper_settings})))
        .catch(error=>{if(JSON.stringify(latestGrippers.current)===JSON.stringify(grippers))setSettingsError(String(error))})
        .finally(()=>{++revision.current;gripperInFlight.current=false;setGripperSaving(false)});
    },350);
    return ()=>clearTimeout(timer);
  },[baseUrl,grippers,gripperDirty,gripperValid,editLocked,motionSaving,gripperSaving,settingsError,parametersOpen]);
  useEffect(() => { onBusyChange?.(!prestartEditable||action==='cancel'); }, [prestartEditable,action,onBusyChange]);
  useEffect(()=>{
    if(!motionLoaded.current||!motionDirty||!motionValid||editLocked||motionSaving||gripperSaving||motionFailure)return;
    const timer=setTimeout(()=>{
      if(motionInFlight.current||gripperInFlight.current)return;
      motionInFlight.current=true;setMotionSaving(true);++revision.current;
      const form=motionBase.current;
      if(serviceParameters&&(!form||!controls)){motionInFlight.current=false;setMotionSaving(false);setMotionFailure('请重新检查设备后修改参数');return}
      const payload=serviceParameters?{action:'apply',confirm:true,changes:Object.fromEntries(motionEdits.flatMap(key=>controls!.targets[key].map(target=>[target,motion[key]]))),expected_revision:form!.revision,expected_service_instance_id:form!.service_instance_id,expected_installation_sha256:form!.installation_sha256}:motion;
      void requestJson<Status>(baseUrl,serviceParameters?'/kuavo/session-settings':'/kuavo/motion',{method:'POST',body:JSON.stringify(payload)})
        .then(value=>{if(serviceParameters){motionBase.current=undefined;setMotionEdits([]);setStatus(value)}else setStatus(current=>({...current,motion_settings:value.motion_settings}))})
        .catch(error=>{if(serviceParameters)setStatus(current=>({...current,session_settings:undefined}));if((Object.keys(defaultMotion) as (keyof Motion)[]).every(key=>latestMotion.current[key]===motion[key]))setMotionFailure(String(error)+(serviceParameters?"；请重新检查设备并修改":""))})
        .finally(()=>{++revision.current;motionInFlight.current=false;setMotionSaving(false)});
    },350);
    return ()=>clearTimeout(timer);
  },[baseUrl,motion,motionDirty,motionValid,editLocked,motionSaving,gripperSaving,motionFailure,serviceParameters,motionEdits,controls]);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const current = revision.current;
      try {
        const value = await requestJson<Status>(baseUrl, '/kuavo/status');
        if (!cancelled && current === revision.current && !motionInFlight.current && !gripperInFlight.current && !settingsInFlight.current) setStatus(value);
      } catch (e) {
        if (!cancelled && current === revision.current) setStatus(s => ({...s,state:'unknown',error:String(e),pose_live:false,pico_live:false,vr_ready:false,pico_ready:false}));
      } finally { if (!cancelled) timer = setTimeout(poll, 500); }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl]);
  async function serviceSettings(body:Record<string,unknown>) {
    ++revision.current;settingsInFlight.current=true;setAction('settings');
    try {
      const value=await requestJson<Status>(baseUrl,'/kuavo/session-settings',{method:'POST',body:JSON.stringify(body)});
      setStatus(value);return value;
    } catch(e){setStatus(current=>({...current,session_settings:undefined,settings_preview:undefined}));throw e}
    finally{++revision.current;settingsInFlight.current=false;setAction('')}
  }
  async function command(name: string, preparation?: string) {
    if (name==='start' && (active||!!action||!(status.control_ready||status.can_restart)))return;
    if (name==='check' && locked)return;
    if(name!=='stop'&&parametersBusy&&!(name==='check'&&motionFailure&&!motionSaving)){if(name==='check'){setMotionFailure('');setSettingsError('')}return}
    if(name==='check'&&motionFailure){motionBase.current=undefined;setMotionEdits([]);setMotionFailure('')}
    const id = ++revision.current;
    let motorIds = false;
    if (name === 'prepare') {
      if (!window.confirm(preparation === 'grippers_inspect' ? '读取会发送夹爪查询帧，不改写参数。继续？' : '此操作可能启动控制或产生动作。确认现场已按厂家流程准备，并执行本次操作？')) return;
      if (preparation === 'start') {
        motorIds = window.confirm('已确认左电机 8、右电机 1、反馈编号 0？');
        if (!motorIds) return;
      }
    }
    setAction(name); setError('');
    const restarting=name==='start'&&!status.control_ready&&!!status.can_restart;
    const preparing=name==='check'||restarting;
    const body = name === 'stop' ? {session_id:status.session_id} : {
      task, network_namespace:'current', arm_mode:armMode, position_scale:serviceParameters?1:motion.position_scale, confirm_motion:name==='check'||name==='start'||(name==='prepare'&&preparation!=='grippers_inspect'),
      prepare_controller:preparing,prepare_grippers:preparing,prepare_session:preparing,require_prepared:name==='start',restart_after_stop:restarting,
      accept_unverified_gripper_bus:true, motor_ids_confirmed:preparing||motorIds,
      ...(preparation ? {preparation_action:preparation} : {}),
    };
    try {
      const cancel=name==='stop'&&prestartEditable;
      const value = await requestJson<Status>(baseUrl, cancel?'/kuavo/cancel-preparation':'/kuavo/'+name, {method:'POST',body:cancel?undefined:JSON.stringify(body)});
      if (id===revision.current) setStatus(value);
    } catch(e) { if (id===revision.current) setError(String(e)); }
    finally { if(id===revision.current) setAction(''); }
  }
  const collection=status.collection;
  const collectionLabel=collection?.recording?'采集中':collection?.busy?'正在保存':collection?.state==='incomplete'?'本轮不完整':collection?.state==='disabled'?'采集已关闭':collection?.armed?`等待手柄 ${collection.settings.start_button}`:'未开始遥操作';
  const statusKnown = !['unknown','error'].includes(status.state);
  const checking = action==='check' || status.state==='checking';
  const devices = [
    {name:'乐聚 KUAVO 5-W',ready:!!status.service?.management_ready,detail:status.service_block_detail||status.hardware_detail||'最近一次检查的机器人服务连接与版本核验结果，不代表电机已使能。'},
    {name:'Galaxis VR',ready:active?!!status.pico_live:!!status.vr_ready,detail:active?(status.pico_live?'正在接收实时头显输入':'等待新的头显输入'):(status.vr_detail||'尚未检查头显输入')},
    {name:'Pico 设备',ready:active?!!status.pico_live:!!status.pico_ready,detail:status.pico_detail||'尚未检查 PICO USB 和头显应用'},
  ];
  return <div className="teleop-workspace kuavo-workspace">
    {serviceParametersOpen&&<KuavoSessionSettingsDialog request={serviceSettings} onClose={()=>setServiceParametersOpen(false)}/>}
    {parametersOpen&&<div className="kuavo-modal-backdrop" onClick={e=>{if(e.target===e.currentTarget)setParametersOpen(false)}} onKeyDown={e=>{if(e.key==='Escape')setParametersOpen(false)}}><section className="surface tianji-parameter-dialog kuavo-auto-parameters" role="dialog" aria-modal="true" aria-labelledby="kuavo-parameters-title">
      <div className="tianji-parameter-header"><h3 id="kuavo-parameters-title">其它参数</h3></div>
      <div className="tianji-extra-parameters kuavo-gripper-fields">
        {(['speed_rad_s','accel_rad_s2','open_rad','closed_rad'] as const).flatMap(key=>(['left','right'] as const).map(side=>{
          const field=gripperFields.find(f=>f.key===key)!;
          const label=(side==='left'?'左':'右')+'夹爪'+({speed_rad_s:'速度',accel_rad_s2:'加速度',open_rad:'打开角度',closed_rad:'关闭角度'}[key]);
          return <label key={side+key}>{label}<input aria-label={label} title={field.unit} type="number" min={field.min} max={field.max} step="any" disabled={editLocked||gripperSaving} value={Number.isNaN(gripperDraftValues[side][key])?'':gripperDraftValues[side][key]} onChange={event=>{setGripperDraftValues(values=>({...values,[side]:{...values[side],[key]:event.target.valueAsNumber}}))}}/><small>{field.unit}</small></label>
        }))}
      </div>
      {!gripperDraftValid&&<p role="alert">参数超出范围</p>}
      <div className="parameter-dialog-actions">
        <ActionButton disabled={editLocked||gripperSaving||!gripperDraftValid} onClick={()=>{setGrippers(gripperDraftValues);setGripperEdited(true);setSettingsError('');setParametersOpen(false)}}>确认</ActionButton>
        <ActionButton kind="secondary" onClick={()=>setParametersOpen(false)}>取消</ActionButton>
      </div>
    </section></div>}

    <section className="surface teleop-control-panel">
      <div className="teleop-setup-flow kuavo-setup-flow">
        <section className="teleop-setup-section kuavo-settings">
          <div className="teleop-setup-label"><span>01</span><strong>遥操作设置</strong></div>
          <div className="kuavo-primary-settings">
            <select aria-label="遥操作范围" value={armMode} disabled={editLocked||motionDirty||motionSaving} onChange={e=>setArmMode(e.target.value)}><option value="dual">双臂</option><option value="dual_base">双臂＋底盘</option><option value="left">左臂</option><option value="right">右臂</option></select>
            <div className="kuavo-settings-row kuavo-parameter-row">
              {([{key:'position_scale',label:'位移倍率',unit:'倍',min:.1,max:1,step:.1},{key:'max_velocity_rad_s',label:'速度',unit:'rad/s',min:.01,max:.7,step:.05},{key:'planning_acceleration_rad_s2',label:'加速度',unit:'rad/s²',min:.01,max:5,step:.1}] as const).map(field=>{const spec=bounds(field.key)||field;return <label key={field.key}>{field.label}<input aria-label={field.label} title={field.unit} type="number" min={spec.min} max={spec.max} step={serviceParameters?'any':field.step} value={Number.isNaN(motion[field.key])?'':motion[field.key]} disabled={editLocked||motionSaving||(serviceParameters&&!status.session_settings?.editable)} onChange={e=>editMotion(field.key,e.target.valueAsNumber)}/><small style={{visibility:serviceParameters&&!controls?'hidden':undefined}}>{('min_exclusive' in spec&&spec.min_exclusive)?'> ':''}{spec.min}–{spec.max} {field.unit}</small></label>})}
            </div>
            <ActionButton kind="secondary" disabled={editLocked||motionDirty||motionSaving||gripperSaving} onClick={()=>{if(serviceParameters){setServiceParametersOpen(true);return}setGripperDraftValues(gripperDirty?grippers:savedGrippers);setParametersOpen(true)}}>其它参数</ActionButton>
          </div>
        </section>
        <section className="teleop-setup-section teleop-devices-section">
          <div className="teleop-setup-label"><span>02</span><strong>所需设备</strong><small>{devices.filter(device=>device.ready).length}/3</small></div>
          <div className="teleop-required-devices">{devices.map(device=><article key={device.name} title={device.detail} className={'teleop-required-device '+(device.ready?'device-connected':'device-offline')}><span>{device.name}</span><strong>{checking?'检测中':device.ready?'已连接':'未连接'}</strong></article>)}</div>
        </section>
      </div>
      <div className="teleop-control-footer">
        <div className="tianji-collection-summary" role="status" aria-live="polite" aria-atomic="true">
          <span>本次已完成 <strong>{collection?.completed??0}</strong> Episode</span>
          <span className="compact-status"><StatusDot online={!!collection?.armed} pulse={!!collection?.recording}/>{collectionLabel}</span>
        </div>
        <div className="teleop-control-actions">
          <ActionButton kind="secondary" disabled={locked||motionSaving||(motionDirty&&!motionFailure)} busy={action==='check'} onClick={()=>void command('check')}>检查设备</ActionButton>
          {(active&&!prestartEditable&&!status.manual?.active)&&<ActionButton danger disabled={(!prestartEditable&&!status.session_id)||status.state==='stopping'||action==='stop'} onClick={()=>void command('stop')}>{status.state==='stopping'?'正在结束…':'结束遥操作'}</ActionButton>}
          {!active&&<ActionButton disabled={!(status.control_ready||status.can_restart)||!!action||parametersBusy} onClick={()=>void command('start')}>开始遥操作</ActionButton>}
        </div>
      </div>
    </section>
    <div className="teleop-observation-grid kuavo-observation-grid">
      <section className="surface teleop-visualizer">
        <div className="preview-head"><strong>机器人姿态</strong></div>
        <div className="kuavo-model-stage"><KuavoRobotPreview joints={status.telemetry?.joints||{}} grippers={status.telemetry?.grippers} darkMode={darkMode}/></div>
      </section>
      <KuavoCameras baseUrl={baseUrl} editable={!status.collection?.busy} status={status.camera_preview} onChange={camera_preview=>setStatus(s=>({...s,camera_preview}))}/>
    </div>
    <KuavoManualControl baseUrl={baseUrl} status={status.manual} teleopBusy={!!action||(active&&!status.manual?.active)} onStatus={value=>setStatus(value as Status)}/>
  </div>;
}
