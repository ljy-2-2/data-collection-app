'use client';
import { useEffect, useRef, useState } from 'react';
import { ActionButton } from '../CollectionUi';
import { brightnessValue, type CameraBrightnessState } from '../CameraBrightness';
import { requestJson } from '../../lib/http';

const defaultRoles = [{id:'usb_1',label:'主相机'},{id:'usb_4',label:'左相机'},{id:'usb_2',label:'右相机'}];
type Mode = { id: string; label: string };
export type CameraSettings = { brightness: CameraBrightnessState; resolution: { options: Mode[]; selected: Mode; editable: boolean } };
type Draft = {mode_id:string;brightness:string};
function checked(value: CameraSettings) {
  if (!value || typeof value.brightness?.supported!=='boolean' || !value.resolution?.options?.length || !value.resolution.selected) throw new Error('相机设置响应无效');
  return value;
}
export function TianjiCameraSettings({baseUrl,open,onClose,cameras,editable,onBusyChange,onApplyStatus,cameraRoles=defaultRoles,loadSettings,applySettings}: {
  baseUrl:string;open:boolean;onClose:()=>void;cameras:{id:string;connected?:boolean}[];editable:boolean;
  onBusyChange:(key:string,busy:boolean)=>void;
  onApplyStatus:(message:string,error:string)=>void;
  cameraRoles?:{id:string;label:string}[];
  loadSettings?:(id:string,signal:AbortSignal)=>Promise<CameraSettings>;
  applySettings?:(changes:Record<string,{mode_id?:string;brightness?:number}>)=>Promise<Record<string,CameraSettings>>;
}) {
  const roles=cameraRoles;
  const loader=useRef(loadSettings);loader.current=loadSettings;
  const dialog=useRef<HTMLDialogElement>(null);
  const [values,setValues]=useState<Record<string,CameraSettings>>({});
  const [drafts,setDrafts]=useState<Record<string,Draft>>({});
  const [errors,setErrors]=useState<Record<string,string>>({});
  const [message,setMessage]=useState('');
  const [loading,setLoading]=useState(false);
  const [saving,setSaving]=useState(false);
  const savingRef=useRef(false);
  const retryDrafts=useRef<Record<string,Draft>|null>(null);
  const connected=useRef(cameras);connected.current=cameras;
  const callback=useRef(onBusyChange);callback.current=onBusyChange;
  const isConnected=(id:string)=>cameras.some(c=>c.id===id&&c.connected!==false);
  useEffect(()=>{
    if (!open) {dialog.current?.close();return;}
    dialog.current?.showModal();
    const abort=new AbortController();
    setValues({});setDrafts(retryDrafts.current||{});setErrors({});setMessage('');setLoading(true);callback.current('dialog',true);
    void (async()=>{
      for (const role of roles) {
        if (abort.signal.aborted) break;
        if (!connected.current.some(c=>c.id===role.id&&c.connected!==false)) continue;
        try {
          const value=checked(await (loader.current?loader.current(role.id,abort.signal):requestJson<CameraSettings>(baseUrl,`/tianji/camera-controls/${role.id}/settings`,{signal:abort.signal})));
          if (!abort.signal.aborted) {
            setValues(previous=>({...previous,[role.id]:value}));
            setDrafts(previous=>({...previous,[role.id]:retryDrafts.current?.[role.id]||{mode_id:value.resolution.selected.id,brightness:value.brightness.supported?String(value.brightness.value):''}}));
          }
        } catch(failure) {if (!abort.signal.aborted) setErrors(previous=>({...previous,[role.id]:String(failure)}));}
      }
      if (!abort.signal.aborted) {setLoading(false);callback.current('dialog',false);}
    })();
    return ()=>{abort.abort();callback.current('dialog',false);};
  },[open,baseUrl]);
  function draft(id:string,change:Partial<Draft>) {setDrafts(previous=>({...previous,[id]:{...previous[id],...change}}));}
  function cancel() {retryDrafts.current=null;onClose();}
  async function confirm() {
    if (!editable||loading||savingRef.current) return;
    const changes:{id:string;data:{mode_id?:string;brightness?:number}}[]=[];
    for (const role of roles) {
      const value=values[role.id],next=drafts[role.id];
      if (!value||!next) continue;
      const data:{mode_id?:string;brightness?:number}={};
      if (value.resolution.editable&&next.mode_id!==value.resolution.selected.id) {
        if (!value.resolution.options.some(mode=>mode.id===next.mode_id)) {setMessage(role.label+'：请选择支持的分辨率');return;}
        data.mode_id=next.mode_id;
      }
      if (value.brightness.supported) {
        const number=brightnessValue(next.brightness,value.brightness);
        if (next.brightness.trim()===''||number===null||number!==Number(next.brightness)) {setMessage(role.label+'：亮度需在支持范围内且符合步长');return;}
        if (number!==value.brightness.value) data.brightness=number;
      }
      if (Object.keys(data).length) {
        if (!isConnected(role.id)) {setMessage(role.label+'已断开，未提交修改');return;}
        changes.push({id:role.id,data});
      }
    }
    if (!changes.length) {retryDrafts.current=null;onClose();return;}
    retryDrafts.current=Object.fromEntries(changes.map(change=>[change.id,{...drafts[change.id]}]));
    savingRef.current=true;setSaving(true);setMessage('');callback.current('apply',true);
    onApplyStatus('正在应用相机参数…','');
    dialog.current?.close();onClose();
    try {
      const changesById=Object.fromEntries(changes.map(change=>[change.id,change.data]));
      const results=await (applySettings?applySettings(changesById):requestJson<Record<string,CameraSettings>>(baseUrl,'/tianji/camera-controls/settings',{
        method:'POST',body:JSON.stringify({changes:changesById}),
      }));
      if (!results||Object.keys(results).length!==changes.length) throw new Error('相机返回无效的批量设置');
      for (const change of changes) checked(results[change.id]);
      for (const change of changes) {
        const result=results[change.id];
        setValues(previous=>({...previous,[change.id]:result}));
        setDrafts(previous=>({...previous,[change.id]:{mode_id:result.resolution.selected.id,brightness:result.brightness.supported?String(result.brightness.value):''}}));
      }
      retryDrafts.current=null;onApplyStatus('相机参数已应用','');
    } catch(failure) {
      const error='相机参数未完成：'+String(failure)+'。修改已保留，请先处理上述错误，再重新确认。';
      setMessage(error);onApplyStatus('相机参数应用失败',error);
    }
    finally {savingRef.current=false;setSaving(false);callback.current('apply',false);}
  }
  return <dialog ref={dialog} className="tianji-parameter-dialog tianji-camera-dialog" aria-labelledby="camera-settings-title"
    onCancel={event=>{event.preventDefault();if (!savingRef.current) cancel();}}>
    <h3 id="camera-settings-title">相机参数</h3>
    <div className="tianji-camera-settings-sections">{roles.map(role=>{
      const value=values[role.id],next=drafts[role.id],brightness=value?.brightness;
      const disabled=!editable||loading||saving||!isConnected(role.id);
      return <fieldset key={role.id} disabled={disabled}>
        <legend>{role.label}</legend>
        <label className="tianji-camera-setting-row"><span>录制分辨率</span><select aria-label={role.label+'录制分辨率'} value={next?.mode_id||''} disabled={!value?.resolution.editable} onChange={event=>draft(role.id,{mode_id:event.target.value})}>
          {!value?<option value="">等待读取相机设置</option>:value.resolution.options.map(mode=><option key={mode.id} value={mode.id}>{mode.label}</option>)}
        </select></label>
        <div className="tianji-camera-setting-row"><span>亮度</span><input type="range" aria-label={role.label+'亮度滑块'} disabled={!brightness?.supported} min={brightness?.supported?brightness.min:0} max={brightness?.supported?brightness.max:1} step={brightness?.supported?brightness.step:1} value={next?.brightness||0} onChange={event=>draft(role.id,{brightness:event.target.value})}/><input type="number" aria-label={role.label+'亮度数值'} disabled={!brightness?.supported} min={brightness?.supported?brightness.min:undefined} max={brightness?.supported?brightness.max:undefined} step={brightness?.supported?brightness.step:1} value={next?.brightness??''} onChange={event=>draft(role.id,{brightness:event.target.value})}/></div>
        <p role="status">{errors[role.id]||(!isConnected(role.id)?'相机未连接':loading?'正在读取相机设置…':brightness?.supported?`亮度范围 ${brightness.min} ～ ${brightness.max} · 步长 ${brightness.step}`:'相机不支持亮度调节')}</p>
      </fieldset>;
    })}</div>
    {message && <p role="alert">{message}</p>}
    <div className="stage-actions"><ActionButton kind="secondary" disabled={saving} onClick={cancel}>取消</ActionButton><ActionButton disabled={!editable||loading||saving} onClick={()=>void confirm()}>{saving?'正在应用…':'确认'}</ActionButton></div>
  </dialog>;
}
