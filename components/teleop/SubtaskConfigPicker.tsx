'use client';
import { useEffect, useRef, useState } from 'react';
import { requestJson } from '../../lib/http';

export type SubtaskConfig = { name: string; labels: string[] };
type SavedConfig = SubtaskConfig & { id: string };
export function SubtaskConfigPicker({baseUrl, value, onChange}: {baseUrl:string; value:SavedConfig|null; onChange:(value:SavedConfig|null)=>void}) {
  const [items,setItems]=useState<SavedConfig[]>([]);
  const [open,setOpen]=useState(false);
  const [editing,setEditing]=useState(false);
  const [name,setName]=useState('');
  const [labels,setLabels]=useState<string[]>(['']);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{const abort=new AbortController();void requestJson<SavedConfig[]>(baseUrl,'/subtask-configs',{signal:abort.signal}).then(setItems).catch(()=>{if(!abort.signal.aborted)setError('无法读取子任务配置，请重试');});return()=>abort.abort();},[baseUrl]);
  useEffect(()=>{if(editing)dialog.current?.showModal();else dialog.current?.close();},[editing]);
  async function remove(item:SavedConfig) {
    setBusy(true);setError('');
    try {await requestJson(baseUrl,`/subtask-configs/${item.id}/delete`,{method:'POST'});setItems(items.filter(x=>x.id!==item.id));if(value?.id===item.id)onChange(null);}
    catch {setError('删除失败，请重试');}finally{setBusy(false);}
  }
  async function save() {
    setBusy(true);setError('');
    try {const item=await requestJson<SavedConfig>(baseUrl,'/subtask-configs',{method:'POST',body:JSON.stringify({name:name.trim(),labels:labels.map(x=>x.trim())})});setItems([...items,item]);onChange(item);setEditing(false);}
    catch {setError('保存失败，请检查名称后重试');}finally{setBusy(false);}
  }
  function move(index:number,delta:number) {const next=[...labels];[next[index],next[index+delta]]=[next[index+delta],next[index]];setLabels(next);}
  return <div className="field subtask-config-picker">
    <span>子任务标注配置</span>
    <div className="subtask-picker-row">
      <div className="subtask-dropdown">
        <button type="button" className="action-button" aria-expanded={open} onClick={()=>setOpen(!open)}>{value?.name??'不使用子任务标注'} ▾</button>
        {open&&<div className="subtask-options" onKeyDown={e=>{if(e.key==='Escape')setOpen(false);}}>
          <button type="button" disabled={busy} onClick={()=>{onChange(null);setOpen(false);}}>不使用子任务标注</button>
          {items.map(item=><div key={item.id} className="subtask-option"><button type="button" disabled={busy} aria-pressed={value?.id===item.id} onClick={()=>{onChange(item);setOpen(false);}}>{item.name}</button><button type="button" disabled={busy} aria-label={`删除配置 ${item.name}`} onClick={()=>void remove(item)}>删除</button></div>)}
        </div>}
      </div>
      <button type="button" className="action-button" disabled={busy} onClick={()=>{setOpen(false);setName('');setLabels(['']);setError('');setEditing(true);}}>新建配置</button>
    </div>
    {value&&<small>{value.labels.join(' → ')} · X 标注本段，Y 重置顺序；A 开始，B 保存</small>}
    {error&&!editing&&<small role="alert">{error}</small>}
    <dialog ref={dialog} className="subtask-config-dialog" onKeyDown={e=>{if(e.key==='Enter' && e.target instanceof HTMLInputElement)e.preventDefault();}} onCancel={e=>{if(busy)e.preventDefault();else setEditing(false);}}>
      <h2>新建子任务标注配置</h2>
      <label className="field"><span>配置名称</span><input value={name} maxLength={120} onChange={e=>setName(e.target.value)} disabled={busy}/></label>
      <div className="subtask-cards">{labels.map((label,index)=><div className="subtask-card" key={index}>
        <span>{index+1}</span><input aria-label={`子任务 ${index+1} 名称`} placeholder="自定义子任务名称" maxLength={120} value={label} disabled={busy} onChange={e=>setLabels(labels.map((x,i)=>i===index?e.target.value:x))}/>
        <button type="button" disabled={busy||index===0} onClick={()=>move(index,-1)}>上移</button>
        <button type="button" disabled={busy||index===labels.length-1} onClick={()=>move(index,1)}>下移</button>
        <button type="button" disabled={busy||labels.length===1} onClick={()=>setLabels(labels.filter((_,i)=>i!==index))}>删除</button>
      </div>)}</div>
      <button type="button" className="action-button" disabled={busy||labels.length>=100} onClick={()=>setLabels([...labels,''])}>添加子任务卡片</button>
      <p>直接编辑卡片名称即可重命名。X 按顺序循环标注，Y 让下一次 X 从第一项开始；未确认的尾段保持未标注。</p>
      {error&&<p role="alert">{error}</p>}
      <div className="stage-actions"><button type="button" className="action-button" disabled={busy} onClick={()=>setEditing(false)}>取消</button><button type="button" className="action-button button-primary" disabled={busy||!name.trim()||labels.some(x=>!x.trim())} onClick={()=>void save()}>{busy?'保存中…':'确认保存'}</button></div>
    </dialog>
  </div>;
}
