'use client';
import {useEffect, useRef, useState} from 'react';
import {ActionButton} from '../CollectionUi';

export type SessionSettings = {
  values:Record<string,number>;
  schema:Record<string,{label:string;unit:string;min:number;max:number;min_exclusive?:boolean;step?:number}>;
  revision:number;settings_sha256:string;service_instance_id:string;installation_sha256:string;
  editable:boolean;locked_reason?:string;
};
const fieldOrder=['arm_scale','arm_speed_rad_s','arm_acceleration_rad_s2','gripper_speed_rad_s','gripper_acceleration_rad_s2','gripper_open_target_rad','gripper_closed_target_rad'];
export function KuavoSessionSettingsDialog({request,onClose}:{
  request:(body:Record<string,unknown>)=>Promise<{session_settings?:SessionSettings;settings_preview?:unknown}>;
  onClose:()=>void;
}) {
  const dialog=useRef<HTMLDialogElement>(null);
  const [form,setForm]=useState<SessionSettings>();
  const [draft,setDraft]=useState<Record<string,string>>({});
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  async function send(action:string) {
    if(busy)return;
    const body:Record<string,unknown>={action};
    try {
      if(action!=='read') {
        if(!form)throw new Error('请先读取机器人参数');
        Object.assign(body,{expected_revision:form.revision,expected_service_instance_id:form.service_instance_id,expected_installation_sha256:form.installation_sha256});
        if(action==='preview'||action==='apply') {
          const changes:Record<string,number>={};
          for(const [key,spec] of Object.entries(form.schema)) {
            const text=draft[key]??'',n=Number(text);
            if(!text.trim()||!Number.isFinite(n)||n<spec.min||n>spec.max||(spec.min_exclusive&&n===spec.min))throw new Error(spec.label+'超出范围或不是有效数字');
            if(n!==form.values[key])changes[key]=n;
          }
          if(!Object.keys(changes).length){onClose();return}
          body.changes=changes;
        }
        if(action==='apply'||action==='reset')body.confirm=true;
      }
    } catch(e){setError(String(e));return}
    setBusy(true);setError('');
    try {
      const result=await request(body);
      setForm(result.session_settings);
      setDraft(Object.fromEntries(Object.entries(result.session_settings?.values||{}).map(([k,v])=>[k,String(v)])));
      if(action==='apply')onClose();
    } catch(e){setError(String(e))}
    finally{setBusy(false)}
  }
  // A fresh read on each open; writes only occur through the explicit buttons.
  useEffect(()=>{dialog.current?.showModal();void send('read')},[]);
  const labels:Record<string,string>={arm_scale:'臂位移倍率',arm_speed_rad_s:'臂速度',arm_acceleration_rad_s2:'臂加速度',gripper_speed_rad_s:'夹爪速度',gripper_acceleration_rad_s2:'夹爪加速度',gripper_open_target_rad:'夹爪打开角度',gripper_closed_target_rad:'夹爪关闭角度'};
  return <dialog ref={dialog} className="tianji-parameter-dialog" aria-label="其它参数"
    onCancel={e=>{e.preventDefault();if(!busy)onClose()}}
    onClick={e=>{if(busy||e.target!==e.currentTarget)return;const r=e.currentTarget.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)onClose()}}>
    <div className="tianji-parameter-header"><h3>其它参数</h3></div>
    <fieldset disabled={busy||!form?.editable}><div className="tianji-parameter-grid">
      {Object.entries(form?.schema||{}).filter(([key])=>key.includes('gripper_')).sort(([a],[b])=>fieldOrder.indexOf(a.replace(/^(left|right)_/,''))-fieldOrder.indexOf(b.replace(/^(left|right)_/,''))||a.localeCompare(b)).map(([key,spec])=>{
        const label=(key.startsWith('left_')?'左':'右')+(labels[key.replace(/^(left|right)_/,'')]||spec.label);
        const unit=spec.unit==='x'?'倍':spec.unit==='rad/s^2'?'rad/s²':spec.unit;
        return <label className="field" key={key}><span>{label}</span><div className="tianji-number"><input type="number" aria-label={label} title={unit} min={spec.min} max={spec.max} step="any" disabled={busy||!form?.editable} value={draft[key]??''} onChange={e=>{setDraft({...draft,[key]:e.target.value});setError('')}}/><small>{unit}</small></div></label>;
      })}
    </div>
    </fieldset>
    <div className="parameter-dialog-actions">
      <ActionButton disabled={busy||!form?.editable} onClick={()=>void send('apply')}>确认</ActionButton>
      <ActionButton kind="secondary" disabled={busy} onClick={onClose}>取消</ActionButton>
    </div>
    {error&&<p role="alert">{error}</p>}
    {form?.locked_reason&&<p role="status">{form.locked_reason}</p>}
  </dialog>;
}
