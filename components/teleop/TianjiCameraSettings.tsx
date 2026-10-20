'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { CameraBrightnessControls, brightnessValue, type CameraBrightnessState } from '../CameraBrightness';
import { requestJson } from '../../lib/http';

type Mode = { id: string; label: string; width: number; height: number; fps: number };
type Settings = { brightness: CameraBrightnessState; resolution: { options: Mode[]; selected: Mode; editable: boolean } };

export function TianjiCameraSettings({ baseUrl, cameraKey, label, active, editable, blocked, onBusyChange }: {
  baseUrl: string; cameraKey: string; label: string; active: boolean; editable: boolean; blocked: boolean;
  onBusyChange: (key: string, busy: boolean) => void;
}) {
  const [value, setValue] = useState<Settings | null>(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const saving = useRef(false);
  const callback = useRef(onBusyChange);
  callback.current = onBusyChange;
  const path = `/tianji/camera-controls/${encodeURIComponent(cameraKey)}/settings`;
  const accept = useCallback((result: Settings) => {
    if (typeof result.brightness?.supported !== 'boolean' || !result.resolution?.options?.length || !result.resolution.selected) {
      throw new Error('相机设置响应无效');
    }
    setValue(result);setDraft(result.brightness.supported ? String(result.brightness.value) : '');
  }, []);
  useEffect(() => {
    const current = ++generation.current;
    const abort = new AbortController();
    if (active && editable) {
      setBusy(true);setError('');callback.current(cameraKey, true);
      void requestJson<Settings>(baseUrl, path, { signal: abort.signal })
        .then(result => { if (generation.current === current) accept(result); })
        .catch(failure => { if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : '读取相机设置失败'); })
        .finally(() => {
          if (generation.current === current) {setBusy(false);callback.current(cameraKey, false);}
        });
    }
    return () => {++generation.current;abort.abort();setBusy(false);callback.current(cameraKey, false);};
  }, [baseUrl, path, cameraKey, active, editable, retry, accept]);

  async function change(data: { mode_id?: string; brightness?: number }) {
    if (!active || !editable || blocked || busy || saving.current) return;
    const current = generation.current;
    saving.current = true;setBusy(true);setError('');callback.current(cameraKey, true);
    try {
      const result = await requestJson<Settings>(baseUrl, path, {method:'POST',body:JSON.stringify(data)});
      if (generation.current === current) accept(result);
    } catch (failure) {
      if (generation.current === current) {
        setError(failure instanceof Error ? failure.message : '相机设置失败');
        setDraft(value?.brightness.supported ? String(value.brightness.value) : '');
      }
    } finally {
      saving.current = false;
      if (generation.current === current) {setBusy(false);callback.current(cameraKey, false);}
    }
  }
  function commit(raw: string) {
    if (!value?.brightness.supported) return;
    const next = brightnessValue(raw, value.brightness);
    if (next === null) {setDraft(String(value.brightness.value));return;}
    setDraft(String(next));
    if (next !== value.brightness.value) void change({brightness:next});
  }
  return <fieldset style={{border:0,padding:0,margin:0,minWidth:0}} disabled={!active || !editable || blocked || busy}>
    <section className="umi-camera-brightness" aria-label={`${label}分辨率设置`} aria-busy={busy}>
      <div className="umi-camera-brightness-row">
        <span className="umi-camera-brightness-label">录制分辨率</span>
        <select style={{flex:1,minWidth:0}} aria-label={`${label}采集分辨率`} value={value?.resolution.selected.id || ''}
          disabled={!value?.resolution.editable} onChange={e => void change({mode_id:e.target.value})}>
          {!value ? <option value="">等待读取相机设置</option> : value.resolution.options.map(mode => <option key={mode.id} value={mode.id}>{mode.label}</option>)}
        </select>
        <button type="button" onClick={() => setRetry(n => n+1)}>刷新</button>
      </div>
      <p className="umi-camera-brightness-message" role="status">{busy ? '正在读取 / 验证实际画面…' : editable ? '按所选原生分辨率录制' : '遥操及采集期间锁定设置'}</p>
    </section>
    <CameraBrightnessControls label={label} control={value?.brightness || null} draft={draft} busy={busy ? 'loading' : null}
      disabled={!active || !editable || blocked || busy || !value?.brightness.supported} message="" error={error}
      onChange={setDraft} onCommit={commit} onRetry={() => setRetry(n => n+1)} />
  </fieldset>;
}
