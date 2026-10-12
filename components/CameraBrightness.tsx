'use client';

import { LoaderCircle, RefreshCw, RotateCcw, Sun } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { requestJson } from '../lib/http';

interface SupportedCameraBrightness {
  supported: true;
  value: number;
  min: number;
  max: number;
  step: number;
  default: number;
  message?: string;
}
export type CameraBrightnessState = SupportedCameraBrightness | { supported: false; message?: string };

// Match the camera's advertised integer control, including non-unit steps.
export function brightnessValue(raw: string, control: SupportedCameraBrightness): number | null {
  if (!raw.trim() || !Number.isFinite(Number(raw))) return null;
  const bounded = Math.min(control.max, Math.max(control.min, Number(raw)));
  const steps = Math.min(
    Math.floor((control.max - control.min) / control.step),
    Math.round((bounded - control.min) / control.step),
  );
  return control.min + steps * control.step;
}

export function CameraBrightnessControls({
  label, control, draft, busy, disabled, message, error, onChange, onCommit, onRetry,
}: {
  label: string;
  control: CameraBrightnessState | null;
  draft: string;
  busy: 'loading' | 'saving' | null;
  disabled: boolean;
  message: string;
  error: string;
  onChange: (value: string) => void;
  onCommit: (value: string) => void;
  onRetry: () => void;
}) {
  const rangeValue = control?.supported ? brightnessValue(draft, control) ?? control.value : 0;
  const range = control?.supported ? { min: control.min, max: control.max, step: control.step } : {};
  return (
    <section className="umi-camera-brightness" aria-label={`${label}亮度调节`} aria-busy={Boolean(busy)}>
      <div className="umi-camera-brightness-row">
        <span className="umi-camera-brightness-label"><Sun size={14} aria-hidden="true" />亮度</span>
        <input
          aria-label={`${label}亮度滑块`} type="range" {...range}
          value={rangeValue} disabled={disabled}
          onChange={(event) => onChange(event.currentTarget.value)}
          onPointerUp={(event) => onCommit(event.currentTarget.value)}
          onKeyUp={(event) => {
            if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) onCommit(event.currentTarget.value);
          }}
          onBlur={(event) => onCommit(event.currentTarget.value)}
        />
        <input
          aria-label={`${label}亮度数值`} type="number" {...range}
          value={draft} disabled={disabled} placeholder="—" inputMode="numeric"
          onChange={(event) => onChange(event.currentTarget.value)}
          onBlur={(event) => onCommit(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              onCommit(event.currentTarget.value);
            }
          }}
        />
        <button className="umi-camera-brightness-reset" type="button" disabled={disabled}
          aria-label={`${label}恢复默认亮度`} title={control?.supported ? `恢复默认亮度 ${control.default}` : '恢复默认亮度'}
          onClick={() => { if (control?.supported) onCommit(String(control.default)); }}>
          <RotateCcw size={13} aria-hidden="true" />
        </button>
        <span className="umi-camera-brightness-status" role="status" aria-label={busy === 'saving' ? '正在设置亮度' : busy === 'loading' ? '正在读取亮度' : undefined}>
          {busy ? <LoaderCircle size={13} className="spin" aria-hidden="true" /> : null}
        </span>
      </div>
      {error ? <p className="umi-camera-brightness-error" role="alert"><span>{error}</span><button type="button" onClick={onRetry} disabled={Boolean(busy)} aria-label={`重新读取${label}亮度`}><RefreshCw size={12} aria-hidden="true" />重试</button></p> : message ? <p className="umi-camera-brightness-message">{message}</p> : null}
    </section>
  );
}

function ActiveCameraBrightness({ baseUrl, cameraKey, label, active }: {
  baseUrl: string;
  cameraKey: string;
  label: string;
  active: boolean;
}) {
  const [control, setControl] = useState<CameraBrightnessState | null>(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState<'loading' | 'saving' | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const timeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const saving = useRef(false);
  const generation = useRef(0);
  const path = `/umi/camera-controls/${encodeURIComponent(cameraKey)}/brightness`;

  const clearPending = useCallback(() => {
    if (timeout.current !== null) clearTimeout(timeout.current);
    timeout.current = null;
  }, []);

  useEffect(() => {
    const requestGeneration = ++generation.current;
    const abort = new AbortController();
    controller.current = abort;
    saving.current = false;
    setControl(null);
    setDraft('');
    setError('');
    setBusy(active ? 'loading' : null);
    if (active) {
      void requestJson<CameraBrightnessState>(baseUrl, path, { signal: abort.signal })
        .then((result) => {
          if (generation.current !== requestGeneration) return;
          setControl(result);
          setDraft(result.supported ? String(result.value) : '');
        })
        .catch((failure: unknown) => {
          if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : '无法读取亮度');
        })
        .finally(() => {
          if (generation.current === requestGeneration) setBusy(null);
        });
    }
    return () => {
      ++generation.current;
      abort.abort();
      clearPending();
    };
  }, [active, baseUrl, path, retry, clearPending]);

  const commit = useCallback((raw: string) => {
    clearPending();
    const abort = controller.current;
    if (!active || !control?.supported || saving.current || !abort || abort.signal.aborted) return;
    const value = brightnessValue(raw, control);
    if (value === null) {
      setDraft(String(control.value));
      return;
    }
    setDraft(String(value));
    if (value === control.value) return;
    const requestGeneration = generation.current;
    saving.current = true;
    setBusy('saving');
    setError('');
    void requestJson<CameraBrightnessState>(baseUrl, path, {
      method: 'POST', body: JSON.stringify({ value }), signal: abort.signal,
    }).then((result) => {
      if (generation.current !== requestGeneration) return;
      setControl(result);
      setDraft(result.supported ? String(result.value) : '');
    }).catch((failure: unknown) => {
      if (abort.signal.aborted) return;
      setDraft(String(control.value));
      setError(failure instanceof Error ? failure.message : '亮度设置失败');
    }).finally(() => {
      if (generation.current !== requestGeneration) return;
      saving.current = false;
      setBusy(null);
    });
  }, [active, baseUrl, path, control, clearPending]);

  const change = (raw: string) => {
    setDraft(raw);
    clearPending();
    if (!control?.supported || raw.trim() === '') return;
    const value = Number(raw);
    // Let users finish typing a negative sign or an out-of-range number.
    // Blur/Enter will normalize it to the hardware's allowed range and step.
    if (!Number.isFinite(value) || value < control.min || value > control.max) return;
    timeout.current = setTimeout(() => commit(raw), 350);
  };

  return <CameraBrightnessControls
    label={label} control={control} draft={draft} busy={busy}
    disabled={!active || !control?.supported || Boolean(busy)}
    message={!active ? '等待相机连接' : control && !control.supported ? control.message || '此相机不支持亮度调节' : ''}
    error={error} onChange={change} onCommit={commit} onRetry={() => setRetry((value) => value + 1)}
  />;
}

export function CameraBrightness(props: { baseUrl: string; cameraKey: string; label: string; active: boolean }) {
  // A different device must never inherit a draft or an in-flight write from
  // the previously selected camera; camera streaming remains a sibling.
  return <ActiveCameraBrightness key={`${props.baseUrl}/${props.cameraKey}`} {...props} />;
}
