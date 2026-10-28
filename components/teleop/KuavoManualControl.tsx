'use client';
import {useCallback, useEffect, useRef, useState} from 'react';
import {ArrowUp, ArrowDown, ArrowLeft, ArrowRight, RotateCcw, RotateCw} from 'lucide-react';
import {ActionButton} from '../CollectionUi';
import {requestJson} from '../../lib/http';
import jointRows from '../../teleop_control/assets/kuavo/manual-joints.json';

type Joint = {name:string;label:string;group:string;min_deg:number;max_deg:number};
export type KuavoManualStatus = {
  available:boolean;active:boolean;state:string;session_id?:string;reason?:string;error?:string;
  fresh:boolean;busy:boolean;joints:Joint[];
  poses?:{id:string;name:string}[];poses_available?:boolean;initial_pose_available?:boolean;pose_supported?:boolean;restoring?:boolean;
  telemetry?:{joints?:Record<string,number>;grippers?:Record<string,{position_rad:number;closure_fraction:number}>};
};
type Command = {action:string;[key:string]:unknown};
const directions = [
  {id:'forward',label:'前进',icon:ArrowUp,angle:-90,x:110,y:28},
  {id:'right',label:'右移',icon:ArrowRight,angle:0,x:192,y:110},
  {id:'backward',label:'后退',icon:ArrowDown,angle:90,x:110,y:192},
  {id:'left',label:'左移',icon:ArrowLeft,angle:180,x:28,y:110},
  {id:'turn_left',label:'左转',icon:RotateCcw,angle:0,x:84,y:110},
  {id:'turn_right',label:'右转',icon:RotateCw,angle:0,x:136,y:110},
];
function directionPath(id:string,angle:number) {
  if(id==='turn_left')return 'M108 61.04 A49 49 0 0 0 108 158.96 Z';
  if(id==='turn_right')return 'M112 61.04 A49 49 0 0 1 112 158.96 Z';
  const point=(radius:number,degrees:number)=>[110+radius*Math.cos(degrees*Math.PI/180),110+radius*Math.sin(degrees*Math.PI/180)].join(' ');
  return `M${point(105,angle-42)} A105 105 0 0 1 ${point(105,angle+42)} L${point(57,angle+42)} A57 57 0 0 0 ${point(57,angle-42)} Z`;
}

export function KuavoManualControl({baseUrl,status,teleopBusy,onStatus}:{
  baseUrl:string;status?:KuavoManualStatus;teleopBusy:boolean;onStatus:(value:unknown)=>void;
}) {
  const [step,setStep] = useState(1);
  const [pending,setPending] = useState(false);
  const [error,setError] = useState('');
  const [held,setHeld] = useState('');
  const [selectedPose,setSelectedPose] = useState('initial');
  const sequence = useRef(0);
  const latest = useRef(status); latest.current = status;
  const heldRef = useRef('');
  useEffect(()=>setSelectedPose('initial'),[status?.session_id]);
  const ready = status?.state==='ready' && status.fresh;
  const enabled = ready && !status?.busy && !pending && !teleopBusy;
  const send = useCallback(async (command:Command) => {
    const current = latest.current;
    if (!current?.active || !current.session_id) return;
    const id = current.session_id;
    try {
      await requestJson(baseUrl,'/kuavo/manual/input',{method:'POST',body:JSON.stringify({
        session_id:id,sequence:++sequence.current,command,
      })});
    } catch(e) {
      if(latest.current?.session_id===id && command.action!=='heartbeat') setError(String(e));
      // Never renew a pressed command after any send failure.
      heldRef.current='';setHeld('');
    }
  },[baseUrl]);
  const release = useCallback(()=>{
    const wasHeld=heldRef.current;heldRef.current='';setHeld('');
    if(wasHeld)void send({action:'base',direction:'stop'});
  },[send]);
  useEffect(()=>{
    if(!status?.active)return;
    const timer=setInterval(()=>void send({action:'heartbeat'}),200);
    return ()=>clearInterval(timer);
  },[status?.active,status?.session_id,send]);
  useEffect(()=>{
    const timer=setInterval(()=>{if(heldRef.current)void send({action:'base',direction:heldRef.current})},80);
    const hide=()=>{if(document.hidden)release()};
    window.addEventListener('blur',release);document.addEventListener('visibilitychange',hide);
    return ()=>{clearInterval(timer);release();window.removeEventListener('blur',release);document.removeEventListener('visibilitychange',hide)};
  },[send,release]);
  useEffect(()=>{if(!enabled)release()},[enabled,release]);
  async function lifecycle(action:'start'|'stop') {
    release();setPending(true);setError('');
    if(action==='start')setSelectedPose('initial');
    try {
      const result=await requestJson(baseUrl,'/kuavo/manual/'+action,{method:'POST',
        body:JSON.stringify(action==='stop'?{session_id:status?.session_id}:{})});
      onStatus(result);
    }catch(e){setError(String(e))}finally{setPending(false)}
  }
  function press(direction:string) {
    if(!enabled||heldRef.current)return;
    heldRef.current=direction;setHeld(direction);setError('');
    void send({action:'base',direction});
  }
  function act(command:Command){if(!enabled||heldRef.current)return;setError('');void send(command)}
  const poses=[{id:'initial',name:'初始位置（默认）'},...(status?.poses||[])];
  const poseAvailable=selectedPose==='initial'?status?.initial_pose_available:poses.some(p=>p.id===selectedPose);
  async function poseOperation(action:'save'|'delete') {
    let body:Record<string,unknown>;
    if(action==='save'){
      const name=window.prompt('请输入姿态名称')?.trim();if(!name)return;
      if(name.length>50){setError('姿态名称为 1～50 个字符');return}
      body={action,name,session_id:status?.session_id};
    }else{
      if(!window.confirm('删除当前姿态配置？'))return;
      body={action,id:selectedPose};
    }
    setPending(true);setError('');
    try{
      const result=await requestJson<{manual:{selected_pose?:string}}>(baseUrl,'/kuavo/manual/poses',{method:'POST',body:JSON.stringify(body)});
      setSelectedPose(result.manual.selected_pose||'initial');onStatus(result);
    }catch(e){setError(String(e))}finally{setPending(false)}
  }
  function restorePose(){
    if(status?.restoring){void send({action:'stop'});return}
    const name=poses.find(p=>p.id===selectedPose)?.name;
    if(enabled&&poseAvailable&&window.confirm(`将双臂、夹爪、躯干和头部复位到「${name}」，请确认运动路径无人和障碍物。`))act({action:'restore',pose_id:selectedPose});
  }
  const rows=status?.joints?.length?status.joints:jointRows;
  function joints(group:string) {
    return rows.filter(j=>j.group===group).map(j=>{
      const radians=status?.telemetry?.joints?.[j.name];
      const angle=typeof radians==='number'&&Number.isFinite(radians)?radians*180/Math.PI:undefined;
      return <div className="teleop-joint" key={j.name}>
        <span><strong>{j.label}</strong><small>{j.min_deg.toFixed(0)}° ～ {j.max_deg.toFixed(0)}°</small></span>
        <div className="teleop-joint-stepper">
          <output aria-label={j.label+'实测角度'} className={!ready?'is-stale':''}>{angle===undefined?'—':angle.toFixed(2)}<small> °</small></output>
          <div>
            <button type="button" aria-label={j.label+'增加'} disabled={!enabled||!!held||angle===undefined||angle+step>j.max_deg} onClick={()=>act({action:'joint',joint:j.name,delta_deg:step})}>▲</button>
            <button type="button" aria-label={j.label+'减少'} disabled={!enabled||!!held||angle===undefined||angle-step<j.min_deg} onClick={()=>act({action:'joint',joint:j.name,delta_deg:-step})}>▼</button>
          </div>
        </div>
      </div>
    });
  }
  function gripper(side:'left'|'right') {
    const value=status?.telemetry?.grippers?.[side]?.closure_fraction;
    return <div className="teleop-joint">
      <span><strong>{side==='left'?'左夹爪':'右夹爪'}</strong><small>{typeof value==='number'?'闭合 '+(value*100).toFixed(0)+'%':'暂无夹爪反馈'}</small></span>
      <div className="kuavo-manual-gripper-actions">
        <ActionButton kind="secondary" disabled={!enabled||!!held} onClick={()=>act({action:'gripper',side,closed:false})}>打开</ActionButton>
        <ActionButton kind="secondary" disabled={!enabled||!!held} onClick={()=>act({action:'gripper',side,closed:true})}>闭合</ActionButton>
      </div>
    </div>;
  }

  return <section className="surface kuavo-manual-panel">
    <div className="preview-head kuavo-manual-heading"><h3>手动控制</h3><span className="badge badge-amber">真实运动</span>
      <div className="kuavo-manual-toolbar"><label>步长 <select aria-label="手动关节步长" value={step} onChange={e=>setStep(Number(e.target.value))}>{[1,5,10].map(n=><option key={n} value={n}>{n}°</option>)}</select></label>
      {status?.active&&<ActionButton kind="secondary" disabled={pending||status.state==='stopping'} onClick={()=>void lifecycle('stop')}>{status.state==='stopping'?'正在退出…':'结束手动控制'}</ActionButton>}</div>
    </div>
    <div className="teleop-preset-toolbar kuavo-preset-toolbar">
      <div><strong>姿态配置</strong></div>
      <div className="teleop-preset-controls">
        <select aria-label="乐聚姿态配置" value={selectedPose} disabled={pending||status?.busy} onChange={e=>setSelectedPose(e.target.value)}>
          {poses.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <ActionButton danger={status?.restoring} disabled={status?.restoring?!status.active:!enabled||!!held||!poseAvailable||!status?.pose_supported} onClick={restorePose}>{status?.restoring?'停止':'复位'}</ActionButton>
        <ActionButton kind="secondary" disabled={!enabled||!!held||status?.poses_available===false} onClick={()=>void poseOperation('save')}>保存当前姿态</ActionButton>
        <ActionButton kind="ghost" danger disabled={pending||status?.busy||selectedPose==='initial'||!poseAvailable} onClick={()=>void poseOperation('delete')}>删除</ActionButton>
      </div>
    </div>
    <div className="teleop-joint-columns kuavo-manual-columns">
      <div className="teleop-joint-column"><header><h4>左臂</h4></header><div>{joints('left')}{gripper('left')}</div></div>
      <div className="teleop-joint-column">
        <header><h4>躯干</h4></header><div>{joints('body')}</div>
        <header><h4>头部</h4></header><div>{joints('head')}</div>
        <header><h4>底盘</h4></header>
        <div className="kuavo-base-stage">
          <svg className="kuavo-base-disc" viewBox="0 0 220 220" role="group" aria-label="底盘圆形控制面板">
            <circle className="kuavo-base-rim" cx="110" cy="110" r="109.5"/>
            {directions.map(d=><g key={d.id} role="button" tabIndex={enabled?0:-1} aria-disabled={!enabled} aria-label={'底盘'+d.label}
              className={'kuavo-base-direction'+(held===d.id?' is-held':'')}
              onPointerDown={e=>{if(e.button!==0||!enabled)return;e.preventDefault();e.currentTarget.setPointerCapture(e.pointerId);press(d.id)}}
              onPointerUp={release} onPointerCancel={release} onLostPointerCapture={release} onBlur={release}
              onKeyDown={e=>{if((e.key===' '||e.key==='Enter')&&!e.repeat){e.preventDefault();press(d.id)}}}
              onKeyUp={e=>{if(e.key===' '||e.key==='Enter'){e.preventDefault();release()}}}>
              <path className="kuavo-base-sector" d={directionPath(d.id,d.angle)}/>
              <d.icon className="kuavo-base-symbol" x={d.x-12} y={d.y-12} width={24} height={24} strokeWidth={1.7} aria-hidden="true"/>
            </g>)}
          </svg>
        </div>
      </div>
      <div className="teleop-joint-column"><header><h4>右臂</h4></header><div>{joints('right')}{gripper('right')}</div></div>
    </div>
    {(error||status?.error||!status?.available||status?.state==='starting')&&<p className="kuavo-manual-message" role={error||status?.error?'alert':undefined}>{error||status?.error||(status?.state==='starting'?'正在核验并连接手动控制…':status?.reason||'检查设备后连接手动控制')}</p>}
  </section>;
}
