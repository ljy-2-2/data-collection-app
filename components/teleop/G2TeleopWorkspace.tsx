'use client';
import { Play, RefreshCw, Square, WifiOff } from 'lucide-react';
import { useEffect, useState } from 'react';
import { G2RobotPreview } from '../G2RobotPreview';
import { ActionButton, StatusDot, classNames } from '../CollectionUi';
import { defaultG2JointTargets, G2_JOINT_GROUPS, G2_MANUAL_PRESET, type JointTargets } from '../../lib/teleop';
import { loadTeleopPosePresetState, saveTeleopPosePresetState } from '../../lib/teleopPosePresets';
import type { AsyncAction, TeleopDevice, TeleopStatus, PreviewStatus } from '../../lib/teleopTypes';

const CAMERA_LABELS: Record<string, string> = {
  head_color: '头部彩色',
  head_stereo_left: '头部左目',
  head_stereo_right: '头部右目',
  hand_left_color: '左手相机',
  hand_right_color: '右手相机',
  head_depth: '头部深度',
  hand_left_depth: '左手深度',
  hand_right_depth: '右手深度',
  head_back_fisheye: '后鱼眼',
  head_left_fisheye: '左鱼眼',
  head_right_fisheye: '右鱼眼',
};

export function G2TeleopWorkspace({
  task,
  baseUrl,
  status,
  preview,
  darkMode,
  action,
  onAction,
  error,
}: {
  task: string;
  baseUrl: string;
  status: TeleopStatus | null;
  preview: PreviewStatus | null;
  darkMode: boolean;
  action: AsyncAction;
  onAction: (action: Exclude<AsyncAction, null>, payload?: Record<string, unknown>) => Promise<boolean>;
  error: string | null;
}) {
  const [teleopMode, setTeleopMode] = useState('arm');
  const [scale, setScale] = useState(0.5);
  const [velocity, setVelocity] = useState(0.5);
  const [selectedCamera, setSelectedCamera] = useState('head_color');
  const [extraCameras, setExtraCameras] = useState<Array<'head_depth' | 'hand_left_depth' | 'hand_right_depth'>>([]);
  const [jointTargets, setJointTargets] = useState<JointTargets>(defaultG2JointTargets);
  const [posePresetState, setPosePresetState] = useState(loadTeleopPosePresetState);
  const session = status?.session.state === 'running' ? status.session : null;
  const requiredDevices = status?.devices ?? [
    { id: 'robot', name: '智元 G2', status: 'offline' },
    { id: 'xr', name: 'Galaxis VR', status: 'offline' },
    { id: 'headset', name: 'Pico 设备', status: 'offline' },
  ] as TeleopDevice[];
  const connectedDeviceCount = requiredDevices.filter((device) => device.status === 'connected').length;
  const allReady = requiredDevices.length === 3 && connectedDeviceCount === 3;
  const cameras = preview?.cameras ?? Object.keys(CAMERA_LABELS).map((id) => ({ id, available: false, updated_at: null }));
  const selected = cameras.find((camera) => camera.id === selectedCamera) ?? cameras[0];
  const activeCameraId = selected?.id ?? selectedCamera;
  const frameUrl = selected?.available
    ? `${baseUrl}/cameras/${encodeURIComponent(activeCameraId)}/frame?v=${encodeURIComponent(selected.updated_at ?? preview?.updated_at ?? '')}`
    : '';
  const displayedJoints = { ...jointTargets, ...preview?.joint_positions };
  const motionRunning = status?.motion?.state === 'moving' || action === 'move-teleop';
  const robotReady = requiredDevices.some((device) => device.id === 'robot' && device.status === 'connected');
  const canMove = robotReady && Boolean(preview?.available) && !session && !motionRunning;
  const selectedPosePreset = posePresetState.presets.find((preset) => preset.id === posePresetState.selectedId);

  useEffect(() => {
    saveTeleopPosePresetState(posePresetState);
  }, [posePresetState]);

  const start = () => {
    if (!task.trim() || !allReady || action) return;
    void onAction('start-teleop', {
      task: task.trim(), robot_model: 'agibot-g2', mode: teleopMode,
      scale, max_joint_vel: velocity, extra_cameras: extraCameras,
    });
  };

  const setExtraCamera = (camera: 'head_depth' | 'hand_left_depth' | 'hand_right_depth', checked: boolean) => {
    setExtraCameras((current) => checked ? [...current, camera] : current.filter((item) => item !== camera));
  };

  const submitManualMove = (targets: JointTargets, changedJoint?: string) => {
    const commandId = typeof crypto?.randomUUID === 'function' ? crypto.randomUUID() : `move-${Date.now()}`;
    void onAction('move-teleop', {
      command_id: commandId,
      robot_model: 'agibot-g2',
      mode: 'arm',
      joint_names: G2_MANUAL_PRESET.map(({ name }) => name),
      joint_positions: G2_MANUAL_PRESET.map(({ name }) => targets[name]),
      preset: 'g2-recorded-pose',
      changed_joint: changedJoint,
    });
  };

  const jogJoint = (name: string, delta: number, min: number, max: number) => {
    if (!canMove) return;
    const next = { ...jointTargets, ...preview?.joint_positions };
    next[name] = Number(Math.min(max, Math.max(min, (next[name] ?? min) + delta)).toFixed(6));
    setJointTargets(next);
    submitManualMove(next, name);
  };

  const resetToSelectedPose = () => {
    if (!canMove || !selectedPosePreset || !window.confirm('机器人将立即移动到所选姿态配置，请确认周围安全并且急停可用。继续吗？')) return;
    const next: JointTargets = { ...jointTargets, ...preview?.joint_positions };
    Object.entries(selectedPosePreset.targets).forEach(([name, value]) => {
      if (typeof value === 'number') next[name] = value;
    });
    setJointTargets(next);
    submitManualMove(next);
  };

  const saveCurrentPose = () => {
    const name = window.prompt('请输入姿态配置名称')?.trim();
    if (!name) return;
    const id = typeof crypto?.randomUUID === 'function' ? crypto.randomUUID() : `pose-${Date.now()}`;
    setPosePresetState((current) => ({ presets: [...current.presets, { id, name: name.slice(0, 50), targets: { ...displayedJoints } }], selectedId: id }));
  };

  const deleteCurrentPose = () => {
    if (!selectedPosePreset || !window.confirm('确定删除当前姿态配置吗？')) return;
    setPosePresetState((current) => {
      const presets = current.presets.filter(({ id }) => id !== current.selectedId);
      return { presets, selectedId: presets[0]?.id ?? '' };
    });
  };

  return (
    <div className="teleop-workspace">
      <section className="surface teleop-control-panel">
        <div className="teleop-setup-flow teleop-g2-setup-flow">
          <section className="teleop-setup-section teleop-mode-section">
            <div className="teleop-setup-label"><span>01</span><strong>遥操作模式</strong></div>
            <select value={teleopMode} onChange={(event) => setTeleopMode(event.target.value)} disabled={Boolean(session)} aria-label="遥操作模式">
              <option value="arm">双臂</option>
            </select>
            <div className="teleop-mode-parameters">
              <label><span>速度</span><input aria-label="速度" type="number" min="0.1" max="1" step="0.1" value={velocity} disabled={Boolean(session)} onChange={(event) => setVelocity(Number(event.target.value))} /><small>rad/s</small></label>
              <label><span>幅度</span><input aria-label="幅度" type="number" min="0.1" max="1" step="0.1" value={scale} disabled={Boolean(session)} onChange={(event) => setScale(Number(event.target.value))} /><small>×</small></label>
            </div>
          </section>
          <section className="teleop-setup-section teleop-devices-section">
            <div className="teleop-setup-label"><span>02</span><strong>所需设备</strong><small>{connectedDeviceCount}/3</small></div>
            <div className="teleop-required-devices">
              {requiredDevices.map((device) => (
                <article className={classNames('teleop-required-device', `device-${device.status}`)} key={device.id}>
                  <span>{device.id === 'robot' ? '智元 G2' : device.id === 'xr' ? 'Galaxis VR' : 'Pico 设备'}</span>
                  <strong>{device.status === 'connected' ? '已连接' : device.status === 'checking' ? '检测中' : device.status === 'warning' ? '需检查' : '未连接'}</strong>
                </article>
              ))}
            </div>
          </section>
        </div>
        {error ? <div className="inline-alert teleop-panel-alert"><WifiOff size={16} /><span>{error}</span></div> : null}
        <div className="teleop-control-footer">
          <div className="teleop-extra-options">
            <span>额外信息保存：</span>
            <label><input type="checkbox" checked={extraCameras.includes('head_depth')} disabled={Boolean(session)} onChange={(event) => setExtraCamera('head_depth', event.target.checked)} />头部深度</label>
            <label><input type="checkbox" checked={extraCameras.includes('hand_left_depth')} disabled={Boolean(session)} onChange={(event) => setExtraCamera('hand_left_depth', event.target.checked)} />左手深度</label>
            <label><input type="checkbox" checked={extraCameras.includes('hand_right_depth')} disabled={Boolean(session)} onChange={(event) => setExtraCamera('hand_right_depth', event.target.checked)} />右手深度</label>
          </div>
          <div className="teleop-control-actions">
            <ActionButton kind="secondary" onClick={() => onAction('check-teleop')} busy={action === 'check-teleop'} spinIcon disabled={Boolean(session)}><RefreshCw size={16} />检查设备</ActionButton>
            {session ? <ActionButton danger onClick={() => onAction('stop-teleop')} busy={action === 'stop-teleop'}><Square size={15} fill="currentColor" />结束遥操作</ActionButton> : <ActionButton onClick={start} busy={action === 'start-teleop'} disabled={!allReady || Boolean(action) || !task.trim()}><Play size={16} fill="currentColor" />开始遥操作采集</ActionButton>}
          </div>
        </div>
      </section>

      <div className="teleop-observation-grid">
        <section className="surface teleop-visualizer">
          <div className="preview-head">
            <div><h3>机器人姿态</h3><span>{preview?.available ? '实时关节状态' : '目标姿态'}</span></div>
            <div className="compact-status"><StatusDot online={Boolean(preview?.available)} pulse={Boolean(preview?.available)} />{preview?.available ? '遥测在线' : '无遥测'}</div>
          </div>
          <div className="teleop-visual-stage">
            <G2RobotPreview joints={displayedJoints} live={Boolean(preview?.available)} darkMode={darkMode} />
          </div>
        </section>

        <section className="surface teleop-camera">
          <div className="preview-head">
            <div><h3>相机画面</h3></div>
            <select className="camera-switcher" value={activeCameraId} onChange={(event) => setSelectedCamera(event.target.value)} aria-label="切换相机画面">
              {cameras.map((camera) => <option key={camera.id} value={camera.id}>{CAMERA_LABELS[camera.id] ?? camera.id}</option>)}
            </select>
          </div>
          <div className="teleop-camera-stage">{frameUrl ? <img src={frameUrl} alt="相机实时画面" /> : null}</div>
        </section>
      </div>

      <section className="surface teleop-manual">
        <div className="preview-head"><div><h3>手动控制</h3></div><span className="badge badge-amber">真实运动</span></div>
        <div className="teleop-manual-body">
          <div className="teleop-preset-toolbar">
            <div><strong>姿态配置</strong></div>
            <div className="teleop-preset-controls">
              <select aria-label="姿态配置" value={posePresetState.selectedId} disabled={motionRunning || Boolean(session) || posePresetState.presets.length === 0} onChange={(event) => setPosePresetState((current) => ({ ...current, selectedId: event.target.value }))}>
                {posePresetState.presets.length === 0 ? <option value="">暂无配置</option> : null}
                {posePresetState.presets.map((preset) => <option value={preset.id} key={preset.id}>{preset.name}</option>)}
              </select>
              <ActionButton onClick={resetToSelectedPose} disabled={!canMove || !selectedPosePreset}>复位</ActionButton>
              <ActionButton kind="secondary" onClick={saveCurrentPose}>保存当前姿态</ActionButton>
              <ActionButton kind="ghost" danger onClick={deleteCurrentPose} disabled={!selectedPosePreset}>删除</ActionButton>
            </div>
          </div>
          <div className="teleop-joint-columns">
            {G2_JOINT_GROUPS.map((group) => (
              <section className="teleop-joint-column" key={group.id}>
                <header><h4>{group.label}</h4><span>{group.joints.length}</span></header>
                <div>{group.joints.map((joint) => {
                  const value = displayedJoints[joint.name] ?? joint.value;
                  return <div className="teleop-joint" key={joint.name}>
                    <span><strong>{joint.label}</strong><small>{joint.name}</small></span>
                    <div className="teleop-joint-stepper"><output>{value.toFixed(4)}</output><div>
                      <button type="button" aria-label={`提高${joint.label}`} disabled={!canMove || value >= joint.max} onClick={() => jogJoint(joint.name, joint.step, joint.min, joint.max)}>▲</button>
                      <button type="button" aria-label={`降低${joint.label}`} disabled={!canMove || value <= joint.min} onClick={() => jogJoint(joint.name, -joint.step, joint.min, joint.max)}>▼</button>
                    </div></div>
                  </div>;
                })}</div>
              </section>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}
