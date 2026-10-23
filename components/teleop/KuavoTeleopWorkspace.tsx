'use client';
import { useEffect, useRef, useState } from 'react';
import { ActionButton } from '../CollectionUi';
import { KuavoRobotPreview } from '../KuavoRobotPreview';
import { requestJson } from '../../lib/http';

type GripperValues = {open_rad:number;closed_rad:number;speed_rad_s:number;accel_rad_s2:number};
type Grippers = Record<'left'|'right',GripperValues>;
type Connection = {configured?:boolean;host:string;port:number;certificate_sha256:string};
const gripperFields = [
  {key:'open_rad',label:'张开位置',unit:'rad',min:-3.12,max:3.12,step:.01},
  {key:'closed_rad',label:'闭合位置',unit:'rad',min:-3.12,max:3.12,step:.01},
  {key:'speed_rad_s',label:'速度',unit:'rad/s',min:.01,max:15,step:.1},
  {key:'accel_rad_s2',label:'加速度',unit:'rad/s²',min:.01,max:200,step:1},
] as const;
function gripperDraft(source?:Record<string,Record<string,number>>): Grippers {
  return Object.fromEntries(['left','right'].map(side=>[side,Object.fromEntries(gripperFields.map(f=>[f.key,source?.[side]?.[f.key]??0]))])) as Grippers;
}
type Status = {
  connection?:Connection;
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
  const [namespace, setNamespace] = useState('current');
  const [armMode,setArmMode] = useState('dual');
  const [positionScale,setPositionScale] = useState(1);
  const [parametersOpen,setParametersOpen] = useState(false);
  const [grippers,setGrippers] = useState<Grippers>(()=>gripperDraft());
  const [connectionOpen,setConnectionOpen] = useState(false);
  const [connection,setConnection] = useState<Connection>({host:'192.168.26.1',port:7443,certificate_sha256:''});
  const [token,setToken] = useState('');
  const [settingsError,setSettingsError] = useState('');
  const [accepted, setAccepted] = useState(false);
  const revision = useRef(0);
  const active = !!status.active || ['starting','running','stopping','unknown'].includes(status.state);
  const locked = !!action || active || status.state === 'checking';
  useEffect(() => { onBusyChange?.(locked); }, [locked, onBusyChange]);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const current = revision.current;
      try {
        const value = await requestJson<Status>(baseUrl, '/kuavo/status');
        if (!cancelled && current === revision.current) setStatus(value);
      } catch (e) {
        if (!cancelled && current === revision.current) setStatus(s => ({...s,state:'unknown',error:String(e),pose_live:false,pico_live:false,vr_ready:false,pico_ready:false}));
      } finally { if (!cancelled) timer = setTimeout(poll, 500); }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl]);
  async function command(name: string, preparation?: string) {
    if (name !== 'stop' && locked) return;
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
      task, network_namespace:namespace, arm_mode:armMode, position_scale:positionScale, confirm_motion:name==='start'||(name==='prepare'&&preparation!=='grippers_inspect'),
      accept_unverified_gripper_bus:accepted, motor_ids_confirmed:motorIds,
      ...(preparation ? {preparation_action:preparation} : {}),
    };
    try {
      const value = await requestJson<Status>(baseUrl, '/kuavo/'+name, {method:'POST',body:JSON.stringify(body)});
      if (id===revision.current) setStatus(value);
    } catch(e) { if (id===revision.current) setError(String(e)); }
    finally { if(id===revision.current) setAction(''); }
  }
  async function saveSettings(kind:'connection'|'grippers',body:unknown) {
    if(locked)return;
    const id=++revision.current;setAction(kind);setSettingsError('');
    try {
      const value=await requestJson<Status>(baseUrl,'/kuavo/'+kind,{method:'POST',body:JSON.stringify(body)});
      if(id!==revision.current)return;
      setStatus(value);
      if(kind==='grippers')setParametersOpen(false);
      else {setToken('');setConnectionOpen(false)}
    } catch(e) {if(id===revision.current)setSettingsError(String(e))}
    finally {if(id===revision.current)setAction('')}
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
    {parametersOpen&&<div className="kuavo-modal-backdrop"><form className="surface kuavo-parameter-dialog" role="dialog" aria-modal="true" aria-label="其他参数 · 乐聚达妙夹爪" onSubmit={event=>{event.preventDefault();void saveSettings('grippers',grippers)}}>
      <h3>其他参数 · 乐聚达妙夹爪</h3>
      <p>修改保存到本机。机器人服务更新配置后才生效，当前不支持在线应用。</p>
      {(['left','right'] as const).map(side=><section key={side}><strong>{side==='left'?'左夹爪':'右夹爪'}</strong><div className="kuavo-gripper-fields">{gripperFields.map(field=><label key={field.key}>{field.label}（{field.unit}）<input aria-label={(side==='left'?'左':'右')+field.label} type="number" required min={field.min} max={field.max} step="any" disabled={!!action} value={Number.isNaN(grippers[side][field.key])?'':grippers[side][field.key]} onChange={event=>setGrippers(values=>({...values,[side]:{...values[side],[field.key]:event.target.valueAsNumber}}))}/></label>)}</div></section>)}
      <p role="status">{status.gripper_settings?.message}</p>
      {status.gripper_settings?.file&&<p style={{overflowWrap:'anywhere'}}>{status.gripper_settings.file}</p>}
      {settingsError&&<p role="alert">{settingsError}</p>}
      <div className="kuavo-actions"><ActionButton kind="secondary" disabled={!!action} onClick={()=>setGrippers(gripperDraft(status.reference?.grippers))}>恢复原项目值</ActionButton><ActionButton kind="secondary" disabled={!!action} onClick={()=>setParametersOpen(false)}>取消</ActionButton><button className="action-button" type="submit" disabled={locked}>{action==='grippers'?'保存中…':'确认'}</button></div>
    </form></div>}
    {connectionOpen&&<div className="kuavo-modal-backdrop"><form className="surface kuavo-parameter-dialog" role="dialog" aria-modal="true" aria-label="机器人连接" onSubmit={event=>{event.preventDefault();void saveSettings('connection',{host:connection.host,port:connection.port,certificate_sha256:connection.certificate_sha256,token})}}>
      <h3>机器人连接</h3><p>本电脑通过自身网络连接机器人。新电脑首次填写配置，并准备 PICO 接收服务和 SDK；保存后点击检查设备。</p>
      <div className="kuavo-connection-fields">
        <label>机器人服务 IP<input required value={connection.host} disabled={!!action} onChange={e=>setConnection({...connection,host:e.target.value})}/></label>
        <label>端口<input required type="number" min="1" max="65535" value={Number.isNaN(connection.port)?'':connection.port} disabled={!!action} onChange={e=>setConnection({...connection,port:e.target.valueAsNumber})}/></label>
        <label>证书 SHA256 指纹（来自机器人连接配置）<input required pattern="[0-9a-fA-F]{64}" maxLength={64} value={connection.certificate_sha256} disabled={!!action} onChange={e=>setConnection({...connection,certificate_sha256:e.target.value})}/></label>
        <label>连接密钥（来自配套 token 文件，每次保存需填写）<input required type="password" autoComplete="off" pattern="[0-9a-fA-F]{64}" maxLength={64} value={token} disabled={!!action} onChange={e=>setToken(e.target.value)}/></label>
      </div>
      {settingsError&&<p role="alert">{settingsError}</p>}
      <div className="kuavo-actions"><ActionButton kind="secondary" disabled={!!action} onClick={()=>{setToken('');setConnectionOpen(false)}}>取消</ActionButton><button className="action-button" type="submit" disabled={locked}>{action==='connection'?'保存中…':'保存'}</button></div>
    </form></div>}

    <section className="surface teleop-control-panel">
      <div className="teleop-setup-flow kuavo-setup-flow">
        <section className="teleop-setup-section kuavo-settings">
          <div className="teleop-setup-label"><span>01</span><strong>遥操作设置</strong><small>乐聚 KUAVO 5-W</small></div>
          <div className="kuavo-settings-row kuavo-parameter-row">
            <label>操作手臂<select aria-label="操作手臂" value={armMode} disabled={locked} onChange={e=>setArmMode(e.target.value)}><option value="dual">双臂</option><option value="left">单臂 · 左臂</option><option value="right">单臂 · 右臂</option></select></label>
            <label>位移倍率<input aria-label="位移倍率" type="number" min="0.1" max="1" step="0.1" value={positionScale} disabled={locked} onChange={e=>{const v=Number(e.target.value);if(Number.isFinite(v)&&v>=.1&&v<=1)setPositionScale(v)}}/></label>
            <label>速度 / 加速度<span>{status.reference ? `${status.reference.max_velocity_rad_s} / ${status.reference.planning_acceleration_rad_s2}` : '—'}</span></label>
            <ActionButton kind="secondary" disabled={locked} onClick={()=>{setGrippers(gripperDraft(status.gripper_settings?.values||status.reference?.grippers));setSettingsError('');setParametersOpen(true)}}>其他参数</ActionButton>
          </div>
          <p className="kuavo-device-hint">手臂速度 rad/s、加速度 rad/s² 沿用乐聚正式配置；夹爪设置在其他参数中修改。</p>
          {armMode!=='dual'&&<p className="kuavo-device-hint">另一侧 Grip / Trigger 无效；Y 双臂回位禁用，B 取消仍有效。</p>}
          <div className="kuavo-settings-row">
            <select aria-label="KUAVO 网络方式" value={namespace} disabled={locked} onChange={e=>setNamespace(e.target.value)}>
              <option value="current">本电脑网络（默认）</option><option value="kuavons">原项目专用网络 kuavons</option>
            </select>
            <ActionButton kind="secondary" disabled={locked} onClick={()=>{setConnection(status.connection||{host:'192.168.26.1',port:7443,certificate_sha256:''});setToken('');setSettingsError('');setConnectionOpen(true)}}>机器人连接</ActionButton>
          </div>
          <p className="kuavo-device-hint">{status.connection?.configured?`已配置机器人地址：${status.connection.host}:${status.connection.port}`:'新电脑首次使用，请先配置机器人连接。'}</p>
          {status.gripper_settings?.saved&&<p className="kuavo-device-hint">{status.gripper_settings.message}</p>}
          <label className="kuavo-bus-condition"><input type="checkbox" checked={accepted} disabled={locked} onChange={e=>setAccepted(e.target.checked)}/> 接受原项目夹爪与手臂共用 CAN 总线的使用条件（兼容性尚未完成验证）</label>
        </section>
        <section className="teleop-setup-section teleop-devices-section">
          <div className="teleop-setup-label"><span>02</span><strong>所需设备</strong><small>{devices.filter(device=>device.ready).length}/3</small></div>
          <div className="teleop-required-devices">{devices.map(device=><article key={device.name} title={device.detail} className={'teleop-required-device '+(device.ready?'device-connected':'device-offline')}><span>{device.name}</span><strong>{checking?'检测中':device.ready?'已连接':'未连接'}</strong></article>)}</div>
          <p className="kuavo-device-hint">点击检查设备会唤醒 PICO、核验 USB 和 VR 输入，必要时恢复本机 VR 接收服务；空闲时显示最近检查结果，遥操中显示实时输入状态。</p>
          {status.vr_checked&&(!status.vr_ready||!status.pico_ready)&&<p className="kuavo-device-hint" role="status">{[status.pico_ready?'':status.pico_detail,status.vr_ready?'':status.vr_detail].filter(Boolean).join('；')}</p>}
        </section>
      </div>
      {(error||status.error)&&<div className="inline-alert teleop-panel-alert" role="alert">{error||status.error}</div>}
      <div className="teleop-control-footer">
        <span className="compact-status" role="status">会话：{sessionLabel} · 本地日志自动保存</span>
        <div className="teleop-control-actions">
          <ActionButton kind="secondary" disabled={locked} busy={action==='check'} onClick={()=>void command('check')}>检查设备</ActionButton>
          {active ? <ActionButton danger disabled={!status.session_id || status.state==='stopping'} onClick={()=>void command('stop')}>{status.state==='stopping'?'正在结束…':'结束遥操作'}</ActionButton> :
            <ActionButton disabled={locked || !!(status.gripper_settings?.saved&&!status.gripper_settings.matches_service) || !accepted || !status.vr_ready || !status.pico_ready || !status.pico_sdk_available || !status.service?.management_ready || !status.service?.service?.ready_to_start} onClick={()=>void command('start')}>开始遥操作</ActionButton>}
        </div>
      </div>
    </section>
    <div className="teleop-observation-grid kuavo-observation-grid">
      <section className="surface teleop-visualizer">
        <div className="preview-head"><strong>机器人姿态</strong><span>● {status.pose_live?'双臂遥测在线':status.pose_available?'反馈已过期':'参考姿态'}</span></div>
        <div className="kuavo-model-stage"><KuavoRobotPreview joints={status.telemetry?.joints||{}} darkMode={darkMode}/></div>
        <p className="kuavo-device-hint">官方 v63 模型 · 非手臂部位为参考零位</p>
      </section>
      <section className="surface teleop-camera">
        <div className="preview-head"><strong>相机预览</strong><span>待接入</span></div>
        <div className="kuavo-empty-cameras" aria-label="相机画面预留"><div/><div/><div/></div>
      </section>
    </div>
    <section className="surface teleop-manual">
      <div className="preview-head"><strong>手动控制</strong></div>
      <div className="teleop-manual-body">
        <div className="kuavo-preparation">
          <strong>主控与夹爪准备</strong>
          <div className="kuavo-actions">{[
            ['start','启动主控'],['initialize','厂家初始化'],['grippers_inspect','读取夹爪状态'],['grippers_prepare','准备夹爪'],
          ].map(([key,label])=><ActionButton key={key} kind="secondary" disabled={locked||!status.service?.management_ready||(key.startsWith('grippers')&&!accepted)} onClick={()=>void command('prepare',key)}>{label}</ActionButton>)}</div>
          <p>沿用乐聚准备接口；已有主控时不要重复启动。检查设备仅查询服务和准备状态。</p>
          {Object.keys(status.preparation||{}).length>0&&<details><summary>查看准备结果</summary><pre>{JSON.stringify(status.preparation,null,2)}</pre></details>}
        </div>
        <div className="kuavo-actions"><button type="button" className="action-button button-secondary" disabled>关节微调 · 待接入</button><button type="button" className="action-button button-secondary" disabled>姿态保存 / 复位 · 待接入</button></div>
        <p>独立手动控制接口尚未接入；双臂会话沿用 Y 回位 / B 取消，单臂禁用 Y。</p>
      </div>
    </section>
    <section className="surface kuavo-info">
      <strong>手柄操作 · 使用乐聚原映射</strong>
      <p>Grip：控制同侧手臂，两侧可同时握持。Trigger：独立接管夹爪，0 张开、1 闭合。<br/>开始时先松开 Grip 与 Trigger。左 Y：双臂模式下松开两侧 Grip 和 Trigger 后回位（单臂禁用）；右 B：取消回位。<br/>A/X 不映射为天机 Marvin M6 的采集按钮；结束会话请使用本页按钮。</p>
    </section>
    <section className="surface kuavo-info">
      <strong>本地记录</strong>
      <p>{status.log_directory ? '会话日志：'+status.log_directory : '启动会话后自动保存输入、机器人回执、退出报告；暂不生成同步图像数据集或上传平台。'}</p>
      <details><summary>日志与待接入说明</summary><ul>{status.limitations?.map(text=><li key={text}>{text}</li>)}</ul><pre>{status.logs?.slice(-12).join('\n')||'暂无日志'}</pre></details>
    </section>
  </div>;
}
