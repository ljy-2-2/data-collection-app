'use client';
import {
  Settings2,
  ChevronUp,
  ChevronDown,
  X,
  Play,
  RefreshCw,
  Square,
  VideoOff,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ActionButton, StatusDot, classNames } from '../CollectionUi';
import { CameraStream } from '../CameraStream';
import { TianjiRobotPreview } from '../TianjiRobotPreview';
import { requestJson } from '../../lib/http';
import {
  TIANJI_CAMERAS,
  TIANJI_DEFAULTS,
  TIANJI_FIELDS,
  TIANJI_JOG_STEP_DEG,
  TIANJI_LIMITS,
  TIANJI_SIDES,
  validTianjiParameters,
  type TianjiMode,
  type TianjiParameters,
  type TianjiSide,
  type TianjiStatus,
} from '../../lib/tianji';

interface Pose {
  id: string;
  name: string;
  joints: Record<TianjiSide, number[]>;
}
const POSES_KEY = 'tianji-pose-presets-v1';
const primaryFields = TIANJI_FIELDS.slice(0, 3);
const otherFields = TIANJI_FIELDS.slice(3);
const emptyDevices: TianjiStatus['devices'] = [
  { id: 'robot', name: 'Tianji', status: 'offline' },
  { id: 'xr', name: 'Galaxis VR', status: 'offline' },
  { id: 'headset', name: 'Pico 设备', status: 'offline' },
];
const endpoints = {
  left: [-1.942282749675746, -0.751316090638591],
  right: [-0.478942549782559, 0.771915770199131],
};
function loadPoses(): Pose[] {
  if (typeof window === 'undefined') return [];
  try {
    const data = JSON.parse(localStorage.getItem(POSES_KEY) || '[]');
    return Array.isArray(data)
      ? data.filter(
          (p) =>
            typeof p.id === 'string' &&
            typeof p.name === 'string' &&
            TIANJI_SIDES.every(
              (s) =>
                Array.isArray(p.joints?.[s.id]) &&
                p.joints[s.id].length === 7 &&
                p.joints[s.id].every(
                  (n: unknown) => typeof n === 'number' && Number.isFinite(n),
                ),
            ),
        )
      : [];
  } catch {
    return [];
  }
}

export function TianjiTeleopWorkspace({
  task,
  baseUrl,
  darkMode,
  onBusyChange,
}: {
  task: string;
  baseUrl: string;
  darkMode: boolean;
  onBusyChange?: (busy: boolean) => void;
}) {
  const parameterDialog = useRef<HTMLDialogElement>(null);
  const [parametersOpen, setParametersOpen] = useState(false);
  useEffect(() => {
    if (parametersOpen) parameterDialog.current?.showModal();
    else parameterDialog.current?.close();
  }, [parametersOpen]);
  const [mode, setMode] = useState<TianjiMode>('dual');
  const [parameters, setParameters] = useState<TianjiParameters>({
    ...TIANJI_DEFAULTS,
  });
  const [saved, setSaved] = useState<TianjiParameters | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveFailure, setSaveFailure] = useState<{
    key: string;
    message: string;
  } | null>(null);
  const [state, setState] = useState<TianjiStatus | null>(null);
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const [camera, setCamera] = useState('usb_1');
  const [poses, setPoses] = useState<Pose[]>(loadPoses);
  const [selectedPose, setSelectedPose] = useState('zero');
  const mounted = useRef(true),
    requestSeq = useRef(0),
    actionRef = useRef('');
  const api = useCallback(
    <T,>(path: string, init?: RequestInit) =>
      requestJson<T>(baseUrl, `/tianji${path}`, init),
    [baseUrl],
  );
  const refresh = useCallback(async () => {
    const id = ++requestSeq.current;
    try {
      const next = await api<TianjiStatus>('/status', {
        signal: AbortSignal.timeout(5000),
      });
      if (mounted.current && id === requestSeq.current) {
        setState(next);
        if (next.state === 'running' && (next.upload_mode || next.mode))
          setMode(next.upload_mode ?? next.mode!);
      }
    } catch {
      if (mounted.current && id === requestSeq.current)
        setState((previous) =>
          previous && previous.state !== 'idle'
            ? {
                ...previous,
                state: 'unknown',
                joints: {},
                devices: emptyDevices,
                cameras: [],
                error: '连接中断，等待确认控制已停止',
              }
            : null,
        );
    }
  }, [api]);
  useEffect(() => {
    mounted.current = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (mounted.current) timer = setTimeout(poll, 50);
    };
    void poll();
    return () => {
      mounted.current = false;
      clearTimeout(timer);
    };
  }, [api, refresh]);
  const running = state?.state === 'running',
    moving = state?.state === 'moving';
  const collection = state?.collection;
  const collecting = Boolean(collection?.recording);
  const collectionBusy = Boolean(collection?.busy);
  const locked =
    Boolean(pending) ||
    collectionBusy ||
    running ||
    moving ||
    state?.state === 'checking' ||
    state?.state === 'unknown' ||
    state?.state === 'error';
  useEffect(() => {
    onBusyChange?.(locked || saving);
  }, [locked, saving, onBusyChange]);
  const devices = state?.devices ?? emptyDevices;
  const count = devices.filter((d) => d.status === 'connected').length;
  const live = devices.some(
    (d) => d.id === 'robot' && d.status === 'connected',
  );
  const parameterKey = JSON.stringify(parameters);
  const dirty =
    !saved || TIANJI_FIELDS.some(({ key }) => parameters[key] !== saved[key]);
  const valid = validTianjiParameters(parameters);
  const canMove = Boolean(state) && live && !locked && !saving && !dirty;
  // Each workspace starts with defaults. Serialize automatic saves, keeping
  // edits made during a request queued until its response has arrived.
  useEffect(() => {
    if (
      state?.state !== 'idle' ||
      pending ||
      saving ||
      !valid ||
      !dirty ||
      saveFailure?.key === parameterKey
    )
      return;
    const timer = setTimeout(() => {
      setSaving(true);
      setSaveFailure(null);
      void api<TianjiParameters>('/parameters', {
        method: 'POST',
        body: parameterKey,
        signal: AbortSignal.timeout(10000),
      })
        .then((result) => {
          if (!validTianjiParameters(result))
            throw new Error('参数保存响应无效');
          if (mounted.current) setSaved(result);
        })
        .catch((reason) => {
          if (mounted.current)
            setSaveFailure({
              key: parameterKey,
              message: `参数自动保存失败：${reason instanceof Error ? reason.message : '请检查本机服务'}`,
            });
        })
        .finally(() => {
          if (mounted.current) setSaving(false);
        });
    }, 350);
    return () => clearTimeout(timer);
  }, [
    api,
    state?.state,
    pending,
    saving,
    valid,
    dirty,
    parameterKey,
    saveFailure,
  ]);
  const selectedCamera = state?.cameras.find((c) => c.id === camera);
  const previewTransition = ['connecting', 'starting', 'streaming', 'cooldown', 'resuming', 'recovering'].includes(
    state?.camera_phase ?? '',
  );
  const frame = selectedCamera?.available || previewTransition
    ? `${baseUrl}/tianji/cameras/${camera}/stream`
    : '';
  const closure: Partial<Record<TianjiSide, number>> = {};
  for (const { id } of TIANJI_SIDES) {
    const f = state?.grippers?.motors[id]?.feedback;
    const n = f?.position_rad;
    if (
      typeof n === 'number' &&
      Number.isFinite(n) &&
      (f?.age_s ?? Infinity) < 0.3
    )
      closure[id] = Math.max(
        0,
        Math.min(
          1,
          (n - endpoints[id][0]) / (endpoints[id][1] - endpoints[id][0]),
        ),
      );
  }
  async function act(name: string, payload?: unknown) {
    if ((actionRef.current || saving) && name !== 'stop') return false;
    actionRef.current = name;
    setPending(name);
    setError('');
    try {
      await api(`/${name}`, {
        method: 'POST',
        body: payload === undefined ? undefined : JSON.stringify(payload),
        signal: AbortSignal.timeout(name === 'check' ? 110000 : 45000),
      });
      await refresh();
      if (name === 'check') setSaveFailure(null);
      return true;
    } catch (e) {
      if (mounted.current)
        setError(e instanceof Error ? e.message : '天机控制失败');
      return false;
    } finally {
      if (actionRef.current === name) {
        actionRef.current = '';
        if (mounted.current) setPending('');
      }
    }
  }
  async function resetPose() {
    if (!canMove) return;
    const selected = poses.find((p) => p.id === selectedPose);
    const label = selectedPose === 'zero' ? '零点' : selected?.name;
    if (
      !label ||
      !window.confirm(
        `将双臂移动到${label}，请确认运动范围内无人和障碍物。`,
      )
    )
      return;
    const targets = selected
      ? Object.fromEntries(
          TIANJI_SIDES.map(
            (s) => [s.id, selected.joints[s.id]],
          ),
        )
      : undefined;
    await act('move', {
      mode: 'dual',
      kind: selectedPose === 'zero' ? 'zero' : 'pose',
      targets,
    });
  }
  function persistPoses(next: Pose[]) {
    try {
      localStorage.setItem(POSES_KEY, JSON.stringify(next));
      setPoses(next);
      return true;
    } catch {
      setError('无法保存姿态，请检查本机存储空间');
      return false;
    }
  }
  function savePose() {
    const left = state?.joints.left,
      right = state?.joints.right;
    if (!live || !left || !right) return;
    const name = window.prompt('请输入姿态名称')?.trim();
    if (!name) return;
    const p = {
      id: crypto.randomUUID(),
      name: name.slice(0, 50),
      joints: { left: [...left], right: [...right] },
    };
    if (persistPoses([...poses, p])) setSelectedPose(p.id);
  }
  return (
    <div className="teleop-workspace tianji-workspace">
      <section className="surface teleop-control-panel">
        <div className="teleop-setup-flow teleop-g2-setup-flow">
          <section className="teleop-setup-section teleop-mode-section">
            <div className="teleop-setup-label">
              <span>01</span>
              <strong>上传曲线</strong>
              <small className="tianji-autosave-label" role="status">
                {saveFailure
                  ? '保存失败'
                  : !valid
                    ? '参数超出范围'
                    : saving || dirty
                      ? '自动保存中…'
                      : null}
              </small>
            </div>
            <select
              value={mode}
              disabled={locked}
              onChange={(e) => setMode(e.target.value as TianjiMode)}
              aria-label="天机上传曲线"
            >
              <option value="dual">双臂</option>
              <option value="left">左臂</option>
              <option value="right">右臂</option>
            </select>
            <div className="tianji-primary-parameters">
              <fieldset disabled={locked}>
                {primaryFields.map((f) => {
                  const value = parameters[f.key];
                  const inRange =
                    Number.isFinite(value) && value >= f.min && value <= f.max;
                  function adjust(direction: number) {
                    setParameters((p) => ({
                      ...p,
                      [f.key]: Number(
                        Math.min(
                          f.max,
                          Math.max(
                            f.min,
                            (Number.isFinite(p[f.key])
                              ? p[f.key]
                              : TIANJI_DEFAULTS[f.key]) +
                              direction * f.step,
                          ),
                        ).toFixed(2),
                      ),
                    }));
                  }
                  return (
                    <div className="field" key={f.key}>
                      <label htmlFor={`tianji-${f.key}`}>{f.label}</label>
                      <div className="tianji-stepper">
                        <input
                          id={`tianji-${f.key}`}
                          type="number"
                          min={f.min}
                          max={f.max}
                          step={f.step}
                          value={Number.isFinite(value) ? value : ''}
                          aria-invalid={!inRange}
                          aria-describedby={`tianji-${f.key}-range`}
                          onChange={(e) =>
                            setParameters((p) => ({
                              ...p,
                              [f.key]:
                                e.target.value === ''
                                  ? NaN
                                  : Number(e.target.value),
                            }))
                          }
                        />
                        <span>{f.unit}</span>
                        <div>
                          <button
                            type="button"
                            aria-label={`增加${f.label}`}
                            disabled={value >= f.max}
                            onClick={() => adjust(1)}
                          >
                            <ChevronUp size={12} />
                          </button>
                          <button
                            type="button"
                            aria-label={`减少${f.label}`}
                            disabled={value <= f.min}
                            onClick={() => adjust(-1)}
                          >
                            <ChevronDown size={12} />
                          </button>
                        </div>
                      </div>
                      <small
                        id={`tianji-${f.key}-range`}
                        className={inRange ? '' : 'tianji-range-error'}
                      >
                        范围 {f.min}–{f.max} {f.unit}
                      </small>
                    </div>
                  );
                })}
              </fieldset>
            </div>
            <ActionButton
              kind="secondary"
              disabled={locked}
              onClick={() => {
                setParametersOpen(true);
              }}
            >
              <Settings2 size={15} />
              其它参数
            </ActionButton>
          </section>
          <section className="teleop-setup-section teleop-devices-section">
            <div className="teleop-setup-label">
              <span>02</span>
              <strong>所需设备</strong>
              <small>{count}/3</small>
            </div>
            <div className="teleop-required-devices">
              {devices.map((d) => (
                <article
                  key={d.id}
                  title={d.id === 'robot' ? undefined : d.detail}
                  className={classNames(
                    'teleop-required-device',
                    `device-${d.status}`,
                  )}
                >
                  <span>{d.id === 'robot' ? 'Tianji' : d.name}</span>
                  <strong>
                    {pending === 'check' || state?.state === 'checking'
                      ? '检测中'
                      : d.status === 'connected'
                        ? '已连接'
                        : '未连接'}
                  </strong>
                </article>
              ))}
            </div>
          </section>
        </div>
        <dialog
          ref={parameterDialog}
          className="tianji-parameter-dialog"
          aria-labelledby="tianji-parameters-title"
          onCancel={(event) => {
            event.preventDefault();
            setParametersOpen(false);
          }}
        >
          <div className="tianji-parameter-header">
            <h3 id="tianji-parameters-title">其它参数</h3>
            <button
              type="button"
              className="tianji-dialog-close"
              aria-label="关闭其它参数"
              disabled={Boolean(pending)}
              onClick={() => setParametersOpen(false)}
            >
              <X size={18} />
            </button>
          </div>
          <fieldset disabled={locked}>
            <div className="tianji-parameter-grid">
              {otherFields.map((f) => (
                <label className="field" key={f.key}>
                  <span>{f.label}</span>
                  <div className="tianji-number">
                    <input
                      aria-label={f.label}
                      type="number"
                      min={f.min}
                      max={f.max}
                      step="any"
                      required
                      value={
                        Number.isFinite(parameters[f.key])
                          ? parameters[f.key]
                          : ''
                      }
                      onChange={(e) =>
                        setParameters((p) => ({
                          ...p,
                          [f.key]:
                            e.target.value === ''
                              ? NaN
                              : Number(e.target.value),
                        }))
                      }
                    />
                    <small>{f.unit}</small>
                  </div>
                </label>
              ))}
            </div>
          </fieldset>
          {saveFailure ? (
            <div className="inline-alert" role="alert">
              {saveFailure.message}
            </div>
          ) : null}
          <div className="tianji-autosave-status" role="status">
            {saveFailure
              ? '自动保存未完成，可通过检查设备重试'
              : !valid
                ? '请输入范围内的参数'
                : saving || dirty
                  ? '等待自动保存'
                  : null}
          </div>
        </dialog>
        {error || state?.error || saveFailure ? (
          <div className="inline-alert teleop-panel-alert" role="alert">
            {error || state?.error || saveFailure?.message}
          </div>
        ) : null}
        <div className="teleop-control-footer">
          <div className="teleop-control-actions">
            <div className="tianji-collection-summary" role="status" aria-live="polite" aria-atomic="true">
              <span>本次完成 <strong>{collection?.completed ?? 0}</strong> Episode</span>
              <span className="compact-status">
                <StatusDot online={collecting || running} pulse={collecting} />
                {collecting
                  ? '采集中'
                  : collection?.state === 'starting'
                    ? '准备采集'
                    : collectionBusy
                      ? '正在保存'
                      : running
                        ? '等待手柄 A'
                        : pending === 'check' || state?.state === 'checking'
                          ? '正在检查设备'
                          : '未开始遥操作'}
              </span>
            </div>
            <ActionButton
              kind="secondary"
              busy={pending === 'check'}
              spinIcon
              disabled={locked || saving}
              onClick={() => act('check')}
            >
              <RefreshCw size={15} />
              检查设备
            </ActionButton>
            {running ||
            moving ||
            pending === 'start' ||
            pending === 'move' ||
            state?.state === 'unknown' ||
            state?.state === 'error' ? (
              <ActionButton
                danger
                busy={pending === 'stop'}
                onClick={() => act('stop')}
              >
                <Square size={15} fill="currentColor" />
                停止控制
              </ActionButton>
            ) : (
              <ActionButton
                busy={pending === 'start'}
                disabled={count !== 3 || locked || saving || !valid || dirty}
                onClick={() => act('start', { task, mode: 'dual', upload_mode: mode, parameters: saved })}
              >
                <Play size={15} fill="currentColor" />
                开始遥操作
              </ActionButton>
            )}
          </div>
        </div>
        {collection?.error ? (
          <div className="inline-alert teleop-panel-alert" role="alert">
            {collection.error}
          </div>
        ) : null}
        {collection?.upload.error ? (
          <div className="tianji-hint" role="status">
            上传待重试：{collection.upload.error}
          </div>
        ) : null}
      </section>
      <div className="teleop-observation-grid">
        <section className="surface teleop-visualizer">
          <div className="preview-head">
            <div>
              <h3>机器人姿态</h3>
              {live ? <span>实时关节状态</span> : null}
            </div>
            <div className="compact-status">
              <StatusDot online={live} pulse={live} />
              {live ? '遥测在线' : '无遥测'}
            </div>
          </div>
          <div className="teleop-visual-stage">
            <TianjiRobotPreview
              joints={state?.joints ?? {}}
              closure={closure}
              darkMode={darkMode}
            />
          </div>
        </section>
        <section className="surface teleop-camera">
          <div className="preview-head">
            <div>
              <h3>相机画面</h3>
            </div>
            <select
              className="camera-switcher"
              aria-label="切换天机相机"
              value={camera}
              onChange={(e) => setCamera(e.target.value)}
            >
              {TIANJI_CAMERAS.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>
          <div className="teleop-camera-stage tianji-camera-stage">
            {frame ? (
              <CameraStream
                key={camera}
                scope="tianji"
                src={frame}
                waiting={!selectedCamera?.available || state?.camera_phase === 'resuming' || state?.camera_phase === 'cooldown'}
                alt={`${TIANJI_CAMERAS.find((c) => c.id === camera)?.label}实时画面`}
              />
            ) : (
              <div className="tianji-camera-empty">
                <VideoOff size={28} />
                <strong>等待相机画面</strong>
                <span>
                  {state?.camera_error || '连接机械臂网线后，点击检查设备'}
                </span>
              </div>
            )}
          </div>
        </section>
      </div>
      <section className="surface teleop-manual">
        <div className="preview-head">
          <div>
            <h3>手动控制</h3>
          </div>
          <span className="badge badge-amber">真实运动</span>
        </div>
        <div className="teleop-manual-body">
          <div className="teleop-preset-toolbar">
            <div>
              <strong>姿态配置</strong>
            </div>
            <div className="teleop-preset-controls">
              <select
                aria-label="天机姿态配置"
                value={selectedPose}
                disabled={locked}
                onChange={(e) => setSelectedPose(e.target.value)}
              >
                <option value="zero">零点（默认）</option>
                {poses.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              <ActionButton disabled={!canMove} onClick={resetPose}>
                复位
              </ActionButton>
              <ActionButton
                kind="secondary"
                disabled={!live || locked}
                onClick={savePose}
              >
                保存当前姿态
              </ActionButton>
              <ActionButton
                kind="ghost"
                danger
                disabled={selectedPose === 'zero' || locked}
                onClick={() => {
                  if (
                    window.confirm('删除当前姿态配置？') &&
                    persistPoses(poses.filter((p) => p.id !== selectedPose))
                  )
                    setSelectedPose('zero');
                }}
              >
                删除
              </ActionButton>
            </div>
          </div>
          <div className="teleop-joint-columns tianji-joint-columns">
            {TIANJI_SIDES.map((side) => {
              return (
                <section
                  className="teleop-joint-column"
                  key={side.id}
                >
                  <header>
                    <h4>{side.label}</h4>
                  </header>
                  <div>
                    {TIANJI_LIMITS.map(([min, max], i) => {
                      const value = state?.joints[side.id]?.[i];
                      return (
                        <div className="teleop-joint" key={i}>
                          <span>
                            <strong>
                              {side.label} J{i + 1}
                            </strong>
                            <small>
                              {min}° ～ {max}°
                            </small>
                          </span>
                          <div className="teleop-joint-stepper">
                            <output>
                              {live && value !== undefined
                                ? value.toFixed(2)
                                : '—'}
                              <small> °</small>
                            </output>
                            <div>
                              <button
                                type="button"
                                aria-label={`提高${side.label} J${i + 1}`}
                                disabled={
                                  !canMove ||
                                  value === undefined ||
                                  value + TIANJI_JOG_STEP_DEG > max
                                }
                                onClick={() =>
                                  act('move', {
                                    kind: 'jog',
                                    mode: 'dual',
                                    side: side.id,
                                    joint: i + 1,
                                    delta: TIANJI_JOG_STEP_DEG,
                                  })
                                }
                              >
                                ▲
                              </button>
                              <button
                                type="button"
                                aria-label={`降低${side.label} J${i + 1}`}
                                disabled={
                                  !canMove ||
                                  value === undefined ||
                                  value - TIANJI_JOG_STEP_DEG < min
                                }
                                onClick={() =>
                                  act('move', {
                                    kind: 'jog',
                                    mode: 'dual',
                                    side: side.id,
                                    joint: i + 1,
                                    delta: -TIANJI_JOG_STEP_DEG,
                                  })
                                }
                              >
                                ▼
                              </button>
                            </div>
                          </div>
                        </div>
                      );
                    })}
                    <div className="teleop-joint">
                      <span>
                        <strong>
                          {side.label === '左臂' ? '左' : '右'}夹爪
                        </strong>
                        <small>
                          {closure[side.id] === undefined
                            ? '暂无夹爪反馈'
                            : `闭合 ${Math.round(closure[side.id]! * 100)}%`}
                        </small>
                      </span>
                      <div className="tianji-gripper-actions">
                        <button
                          type="button"
                          disabled={!canMove}
                          onClick={() =>
                            act('move', {
                              kind: 'gripper',
                              mode: 'dual',
                              side: side.id,
                              closure: 0,
                            })
                          }
                        >
                          打开
                        </button>
                        <button
                          type="button"
                          disabled={!canMove}
                          onClick={() =>
                            act('move', {
                              kind: 'gripper',
                              mode: 'dual',
                              side: side.id,
                              closure: 1,
                            })
                          }
                        >
                          闭合
                        </button>
                      </div>
                    </div>
                  </div>
                </section>
              );
            })}
          </div>
          <p className="tianji-hint">
            握把接管对应机械臂，扳机控制夹爪。A / X 开始采集，B / Y
            结束并保存；停止运动请使用“停止控制”或硬件急停。
          </p>
          {state?.reason ? (
            <p className="tianji-hint" role="status">
              {state.reason}
            </p>
          ) : null}
        </div>
      </section>
    </div>
  );
}
