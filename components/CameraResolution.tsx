'use client';
import { useEffect, useRef, useState } from 'react';
import { requestJson } from '../lib/http';

type Mode = { id: string; label: string; width: number; height: number; fps: number };
type Resolution = { options: Mode[]; selected: Mode | null; editable: boolean };
export function CameraResolution({baseUrl, cameraKey, label, active, editable = true, onBusyChange}: {
  baseUrl: string; cameraKey: string; label: string; active: boolean; editable?: boolean;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [value, setValue] = useState<Resolution | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const callback = useRef(onBusyChange);
  callback.current = onBusyChange;
  const path = `/umi/camera-controls/${encodeURIComponent(cameraKey)}/resolution`;
  useEffect(() => {
    const current = ++generation.current;
    const abort = new AbortController();
    setValue(null); setError(''); setBusy(active);
    callback.current?.(active);
    if (active) void requestJson<Resolution>(baseUrl, path, { signal: abort.signal })
      .then(result => { if (current === generation.current) setValue(result); })
      .catch(e => { if (!abort.signal.aborted) setError(String(e.message || e)); })
      .finally(() => { if (current === generation.current) { setBusy(false); callback.current?.(false); } });
    return () => { ++generation.current; abort.abort(); callback.current?.(false); };
  }, [baseUrl, path, active, retry]);
  async function select(mode_id: string) {
    if (busy || !active || !editable) return;
    const current = generation.current;
    setBusy(true); setError(''); callback.current?.(true);
    try {
      const result = await requestJson<Resolution>(baseUrl, path, {method:'POST', body:JSON.stringify({mode_id})});
      if (current === generation.current) setValue(result);
    } catch (e) {
      if (current === generation.current) setError(e instanceof Error ? e.message : '分辨率设置失败');
    } finally {
      if (current === generation.current) { setBusy(false); callback.current?.(false); }
    }
  }
  return <section className="umi-camera-brightness" aria-label={`${label}分辨率设置`} aria-busy={busy}>
    <div className="umi-camera-brightness-row">
      <span className="umi-camera-brightness-label">录制分辨率</span>
      <select style={{flex:1,minWidth:0}} aria-label={`${label}采集分辨率`}
        value={value?.selected?.id || ''} disabled={!active || !editable || !value?.editable || busy}
        onChange={e => void select(e.target.value)}>
        {!value?.options.length ? <option value={value?.selected?.id || ''}>{value?.selected?.label || '等待相机连接'}</option> :
          value.options.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}
      </select>
      <button type="button" disabled={!active || busy} onClick={() => setRetry(n => n+1)}>刷新</button>
    </div>
    <p className={error ? 'umi-camera-brightness-error' : 'umi-camera-brightness-message'} role={error ? 'alert' : 'status'}>
      {error || (busy ? '正在读取 / 验证实际画面…' : editable ? '按所选分辨率录制 · 采集前验证帧率' : '采集期间锁定；返回检查硬件可修改')}
    </p>
  </section>;
}
