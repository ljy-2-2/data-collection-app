'use client';

import {
  Activity,
  ArrowRight,
  Camera,
  Check,
  CircleHelp,
  Database,
  ExternalLink,
  Gamepad2,
  Glasses,
  LoaderCircle,
  LogOut,
  Radio,
  RefreshCw,
  Settings2,
  Square,
  Wifi,
  WifiOff,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { CameraStream as UmiCameraStream, releaseUmiCameraStreams } from '../components/CameraStream';
import { CameraBrightness } from '../components/CameraBrightness';
import { CameraResolution } from '../components/CameraResolution';
import { LoginScreen } from '../components/LoginScreen';
import { requestJson } from '../lib/http';
import { hasPendingLogout, logoutPendingKey, type PlatformUser } from '../lib/platformAuth';
import { ActionButton, SectionTitle, StatusDot, classNames } from '../components/CollectionUi';
import { TeleopPanel } from '../components/teleop/TeleopPanel';
import type { AsyncAction, TeleopStatus, PreviewStatus } from '../lib/teleopTypes';

type Section = 'teleop' | 'umi' | 'ego';
type ThemeMode = 'system' | 'light' | 'dark';
interface RuntimeConfig {
  appName: string;
  localControlUrl: string;
  platformApiUrl: string;
  platformWebUrl: string;
}

type UmiArmSide = 'left' | 'right';
type UmiCameraRole = 'left' | 'center' | 'right';
type UmiEncoderPoint = { t: number; value: number };
type UmiProfile = 'dual' | 'single-left' | 'single-right';

interface UmiDeviceReading {
  online?: boolean;
  tracked?: boolean;
  valid?: boolean;
  active?: boolean;
  rate_hz?: number;
  device_id?: string;
  position?: number | number[];
  rotation?: number[];
  seq?: number;
  name?: string;
}

interface UmiEncoderPreview {
  checked_at: number;
  preview_suspended?: boolean;
  message?: string;
  encoders: Partial<Record<UmiArmSide, {
    device: string;
    online: boolean;
    value: number | null;
    updated_at: number | null;
    error?: string;
  }>>;
}

interface UmiStatus {
  state: 'idle' | 'starting' | 'ready' | 'recording' | 'stopping' | 'error';
  message: string;
  session_id: string | null;
  task: string | null;
  collection_profile: 'dual' | 'single-left' | 'single-right';
  started_at: string | null;
  monitor_connected: boolean;
  recording: boolean;
  episodes_completed: number;
  pending_imports: number;
  pending_processing?: number;
  failed_processing?: number;
  imported_episodes: number;
  monitor: {
    ts?: number;
    cameras?: Array<{ key: string; label?: string }>;
    vr?: Partial<Record<'left' | 'hmd' | 'right', UmiDeviceReading>>;
    encoders?: Partial<Record<UmiArmSide, UmiDeviceReading>>;
    storage_gb?: { used?: number; free?: number; total?: number };
    device_check?: {
      ready?: boolean;
      hardware_ready?: boolean;
      stack_occupied?: boolean;
      roles_configured?: boolean;
      cameras?: number;
      expected_cameras?: number;
      encoders?: number;
      expected_encoders?: number;
      vr_service_ready?: boolean;
      vr_service_detail?: string;
      pico_ready?: boolean;
      pico_detail?: string;
      camera_candidates?: string[];
      camera_candidate_details?: {device: string; vendor: string; product: string; serial: string; usb_path: string; generation: string}[];
      camera_roles?: Partial<Record<UmiCameraRole, string>>;
      suggested_camera_roles?: Partial<Record<UmiCameraRole, string>>;
      encoder_candidates?: string[];
    };
    episode?: { elapsed_sec?: number; subtask_elapsed_sec?: number; completed_total?: number; label?: string };
  };
  log_tail: string[];
}

interface PlatformStatus {
  healthy: boolean;
  api_url: string;
  upload_mode: string;
  authenticated?: boolean;
  user?: PlatformUser | null;
  unavailable?: boolean;
  detail?: string;
}

declare global {
  interface Window {
    __COLLECTION_APP_CONFIG__?: Partial<RuntimeConfig>;
  }
}

const FALLBACK_CONFIG: RuntimeConfig = {
  appName: 'Robot Collection Console',
  localControlUrl: 'http://127.0.0.1:8010',
  platformApiUrl: 'http://127.0.0.1:8000',
  platformWebUrl: 'http://127.0.0.1:5173',
};

const NAV_ITEMS = [
  { id: 'teleop' as const, label: '遥操作采集', short: 'TELEOP', icon: Gamepad2 },
  { id: 'umi' as const, label: 'UMI 采集', short: 'UMI', icon: Camera },
  { id: 'ego' as const, label: 'Ego 采集', short: 'EGO', icon: Glasses },
];

function config(): RuntimeConfig {
  if (typeof window === 'undefined') return FALLBACK_CONFIG;
  return { ...FALLBACK_CONFIG, ...window.__COLLECTION_APP_CONFIG__ };
}

function secondsText(value?: number) {
  const seconds = Math.max(0, Math.floor(value ?? 0));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function UsageGuide({ image, alt }: { image: string; alt: string }) {
  return (
    <div className="usage-guide" tabIndex={0}>
      <span className="usage-guide-label"><CircleHelp size={19} aria-hidden="true" />使用说明</span>
      <figure className="usage-guide-popover"><img src={image} alt={alt} /></figure>
    </div>
  );
}

const UMI_CAMERA_ROLES: UmiCameraRole[] = ['left', 'center', 'right'];
const UMI_ARM_SIDES: UmiArmSide[] = ['left', 'right'];
const UMI_RADIANS_TO_DEGREES = 180 / Math.PI;
const UMI_PROFILE_ARMS: Record<UmiProfile, UmiArmSide[]> = {
  dual: ['left', 'right'],
  'single-left': ['left'],
  'single-right': ['right'],
};

function umiCameraLabel(role: UmiCameraRole) {
  return role === 'left' ? '左侧相机' : role === 'right' ? '右侧相机' : '中心相机';
}

function initialUmiCameraRoles(
  check: NonNullable<UmiStatus['monitor']['device_check']>,
  profile: UmiProfile,
  current: Partial<Record<UmiCameraRole, string>>,
) {
  const candidates = check.camera_candidates ?? [];
  const activeRoles: UmiCameraRole[] = profile === 'dual'
    ? ['left', 'center', 'right']
    : profile === 'single-left' ? ['left', 'center'] : ['center', 'right'];
  const center = candidates.find((path) => path === '/dev/video0') ?? candidates[0];
  const hands = candidates.filter((path) => path !== center);
  const fallback: Partial<Record<UmiCameraRole, string>> = profile === 'dual'
    ? { left: hands[0], center, right: hands[1] }
    : profile === 'single-left' ? { left: hands[0], center } : { center, right: hands[0] };
  const resolved: Partial<Record<UmiCameraRole, string>> = {};
  const used = new Set<string>();
  for (const source of [current, check.camera_roles, check.suggested_camera_roles, fallback]) {
    activeRoles.forEach((role) => {
      const device = source?.[role];
      if (!resolved[role] && device && candidates.includes(device) && !used.has(device)) {
        resolved[role] = device;
        used.add(device);
      }
    });
  }
  activeRoles.forEach((role) => {
    if (resolved[role]) return;
    const device = candidates.find((candidate) => !used.has(candidate));
    if (device) {
      resolved[role] = device;
      used.add(device);
    }
  });
  return resolved;
}


function umiDegreesText(degrees: number) {
  const rounded = degrees.toFixed(2);
  return `${rounded === '-0.00' ? '0.00' : rounded}°`;
}

function UmiSensorCurve({
  side,
  reading,
  points,
  enabled = true,
  minimum,
  onMinimumChange,
}: {
  side: UmiArmSide;
  reading?: { device?: string; online?: boolean; value?: number | null; error?: string };
  points: UmiEncoderPoint[];
  enabled?: boolean;
  minimum?: number;
  onMinimumChange?: (checked: boolean) => void;
}) {
  // Preview/calibration and ROS telemetry stay in radians; only presentation
  // uses degrees, so confirming a minimum never changes the submitted zero.
  const degreePoints = points.map((point) => ({ ...point, value: point.value * UMI_RADIANS_TO_DEGREES }));
  const values = degreePoints.map((point) => point.value);
  const rawLow = values.length ? Math.min(...values) : 0;
  const rawHigh = values.length ? Math.max(...values) : 1;
  // Keep small movements visible with the same physical minimum span.
  const padding = Math.max((rawHigh - rawLow) * 0.15, 0.0005 * UMI_RADIANS_TO_DEGREES);
  const low = onMinimumChange ? rawLow - padding : Math.max(0, rawLow - padding);
  const high = rawHigh + padding;
  const span = high - low;
  const start = points[0]?.t ?? 0;
  const end = points.at(-1)?.t ?? start + 6;
  const duration = Math.max(end - start, 0.001);
  const polyline = degreePoints.map((point) => {
    const x = 56 + ((point.t - start) / duration) * 544;
    const y = 116 - ((point.value - low) / span) * 96;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const label = side === 'left' ? '左侧传感器' : '右侧传感器';
  const current = typeof reading?.value === 'number' && Number.isFinite(reading.value)
    ? reading.value * UMI_RADIANS_TO_DEGREES : null;

  return (
    <article className={classNames('umi-sensor-curve-card', !enabled && 'is-placeholder')}>
      <header>
        <div><strong>{label}</strong><small>{enabled ? reading?.device || '等待传感器' : '未启用'}</small></div>
        <span className={reading?.online ? 'is-online' : ''}>{reading?.online ? '实时' : enabled ? '离线' : '--'}</span>
      </header>
      <div className="umi-sensor-chart">
        <svg viewBox="0 0 600 136" preserveAspectRatio="none" aria-hidden="true">
          <path className="grid-line" d="M56 20V116 M56 20H600 M56 68H600 M56 116H600" />
          <text x="51" y="24" textAnchor="end">{umiDegreesText(high)}</text>
          <text x="51" y="72" textAnchor="end">{umiDegreesText((high + low) / 2)}</text>
          <text x="51" y="120" textAnchor="end">{umiDegreesText(low)}</text>
          {polyline ? <polyline points={polyline} /> : null}
        </svg>
        {!polyline ? <span>{enabled ? reading?.error || '等待实时数据…' : '未启用'}</span> : null}
      </div>
      <footer><span>{onMinimumChange ? '原始角度' : '相对角度'}</span><strong>{current === null ? '--' : umiDegreesText(current)}</strong>{onMinimumChange && enabled ? <label className="umi-minimum-confirm"><input type="checkbox" checked={minimum !== undefined} disabled={!reading?.online || current === null} onChange={(event) => onMinimumChange(event.target.checked)} /><span>闭合最小值（原始零点）</span><strong>{minimum === undefined ? '--' : umiDegreesText(minimum * UMI_RADIANS_TO_DEGREES)}</strong></label> : null}</footer>
    </article>
  );
}

function umiHz(reading?: UmiDeviceReading) {
  return reading?.online && reading.rate_hz ? `${reading.rate_hz.toFixed(1)} Hz` : '-- Hz';
}

function umiVrLive(reading?: UmiDeviceReading) {
  return Boolean(reading?.online && (reading.rate_hz ?? 0) > 0);
}

function umiVector(value?: number | number[], suffix = '') {
  if (typeof value === 'number') return `${value.toFixed(4)}${suffix}`;
  return value?.length ? `[${value.map((item) => Number(item).toFixed(4)).join(', ')}]${suffix}` : '--';
}

function UmiPanel({
  baseUrl,
  status,
  action,
  error,
  onAction,
  visible,
}: {
  baseUrl: string;
  status: UmiStatus | null;
  action: AsyncAction;
  error: string | null;
  onAction: (action: Exclude<AsyncAction, null>, payload?: Record<string, unknown>) => Promise<boolean>;
  visible: boolean;
}) {
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [task, setTask] = useState('Umi数据采集');
  const [profile, setProfile] = useState<UmiProfile>('dual');
  const [cameraRoles, setCameraRoles] = useState<Partial<Record<UmiCameraRole, string>>>({});
  const cameraIdentities = useRef<Partial<Record<UmiCameraRole, NonNullable<NonNullable<UmiStatus['monitor']['device_check']>['camera_candidate_details']>[number]>>>({});
  const [cameraLive, setCameraLive] = useState<Partial<Record<UmiCameraRole, boolean>>>({});
  const [resolutionBusy, setResolutionBusy] = useState<Partial<Record<UmiCameraRole, boolean>>>({});
  const [cameraPreviewGeneration, setCameraPreviewGeneration] = useState(0);
  const [encoderPreview, setEncoderPreview] = useState<UmiEncoderPreview>({ checked_at: 0, encoders: {} });
  const [encoderHistory, setEncoderHistory] = useState<Record<UmiArmSide, UmiEncoderPoint[]>>({ left: [], right: [] });
  const [encoderMinima, setEncoderMinima] = useState<Partial<Record<UmiArmSide, number>>>({});
  const [monitorEncoderHistory, setMonitorEncoderHistory] = useState<Record<UmiArmSide, UmiEncoderPoint[]>>({ left: [], right: [] });
  const [preflightRequested, setPreflightRequested] = useState(false);
  const [telemetry, setTelemetry] = useState<{
    monitor_connected: boolean;
    ts?: number;
    vr?: UmiStatus['monitor']['vr'];
    encoders?: UmiStatus['monitor']['encoders'];
  }>({ monitor_connected: false });
  useEffect(() => {
    if (!visible || step !== 3 || action === 'stop-umi') return;
    let lastSample = performance.now();
    const stream = new EventSource(`${baseUrl}/umi/telemetry`);
    const offline = () => setTelemetry({ monitor_connected: false });
    stream.onmessage = (event) => {
      try {
        const value = JSON.parse(event.data);
        if (!value || typeof value !== 'object' || !value.vr || !value.encoders) return;
        lastSample = performance.now();
        setTelemetry(value);
      } catch { /* An incomplete frame must not interrupt the monitor. */ }
    };
    stream.onerror = offline;
    const watchdog = window.setInterval(() => {
      if (performance.now() - lastSample > 1500) offline();
    }, 500);
    return () => {
      window.clearInterval(watchdog);
      stream.close();
      offline();
    };
  }, [baseUrl, visible, step, action === 'stop-umi']);
  const spaceActionPending = useRef(false);
  const sessionActive = Boolean(status?.session_id);
  const recordReady = status?.state === 'ready' && Boolean(status.monitor_connected);
  const cameras = status?.monitor.cameras ?? [];
  // A prior check can remain cached in the local service after logout. Only
  // show results requested by this newly mounted operator session.
  const deviceCheck = preflightRequested ? status?.monitor.device_check : undefined;
  const cameraCandidates = deviceCheck?.camera_candidates ?? [];
  const requiredRoles = useMemo<UmiCameraRole[]>(() => profile === 'dual'
    ? ['left', 'right', 'center']
    : profile === 'single-left' ? ['left', 'center'] : ['right', 'center'], [profile]);
  const activeArms = UMI_PROFILE_ARMS[profile];
  const cameraCandidatesKey = cameraCandidates.join('|');
  const assignedRoles = deviceCheck?.camera_roles ?? {};
  const rolesChanged = requiredRoles.some((role) => assignedRoles[role] !== cameraRoles[role]);
  const needsConfigure = !deviceCheck?.roles_configured || rolesChanged;
  const canEnterMonitor = Boolean(
    !sessionActive && !action && task.trim() &&
    status?.collection_profile === profile &&
    deviceCheck?.hardware_ready && !deviceCheck?.stack_occupied &&
    deviceCheck?.pico_ready &&
    deviceCheck?.expected_cameras === requiredRoles.length &&
    deviceCheck?.expected_encoders === activeArms.length &&
    requiredRoles.every((role) => cameraRoles[role]) &&
    new Set(requiredRoles.map((role) => cameraRoles[role])).size === requiredRoles.length &&
    requiredRoles.every((role) => cameraLive[role]) &&
    !requiredRoles.some((role) => resolutionBusy[role]) &&
    activeArms.every((side) => encoderPreview.encoders[side]?.online) &&
    activeArms.every((side) => encoderMinima[side] !== undefined) &&
    (needsConfigure || deviceCheck?.ready),
  );

  const checkHardware = useCallback(() => {
    setPreflightRequested(true);
    setCameraLive({});
    setEncoderMinima({});
    setCameraPreviewGeneration((value) => value + 1);
    void onAction('check-umi', { profile });
  }, [onAction, profile]);

  useEffect(() => {
    if (!visible || step !== 2 || preflightRequested || sessionActive || action) return;
    checkHardware();
  }, [action, checkHardware, preflightRequested, sessionActive, step, visible]);

  useEffect(() => {
    if (!deviceCheck?.camera_candidates) return;
    const details = deviceCheck.camera_candidate_details;
    setCameraRoles((current) => {
      const next = initialUmiCameraRoles(deviceCheck, profile, current);
      if (details) {
        for (const role of requiredRoles) {
          const saved = cameraIdentities.current[role];
          // Track the operator's explicit assignment before reconciling hotplug.
          const chosen = details.find(d => d.device === current[role]);
          if (chosen && (!saved || (saved.device !== chosen.device && saved.generation !== chosen.generation))) {
            cameraIdentities.current[role] = chosen;
          }
          const identity = cameraIdentities.current[role];
          if (identity) {
            const matching = details.filter(d => d.vendor === identity.vendor && d.product === identity.product && d.serial === identity.serial);
            const match = identity.serial && matching.length === 1 ? matching[0] : matching.find(d => d.usb_path === identity.usb_path);
            next[role] = match?.device || '';
            if (match) cameraIdentities.current[role] = match;
          } else {
            const initial = details.find(d => d.device === next[role]);
            if (initial) cameraIdentities.current[role] = initial;
          }
        }
      }
      return JSON.stringify(next) === JSON.stringify(current) ? current : next;
    });
  }, [deviceCheck, profile, requiredRoles, cameraRoles]);

  useEffect(() => {
    setEncoderHistory({ left: [], right: [] });
    setMonitorEncoderHistory({ left: [], right: [] });
    setEncoderPreview({ checked_at: 0, encoders: {} });
    setEncoderMinima({});
    setPreflightRequested(false);
    cameraIdentities.current = {};
  }, [profile]);

  useEffect(() => {
    if (!visible || step !== 3 || !telemetry.monitor_connected) return;
    const sampledAt = telemetry.ts;
    if (typeof sampledAt !== 'number' || !Number.isFinite(sampledAt)) return;
    setMonitorEncoderHistory((current) => {
      const updated = { left: [...current.left], right: [...current.right] };
      activeArms.forEach((side) => {
        const reading = telemetry.encoders?.[side];
        if (!reading?.online || typeof reading.position !== 'number' || !Number.isFinite(reading.position)) return;
        if (updated[side].at(-1)?.t !== sampledAt) updated[side].push({ t: sampledAt, value: reading.position });
        updated[side] = updated[side].filter((point) => sampledAt - point.t <= 8);
      });
      return updated;
    });
  }, [activeArms, telemetry.encoders, telemetry.ts, telemetry.monitor_connected, step, visible]);

  useEffect(() => {
    if (!visible || step !== 2 || sessionActive || !deviceCheck || action) return;
    let disposed = false;
    let timer = 0;
    const poll = async () => {
      try {
        const next = await requestJson<UmiEncoderPreview>(baseUrl, `/umi/encoders/preview?collection_profile=${encodeURIComponent(profile)}`);
        if (disposed) return;
        setEncoderPreview(next);
        setEncoderHistory((current) => {
          const updated = { left: [...current.left], right: [...current.right] };
          activeArms.forEach((side) => {
            const reading = next.encoders[side];
            if (reading?.online && typeof reading.value === 'number' && reading.updated_at) {
              if (updated[side].at(-1)?.t !== reading.updated_at) updated[side].push({ t: reading.updated_at, value: reading.value });
              updated[side] = updated[side].filter((point) => next.checked_at - point.t <= 6);
            }
          });
          return updated;
        });
      } catch {
        // Keep the latest sensor curve visible while the serial device reconnects.
      } finally {
        if (!disposed) timer = window.setTimeout(poll, 300);
      }
    };
    void poll();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [action, activeArms, baseUrl, deviceCheck, profile, sessionActive, step, visible]);

  useEffect(() => {
    if (!visible || step !== 2 || sessionActive || !deviceCheck || action) return;
    let disposed = false;
    let timer = 0;
    const poll = async () => {
      const states = await Promise.all(requiredRoles.map(async (role): Promise<[UmiCameraRole, boolean]> => {
        const cameraKey = cameraRoles[role]?.replace(/^\/dev\//, '');
        if (!cameraKey) return [role, false];
        try {
          const result = await requestJson<{ ready: boolean }>(baseUrl, `/umi/device-cameras/${encodeURIComponent(cameraKey)}/health`);
          return [role, result.ready];
        } catch {
          return [role, false];
        }
      }));
      if (disposed) return;
      const next = Object.fromEntries(states) as Partial<Record<UmiCameraRole, boolean>>;
      setCameraLive((current) => UMI_CAMERA_ROLES.every((role) => Boolean(current[role]) === Boolean(next[role])) ? current : next);
      timer = window.setTimeout(poll, 1000);
    };
    void poll();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [action, baseUrl, cameraCandidatesKey, cameraRoles, deviceCheck, requiredRoles, sessionActive, step, visible]);

  const selectCameraRole = (role: UmiCameraRole, device: string) => {
    const occupiedRole = requiredRoles.find((candidate) => candidate !== role && cameraRoles[candidate] === device);
    setCameraLive((current) => ({ ...current, [role]: false, ...(occupiedRole ? { [occupiedRole]: false } : {}) }));
    setCameraRoles((current) => {
      const next = { ...current };
      const previous = next[role];
      next[role] = device || undefined;
      if (occupiedRole) next[occupiedRole] = previous;
      return next;
    });
  };

  const setEncoderMinimum = (side: UmiArmSide, checked: boolean) => {
    setEncoderMinima((current) => {
      const next = { ...current };
      const value = encoderPreview.encoders[side]?.value;
      if (checked && encoderPreview.encoders[side]?.online && typeof value === 'number' && Number.isFinite(value)) next[side] = value;
      else delete next[side];
      return next;
    });
  };

  const enterMonitor = async () => {
    if (!canEnterMonitor) return;
    if (await onAction('start-umi', { task: task.trim(), profile, cameraRoles, encoderMinima })) {
      setMonitorEncoderHistory({ left: [], right: [] });
      setStep(3);
    }
  };

  const endAndReturn = async () => {
    if (sessionActive && !await onAction('stop-umi')) return;
    setCameraLive({});
    setCameraPreviewGeneration((value) => value + 1);
    setPreflightRequested(false);
    setStep(2);
  };

  useEffect(() => {
    if (!visible || step !== 3) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== 'Space' || event.repeat || action || spaceActionPending.current || !sessionActive) return;
      const target = event.target;
      if (target instanceof Element && target.closest('input, select, textarea, [contenteditable="true"]')) return;
      if (status?.recording) {
        event.preventDefault();
        spaceActionPending.current = true;
        void onAction('record-stop').finally(() => { spaceActionPending.current = false; });
      } else if (recordReady) {
        event.preventDefault();
        spaceActionPending.current = true;
        void onAction('record-start').finally(() => { spaceActionPending.current = false; });
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [action, onAction, recordReady, sessionActive, status?.recording, step, visible]);

  return (
    <div className="umi-workspace">
      <section className="surface umi-stepper" aria-label="UMI 采集步骤">
        {[
          { id: 1, title: '选择配置' },
          { id: 2, title: '检查硬件' },
          { id: 3, title: '采集监控' },
        ].map((item, index) => (
          <div key={item.id} className={classNames('umi-step', step === item.id && 'active', step > item.id && 'done')}>
            <span>{step > item.id ? <Check size={15} /> : item.id}</span>
            <div><strong>{item.title}</strong></div>
            {index < 2 ? <i /> : null}
          </div>
        ))}
      </section>

      {step === 1 ? (
        <section className="surface umi-stage-card">
          <SectionTitle eyebrow="STEP 01" title="选择采集配置" />
          <div className="umi-stage-body">
            <div className="field-grid">
              <label className="field">
                <span>任务名称</span>
                <input className="collection-task-name" type="text" value={task} onChange={(event) => setTask(event.target.value)} maxLength={120} />
              </label>
              <label className="field">
                <span>采集配置</span>
                <select value={profile} onChange={(event) => { setProfile(event.target.value as typeof profile); setCameraRoles({}); setCameraLive({}); }}>
                  <option value="dual">双臂</option>
                  <option value="single-left">仅左臂</option>
                  <option value="single-right">仅右臂</option>
                </select>
              </label>
            </div>
            <div className="profile-summary">
              <div><Camera size={20} /><span>相机</span><strong>{profile === 'dual' ? '3 路' : '2 路'}</strong></div>
              <div><Settings2 size={20} /><span>编码器</span><strong>{profile === 'dual' ? '2 个' : '1 个'}</strong></div>
              <div><Glasses size={20} /><span>头显</span><strong>PICO</strong></div>
            </div>
            <div className="stage-actions"><ActionButton onClick={() => { setPreflightRequested(false); setStep(2); }} disabled={!task.trim()}>下一步：检查硬件 <ArrowRight size={16} /></ActionButton></div>
          </div>
        </section>
      ) : null}

      {step === 2 ? (
        <section className="surface umi-stage-card">
          <SectionTitle eyebrow="STEP 02" title="检查并选择硬件" />
          <div className="umi-stage-body">
            <div className="umi-device-summary">
              <div><Camera size={19} /><span>相机</span><strong>{Math.min(cameraCandidates.length, requiredRoles.length)}/{requiredRoles.length}</strong></div>
              <div><Settings2 size={19} /><span>编码器</span><strong>{Math.min(deviceCheck?.encoder_candidates?.length ?? 0, activeArms.length)}/{activeArms.length}</strong></div>
              <div><Wifi size={19} /><span>Galaxis VR</span><strong>{action === 'check-umi' ? '检测中' : deviceCheck?.vr_service_ready ? '已启动' : '未启动'}</strong></div>
              <div><Glasses size={19} /><span>PICO</span><strong>{deviceCheck?.pico_ready ? '已连接' : '未连接'}</strong></div>
            </div>
            <div className="hardware-check-row">
              <div><strong>硬件检测</strong></div>
              <ActionButton kind="secondary" onClick={checkHardware} busy={action === 'check-umi'} spinIcon disabled={sessionActive || Boolean(action)}>
                <RefreshCw size={17} /> {deviceCheck ? '重新检测' : '开始检测'}
              </ActionButton>
            </div>
            {deviceCheck ? (
              <div className="umi-preflight-live">
                <div className="umi-preflight-camera-grid" aria-label="相机实时预览">
                  {UMI_CAMERA_ROLES.map((role) => {
                    const enabled = requiredRoles.includes(role);
                    const device = cameraRoles[role];
                    const cameraKey = device?.replace(/^\/dev\//, '');
                    return (
                      <article className={classNames('umi-preflight-camera', !enabled && 'is-placeholder')} key={role}>
                        <header>
                          <strong>{umiCameraLabel(role)}</strong>
                          <select aria-label={`选择${umiCameraLabel(role)}`} value={enabled ? device ?? '' : ''} disabled={!enabled} onChange={(event) => selectCameraRole(role, event.target.value)}>
                            <option value="">{enabled ? '请选择设备' : '未启用'}</option>
                            {cameraCandidates.map((candidate) => <option key={candidate} value={candidate}>{candidate}</option>)}
                          </select>
                        </header>
                        <div className="umi-preflight-camera-stage">
                          {visible && enabled && cameraKey && !action ? <UmiCameraStream key={`${cameraKey}-${cameraPreviewGeneration}`} src={`${baseUrl}/umi/device-cameras/${encodeURIComponent(cameraKey)}`} alt={`${umiCameraLabel(role)}实时画面`} /> : <span>{action === 'check-umi' ? '检测中' : action === 'start-umi' ? '启动中' : enabled ? '等待相机接入' : '未启用'}</span>}
                        </div>
                        {enabled ? <CameraResolution baseUrl={baseUrl} cameraKey={cameraKey ?? ''} label={umiCameraLabel(role)} active={Boolean(visible && cameraKey && !action)} onBusyChange={busy => setResolutionBusy(current => current[role] === busy ? current : {...current,[role]:busy})} /> : null}
                        {enabled ? <CameraBrightness baseUrl={baseUrl} cameraKey={cameraKey ?? ''} label={umiCameraLabel(role)} active={Boolean(visible && cameraKey && !action && cameraLive[role])} /> : null}
                      </article>
                    );
                  })}
                </div>
                <div className="umi-sensor-curve-grid" aria-label="传感器实时曲线">
                  {UMI_ARM_SIDES.map((side) => <UmiSensorCurve key={side} side={side} enabled={activeArms.includes(side)} reading={encoderPreview.encoders[side]} points={encoderHistory[side]} minimum={encoderMinima[side]} onMinimumChange={(checked) => setEncoderMinimum(side, checked)} />)}
                </div>
              </div>
            ) : null}
            {deviceCheck?.stack_occupied ? <div className="inline-alert"><WifiOff size={16} /><span>其他 Galaxis 服务正在占用相机和编码器。请先结束该服务，再重新检测。</span></div> : null}
            {error ? <div className="inline-alert"><WifiOff size={16} /><span>{error}</span></div> : null}
            {deviceCheck?.pico_ready === false ? <div className="inline-alert"><Glasses size={16} /><span>{deviceCheck.pico_detail}</span></div> : null}
            <div className="stage-actions split"><ActionButton kind="ghost" onClick={() => setStep(1)}>返回配置</ActionButton><ActionButton onClick={enterMonitor} busy={action === 'start-umi'} disabled={!canEnterMonitor}>{canEnterMonitor ? '进入监控' : '确认并勾选'} <ArrowRight size={16} /></ActionButton></div>
          </div>
        </section>
      ) : null}

      {step === 3 ? (
        <section className="surface umi-live-console">
          <div className="umi-live-head">
            <div><span className="eyebrow">STEP 03</span><h3>采集监控</h3><p>{task} · {profile === 'dual' ? '双臂' : profile === 'single-left' ? '仅左臂' : '仅右臂'}</p></div>
            <div className="compact-status"><StatusDot online={Boolean(sessionActive && status?.monitor_connected)} pulse={Boolean(status?.recording)} />{status?.recording ? '录制中' : recordReady ? '已就绪' : status?.state === 'error' ? '启动失败' : sessionActive ? '启动中' : '未启动'}</div>
          </div>

          <div className="umi-live-camera-grid" aria-label="UMI 相机监控区">
            {UMI_CAMERA_ROLES.map((role) => {
              const enabled = requiredRoles.includes(role);
              const camera = cameras.find((item) => item.key === role);
              const live = Boolean(visible && enabled && camera && status?.monitor_connected && action !== 'stop-umi');
              return (
                <article className={classNames('umi-live-camera-card', !enabled && 'is-placeholder')} key={role}>
                  <header><strong>{umiCameraLabel(role)}</strong><span className={live ? 'is-live' : ''}>{live ? 'LIVE' : '--'}</span></header>
                  <div>{live ? <UmiCameraStream src={`${baseUrl}/umi/cameras/${encodeURIComponent(role)}`} alt={`${umiCameraLabel(role)}实时画面`} /> : <span className="umi-camera-slot-wait">{enabled ? '等待视频流' : '未启用'}</span>}</div>
                </article>
              );
            })}
          </div>

          <div className="umi-live-device-grid">
            <section className="umi-live-device-card">
              <header><h4>VR 状态</h4></header>
              <div className="umi-live-vr-grid">
                {([['left', '左手柄'], ['hmd', '头显'], ['right', '右手柄']] as const).map(([key, label]) => {
                  const reading = telemetry.vr?.[key];
                  const live = Boolean(telemetry.monitor_connected && umiVrLive(reading));
                  return <div className={live ? '' : 'is-offline'} key={key}><div><strong>{label}</strong><span>{!live ? '无数据' : reading?.tracked === false || reading?.valid === false ? '追踪丢失' : '在线'}</span><small>{live ? `${umiHz(reading)} · ${reading?.seq ?? '--'}` : '-- Hz'}</small></div><p><i>位置</i><span>{live ? umiVector(reading?.position, ' m') : '--'}</span></p><p><i>姿态</i><span>{live ? umiVector(reading?.rotation) : '--'}</span></p></div>;
                })}
              </div>
            </section>
          </div>

          <div className="umi-live-footer-grid">
            <section className="umi-live-device-card">
              <header><h4>磁编码器</h4></header>
              <div className="umi-live-sensor-curve-grid">
                {UMI_ARM_SIDES.map((side) => {
                  const enabled = activeArms.includes(side);
                  const source = telemetry.encoders?.[side];
                  const live = Boolean(enabled && telemetry.monitor_connected && source?.online && (source.rate_hz ?? 0) > 0);
                  const reading = { device: live ? `${source?.device_id ? `ID ${source.device_id} · ` : ''}${umiHz(source)}` : source?.name || '', online: live, value: live && typeof source?.position === 'number' ? source.position : null };
                  const points = live ? monitorEncoderHistory[side] : [];
                  return <UmiSensorCurve key={side} side={side} enabled={enabled} points={points} reading={reading} />;
                })}
              </div>
            </section>
            <section className="umi-live-session-card">
              <div className="umi-live-stats">
                <header><h4>采集统计</h4><span className={status?.recording ? 'is-recording' : recordReady ? 'is-ready' : ''}>{status?.recording ? '录制中' : recordReady ? '待机' : '未就绪'}</span></header>
                <div><p><small>本次完成</small><strong>{status?.episodes_completed ?? 0}</strong><em>Episode</em></p><p><small>当前回合</small><strong>{status?.monitor.episode?.label ?? '—'}</strong></p><p><small>本回合录制时长</small><strong>{secondsText(status?.monitor.episode?.elapsed_sec)}</strong></p><p><small>待上传</small><strong>{status?.pending_imports ?? 0}</strong></p></div>
              </div>
              <div className="umi-live-controls">
                <div className="umi-live-control-note">
                  <strong>{status?.recording ? '正在录制' : recordReady ? '可以开始采集' : status?.state === 'error' ? '采集服务异常' : sessionActive ? '启动中' : '采集服务未运行'}</strong>
                  {Boolean(status?.pending_processing || status?.failed_processing) && <span>后台待处理 {status?.pending_processing ?? 0} 段 · 处理失败 {status?.failed_processing ?? 0} 段，可继续采集</span>}
                  <span>{status?.recording ? '手柄 B/Y 或空格保存本段' : recordReady ? '手柄 A/X 或空格开始' : status?.message || '完成设备检测后进入监控'}</span>
                </div>
                <div className="umi-live-control-buttons">
                  {!sessionActive ? <ActionButton kind="ghost" onClick={endAndReturn}>结束返回</ActionButton> : status?.recording ? <><ActionButton onClick={() => onAction('record-stop')} busy={action === 'record-stop'}><Square size={15} fill="currentColor" /> 完成本段</ActionButton><ActionButton kind="ghost" danger onClick={() => onAction('record-cancel')} busy={action === 'record-cancel'}><X size={16} /> 放弃本段</ActionButton></> : <><ActionButton onClick={() => onAction('record-start')} busy={action === 'record-start'} disabled={!recordReady}><Radio size={16} /> 开始采集</ActionButton><ActionButton kind="ghost" danger onClick={endAndReturn} busy={action === 'stop-umi'}><Square size={15} fill="currentColor" /> 结束返回</ActionButton></>}
                </div>
              </div>
            </section>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function EgoPanel() {
  return <section className="surface ego-blank" aria-label="Ego 采集预留空白页" />;
}

export default function Home() {
  const [runtime] = useState(config);
  const controlBase = runtime.localControlUrl.replace(/\/$/, '');
  const [active, setActive] = useState<Section>('teleop');
  const [theme, setTheme] = useState<ThemeMode>('system');
  const [systemDark, setSystemDark] = useState(false);
  const [themeReady, setThemeReady] = useState(false);
  const [authChecking, setAuthChecking] = useState(true);
  const [authUser, setAuthUser] = useState<PlatformUser | null>(null);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [logoutMessage, setLogoutMessage] = useState('');
  const [authNotice, setAuthNotice] = useState('');
  const [agentOnline, setAgentOnline] = useState(false);
  const [platformStatus, setPlatformStatus] = useState<PlatformStatus | null>(null);
  const [teleopRobotModel, setTeleopRobotModel] = useState('tianji');
  const [teleopStatus, setTeleopStatus] = useState<TeleopStatus | null>(null);
  const [preview, setPreview] = useState<PreviewStatus | null>(null);
  const [umiStatus, setUmiStatus] = useState<UmiStatus | null>(null);
  const [umiStatusError, setUmiStatusError] = useState<string | null>(null);
  const [action, setAction] = useState<AsyncAction>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const logoutRequested = useRef(false);
  const authEpoch = useRef(0);
  const umiRequestSeq = useRef(0);
  const umiPollFailures = useRef(0);
  const platformRequestSeq = useRef(0);
  const toolSnapshot = useRef({ active, agentOnline, platformStatus, teleopStatus, umiStatus });

  useEffect(() => {
    let activeRequest = true;
    requestJson<PlatformStatus>(controlBase, '/platform/status')
      .then((result) => {
        if (activeRequest) {
          setAuthUser(!hasPendingLogout(controlBase) && result.authenticated ? result.user ?? null : null);
        }
      })
      .catch(() => {
        if (activeRequest) setAuthUser(null);
      })
      .finally(() => {
        if (activeRequest) setAuthChecking(false);
      });
    return () => { activeRequest = false; };
  }, [controlBase]);

  useEffect(() => {
    toolSnapshot.current = { active, agentOnline, platformStatus, teleopStatus, umiStatus };
  }, [active, agentOnline, platformStatus, teleopStatus, umiStatus]);

  useEffect(() => {
    if (!themeReady) return;
    window.localStorage.setItem('data-platform-theme', theme);
    if (theme === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
  }, [theme, themeReady]);

  useEffect(() => {
    const stored = window.localStorage.getItem('data-platform-theme');
    if (stored === 'light' || stored === 'dark' || stored === 'system') setTheme(stored);
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    setSystemDark(media.matches);
    setThemeReady(true);
    const listener = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    media.addEventListener('change', listener);
    return () => media.removeEventListener('change', listener);
  }, []);

  useEffect(() => {
    type ToolRegistration = {
      name: string;
      title: string;
      description: string;
      inputSchema: Record<string, unknown>;
      annotations: { readOnlyHint: boolean; untrustedContentHint: boolean };
      execute: (input: unknown) => unknown;
    };
    const modelContext = (document as Document & {
      modelContext?: { registerTool: (tool: ToolRegistration, options?: { signal?: AbortSignal }) => void | Promise<void> };
    }).modelContext;
    if (!modelContext?.registerTool) return;
    const lifecycle = new AbortController();
    const register = (tool: ToolRegistration) => {
      try {
        void Promise.resolve(modelContext.registerTool(tool, { signal: lifecycle.signal })).catch(() => undefined);
      } catch {
        // WebMCP is optional and must never block the operator UI.
      }
    };
    register({
      name: 'read_collection_console_status',
      title: '读取采集台状态',
      description: '读取本机采集服务、上传平台以及当前遥操作和 UMI 会话状态。',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: false },
      execute: () => {
        const current = toolSnapshot.current;
        return {
          activeMode: current.active,
          localAgentOnline: current.agentOnline,
          platformOnline: Boolean(current.platformStatus?.healthy),
          teleopSession: current.teleopStatus?.session.state ?? 'unknown',
          umiState: current.umiStatus?.state ?? 'unknown',
          umiRecording: Boolean(current.umiStatus?.recording),
        };
      },
    });
    register({
      name: 'open_collection_mode',
      title: '打开采集模式',
      description: '在采集台中打开遥操作、UMI 或 Ego 工作区；只切换界面，不启动硬件。',
      inputSchema: {
        type: 'object',
        properties: { mode: { type: 'string', enum: ['teleop', 'umi', 'ego'] } },
        required: ['mode'],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      execute: (input) => {
        const mode = (input as { mode?: unknown })?.mode;
        if (mode !== 'teleop' && mode !== 'umi' && mode !== 'ego') throw new Error('mode must be teleop, umi, or ego');
        setActive(mode);
        setError(null);
        return { activeMode: mode };
      },
    });
    return () => lifecycle.abort();
  }, []);

  const refreshPlatform = useCallback(async () => {
    const requestId = ++platformRequestSeq.current;
    try {
      const next = await requestJson<PlatformStatus>(controlBase, '/platform/status');
      if (alive.current && !logoutRequested.current && requestId === platformRequestSeq.current) {
        setPlatformStatus(next);
        if (next.healthy && next.authenticated === false && !next.unavailable) setAuthUser(null);
        const authenticatedUser = next.user;
        if (next.authenticated && authenticatedUser) {
          setAuthUser((current) => current
            && current.id === authenticatedUser.id
            && current.username === authenticatedUser.username
            && current.is_admin === authenticatedUser.is_admin
            && current.is_active === authenticatedUser.is_active
            ? current : authenticatedUser);
        }
      }
    } catch {
      if (alive.current && requestId === platformRequestSeq.current) setPlatformStatus({ healthy: false, api_url: runtime.platformApiUrl, upload_mode: 'direct' });
    }
  }, [controlBase, runtime.platformApiUrl]);

  const refreshTeleop = useCallback(async (withPreview = true) => {
    try {
      const next = await requestJson<TeleopStatus>(controlBase, '/status');
      if (!alive.current) return;
      setAgentOnline(true);
      setTeleopStatus(next);
      if (withPreview && teleopRobotModel === 'agibot-g2') {
        requestJson<PreviewStatus>(controlBase, '/preview?camera_id=head_color')
          .then((value) => alive.current && setPreview(value))
          .catch(() => alive.current && setPreview(null));
      }
    } catch (reason) {
      if (!alive.current) return;
      setAgentOnline(false);
      setTeleopStatus(null);
      setPreview(null);
      if (action) setError(reason instanceof Error ? reason.message : '本机采集服务不可用');
    }
  }, [action, controlBase, teleopRobotModel]);

  const refreshUmi = useCallback(async () => {
    const requestId = ++umiRequestSeq.current;
    const requestEpoch = authEpoch.current;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const next = await requestJson<UmiStatus>(controlBase, '/umi/status', { signal: controller.signal });
      if (logoutRequested.current || requestEpoch !== authEpoch.current || requestId !== umiRequestSeq.current) return;
      setAgentOnline(true);
      umiPollFailures.current = 0;
      setUmiStatusError(null);
      setUmiStatus(next);
    } catch (reason) {
      if (logoutRequested.current || requestEpoch !== authEpoch.current || requestId !== umiRequestSeq.current) return;
      // Preserve the last known session so the stop control stays usable, but
      // do not let a broken status connection look like endless startup.
      umiPollFailures.current += 1;
      if (umiPollFailures.current >= 3) {
        setUmiStatus((current) => current ? { ...current, monitor_connected: false } : current);
        const detail = reason instanceof Error ? reason.message : '';
        setUmiStatusError(reason instanceof DOMException && reason.name === 'AbortError'
          ? '采集状态请求超时，正在重试'
          : detail === 'Failed to fetch' || detail === 'Load failed' || reason instanceof TypeError
            ? '本机采集服务暂时断开，正在恢复'
            : `采集状态请求失败：${detail || '未知错误'}`);
      }
    } finally {
      window.clearTimeout(timeout);
    }
  }, [controlBase]);

  useEffect(() => {
    if (!authUser) return;
    alive.current = true;
    void refreshTeleop();
    void refreshPlatform();
    const statusTimer = window.setInterval(() => {
      void refreshTeleop(active === 'teleop');
    }, 3000);
    const platformTimer = window.setInterval(() => void refreshPlatform(), 10000);
    return () => {
      alive.current = false;
      window.clearInterval(statusTimer);
      window.clearInterval(platformTimer);
    };
  }, [active, authUser, refreshPlatform, refreshTeleop]);

  useEffect(() => {
    if (!authUser) return;
    let disposed = false;
    let timer = 0;
    const poll = async () => {
      await refreshUmi();
      if (!disposed) timer = window.setTimeout(poll, active === 'umi' ? 1000 : 2000);
    };
    void poll();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [active, authUser, refreshUmi]);

  const logout = useCallback(() => {
    releaseUmiCameraStreams();
    // Leave the UI immediately; the local service clears its token before
    // notifying the platform, so a slow cloud logout must not trap the user.
    logoutRequested.current = true;
    window.localStorage.setItem(logoutPendingKey(controlBase), '1');
    setLogoutBusy(true);
    setLogoutMessage('');
    setAuthNotice('');
    authEpoch.current += 1;
    umiRequestSeq.current += 1;
    platformRequestSeq.current += 1;
    setAuthUser(null);
    setPlatformStatus(null);
    setUmiStatus(null);
    setUmiStatusError(null);
    umiPollFailures.current = 0;
    setActive('teleop');
    setAction(null);
    setError(null);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    void requestJson<{ warning?: string }>(controlBase, '/platform/auth/logout', { method: 'POST', keepalive: true, signal: controller.signal })
      .then((result) => { window.localStorage.removeItem(logoutPendingKey(controlBase)); setLogoutMessage(result.warning ?? ''); })
      .catch(() => { setLogoutMessage('已退出。本机登录信息尚未清理，重新连接后会先清理，不会自动登录。'); })
      .finally(() => { window.clearTimeout(timeout); setLogoutBusy(false); });
  }, [controlBase]);

  const runAction = useCallback(async (nextAction: Exclude<AsyncAction, null>, payload?: Record<string, unknown>) => {
    if (action && nextAction !== 'stop-umi') return false;
    const requestEpoch = authEpoch.current;
    let succeeded = true;
    setAction(nextAction);
    setError(null);
    try {
      if (nextAction === 'check-teleop') {
        setTeleopStatus((current) => current ? { ...current, devices: current.devices.map((device) => ({ ...device, status: 'checking' })) } : current);
        setTeleopStatus(await requestJson<TeleopStatus>(controlBase, '/devices/check', { method: 'POST' }));
      } else if (nextAction === 'start-teleop') {
        await requestJson(controlBase, '/sessions', { method: 'POST', body: JSON.stringify(payload ?? {}) });
      } else if (nextAction === 'stop-teleop') {
        const sessionId = teleopStatus?.session.session_id;
        if (!sessionId) throw new Error('找不到正在运行的遥操作会话');
        await requestJson(controlBase, `/sessions/${encodeURIComponent(sessionId)}/stop`, { method: 'POST' });
      } else if (nextAction === 'move-teleop') {
        await requestJson(controlBase, '/move', { method: 'POST', body: JSON.stringify(payload ?? {}) });
      } else if (nextAction === 'check-umi') {
        releaseUmiCameraStreams();
        const profile = payload?.profile;
        const profileValue = profile === 'single-left' || profile === 'single-right' ? profile : 'dual';
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 60000);
        let checked: UmiStatus;
        try {
          checked = await requestJson<UmiStatus>(controlBase, `/umi/check?collection_profile=${encodeURIComponent(profileValue)}`, { method: 'POST', signal: controller.signal });
        } finally {
          window.clearTimeout(timeout);
        }
        if (requestEpoch === authEpoch.current) {
          umiRequestSeq.current += 1;
          setUmiStatus(checked);
        }
      } else if (nextAction === 'start-umi') {
        releaseUmiCameraStreams();
        const profile = payload?.profile;
        const profileValue = profile === 'single-left' || profile === 'single-right' ? profile : 'dual';
        const cameraRoles = payload?.cameraRoles;
        const encoderMinima = payload?.encoderMinima;
        if (!cameraRoles || typeof cameraRoles !== 'object') throw new Error('没有可用的相机配置');
        if (!encoderMinima || typeof encoderMinima !== 'object') throw new Error('请确认夹爪最小值');
        await requestJson<UmiStatus>(controlBase, '/umi/configure', {
          method: 'POST',
          body: JSON.stringify({
            collection_profile: profileValue,
            camera_roles: cameraRoles,
            capture_encoder_zero: false,
            encoder_minima: encoderMinima,
          }),
        });
        const started = await requestJson<{ session_id: string; state: UmiStatus['state']; started_at: string }>(controlBase, '/umi/sessions', { method: 'POST', body: JSON.stringify({ task: payload?.task, collection_profile: profileValue }) });
        if (requestEpoch === authEpoch.current) {
          umiRequestSeq.current += 1;
          setUmiStatus((current) => current ? { ...current, state: started.state, session_id: started.session_id, started_at: started.started_at, monitor: {}, monitor_connected: false, recording: false, task: typeof payload?.task === 'string' ? payload.task : current.task } : current);
        }
      } else if (nextAction === 'stop-umi') {
        releaseUmiCameraStreams();
        if (!umiStatus?.session_id) throw new Error('找不到正在运行的 UMI 会话');
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 120000);
        let stopped: UmiStatus;
        try {
          stopped = await requestJson<UmiStatus>(controlBase, `/umi/sessions/${encodeURIComponent(umiStatus.session_id)}/stop`, { method: 'POST', signal: controller.signal });
        } finally {
          window.clearTimeout(timeout);
        }
        if (requestEpoch === authEpoch.current) {
          umiRequestSeq.current += 1;
          setUmiStatus(stopped);
        }
      } else if (nextAction.startsWith('record-')) {
        if (nextAction === 'record-start' && (umiStatus?.state !== 'ready' || !umiStatus.monitor_connected)) {
          throw new Error('UMI 采集服务正在启动，请等待状态变为“已就绪”');
        }
        const recorded = await requestJson<UmiStatus>(controlBase, `/umi/record/${nextAction.replace('record-', '')}`, { method: 'POST' });
        if (requestEpoch === authEpoch.current) {
          umiRequestSeq.current += 1;
          setUmiStatus(recorded);
        }
      }
      // The action has succeeded. Secondary status refreshes must not delay
      // navigation back from monitoring or leave controls looking unresponsive.
      if (requestEpoch === authEpoch.current) {
        void refreshTeleop();
        void refreshUmi();
        void refreshPlatform();
      }
    } catch (reason) {
      succeeded = false;
      if (requestEpoch === authEpoch.current) setError(reason instanceof Error && reason.name === 'AbortError' ? '请求超时，请确认当前服务状态后重试。' : reason instanceof Error ? reason.message : '操作失败');
    } finally {
      if (requestEpoch === authEpoch.current) setAction(null);
    }
    return succeeded;
  }, [action, controlBase, refreshPlatform, refreshTeleop, refreshUmi, teleopStatus, umiStatus]);

  const runActionRef = useRef(runAction);
  // Child passive effects may immediately request a new hardware check after
  // stop completes. Publish the cleared action before those effects run.
  useLayoutEffect(() => { runActionRef.current = runAction; }, [runAction]);
  const stableRunAction = useCallback(
    (nextAction: Exclude<AsyncAction, null>, payload?: Record<string, unknown>) => runActionRef.current(nextAction, payload),
    [],
  );

  const activeLogo = theme === 'dark' || (theme === 'system' && systemDark) ? '/K_dark.png' : '/K.png';
  const darkMode = theme === 'dark' || (theme === 'system' && systemDark);
  const platformIsLocal = /^(https?:\/\/)?(localhost|127\.0\.0\.1)(:|\/|$)/i.test(runtime.platformWebUrl);

  if (authChecking) return <main className="app-auth-loading"><LoaderCircle size={22} className="spin" /><span>正在验证登录状态</span></main>;
  if (!authUser) return <LoginScreen baseUrl={controlBase} forgetSaved={logoutRequested.current} logoutBusy={logoutBusy} logoutMessage={logoutMessage} onLogin={(user, warning) => { authEpoch.current += 1; logoutRequested.current = false; setUmiStatus(null); setUmiStatusError(null); umiPollFailures.current = 0; setActive('teleop'); setAction(null); setError(null); setLogoutMessage(''); setAuthNotice(warning ?? ''); setAuthUser(user); }} />;

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand-lockup">
          <button className="brand-mark" type="button" aria-label="打开遥操作采集" onClick={() => setActive('teleop')}>
            <img src={activeLogo} alt="" />
          </button>
          <div><p className="brand-kicker">Robot Data Platform</p><h1>数据采集</h1></div>
        </div>
        <div className="header-right">
          <fieldset className="theme-switcher" aria-label="主题">
            <span>主题</span>
            <div className="theme-options">
              {(['system', 'light', 'dark'] as const).map((mode) => (
                <button key={mode} type="button" className={classNames('theme-option', theme === mode && 'theme-option-active')} aria-pressed={theme === mode} onClick={() => setTheme(mode)}>
                  {mode === 'system' ? '跟随系统' : mode === 'light' ? '浅色' : '深色'}
                </button>
              ))}
            </div>
          </fieldset>
        </div>
      </header>

      <div className="app-body">
        <aside className="side-nav" aria-label="采集类型">
          <div className="side-nav-primary">
            {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
              <button type="button" key={id} className={classNames(active === id && 'active')} aria-current={active === id ? 'page' : undefined} onClick={() => { setActive(id); setError(null); }}>
                <Icon size={16} strokeWidth={1.9} /><span>{label}</span>
              </button>
            ))}
            <span className="side-nav-separator" />
            <a href={runtime.platformWebUrl} target="_blank" rel="noreferrer" title={`打开 ${runtime.platformWebUrl}`}>
              <Database size={16} /><span>跳转平台</span><ExternalLink size={13} className="nav-external" />
              <small>{platformIsLocal ? '本地' : '云端'}</small>
            </a>
          </div>
          <div className="side-account-card">
            <div className="account-avatar">{authUser?.username.slice(0, 1).toUpperCase() ?? ''}</div>
            <div><strong>{authUser?.username ?? ''}</strong><small>v2.0.0</small></div>
            <button type="button" className="account-logout" title="退出登录" aria-label="退出登录" onClick={logout}><LogOut size={14} /></button>
          </div>
        </aside>

        <main className="app-main">
          <section className="page">
            {active !== 'ego' ? (
              <div className="page-tools">
                {active === 'teleop'
                  ? <UsageGuide image="/teleop-usage-guide.png" alt="遥操作手柄按键使用说明" />
                  : <UsageGuide image="/umi-usage-guide.png" alt="UMI 手柄、脚踏板和键盘按键使用说明" />}
              </div>
            ) : null}
            {authNotice ? <output className="inline-alert"><span>{authNotice}</span></output> : null}
            {error?.startsWith('退出登录失败') ? <div className="inline-alert" role="alert"><WifiOff size={16} /><span>{error}</span></div> : null}
            <div className="collection-view" hidden={active !== 'teleop'}>
              <TeleopPanel onRobotModelChange={setTeleopRobotModel} baseUrl={controlBase} status={teleopStatus} preview={preview} darkMode={darkMode} action={action} onAction={stableRunAction} error={error} />
            </div>
            <div className="collection-view" hidden={active !== 'umi'}>
              <UmiPanel baseUrl={controlBase} status={umiStatus} action={action} error={error || umiStatusError} onAction={stableRunAction} visible={active === 'umi'} />
            </div>
            <div className="collection-view" hidden={active !== 'ego'}>
              <EgoPanel />
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}
