'use client';

import { ArrowLeft, ArrowRight, Check } from 'lucide-react';
import { memo, useEffect, useState, type ComponentType } from 'react';
import { ActionButton, SectionTitle, classNames } from '../CollectionUi';
import { G2TeleopWorkspace } from './G2TeleopWorkspace';
import { KuavoTeleopWorkspace } from './KuavoTeleopWorkspace';
import { TianjiTeleopWorkspace } from './TianjiTeleopWorkspace';
import { requestJson } from '../../lib/http';
import type { AsyncAction, PreviewStatus, TeleopStatus } from '../../lib/teleopTypes';

interface TeleopPanelProps {
  baseUrl: string;
  status: TeleopStatus | null;
  preview: PreviewStatus | null;
  darkMode: boolean;
  action: AsyncAction;
  onAction: (action: Exclude<AsyncAction, null>, payload?: Record<string, unknown>) => Promise<boolean>;
  error: string | null;
  onBusyChange?: (busy: boolean) => void;
  onRobotModelChange?: (model: string) => void;
}

// Register a separate workspace for each supported robot. Never fall back to G2
// for an unknown model: its joint limits, telemetry and commands are G2-specific.
const ROBOTS: Record<string, { label: string; Workspace: ComponentType<TeleopPanelProps & { task: string }> }> = {
  'agibot-g2': { label: '智元 G2', Workspace: G2TeleopWorkspace },
  'kuavo-5w': { label: '乐聚 KUAVO 5-W', Workspace: KuavoTeleopWorkspace },
  'tianji': { label: '天机 Marvin M6', Workspace: TianjiTeleopWorkspace },
};

export const TeleopPanel = memo(function TeleopPanel(props: TeleopPanelProps) {
  const [step, setStep] = useState<1 | 2>(1);
  const [uploadTarget,setUploadTarget]=useState<{selected:string;web_url:string}|null>(null);
  useEffect(()=>{
    if(step!==1)return;
    const abort=new AbortController();
    void requestJson<{selected:string;web_url:string}>(props.baseUrl,'/platform/destinations',{signal:abort.signal})
      .then(value=>{if(!abort.signal.aborted)setUploadTarget(value);}).catch(()=>{if(!abort.signal.aborted)setUploadTarget(null);});
    return ()=>abort.abort();
  },[props.baseUrl,step]);
  const [task, setTask] = useState('遥操作数据采集');
  const [robotModel, setRobotModel] = useState('tianji');
  useEffect(() => { props.onRobotModelChange?.(robotModel); }, [robotModel, props.onRobotModelChange]);
  const [configured, setConfigured] = useState<{ task: string; robotModel: string } | null>(null);
  const [robotBusy, setRobotBusy] = useState(false);
  const [returnError, setReturnError] = useState('');
  const robot = Object.hasOwn(ROBOTS, robotModel) ? ROBOTS[robotModel] : undefined;
  const configuredRobot = configured && Object.hasOwn(ROBOTS, configured.robotModel) ? ROBOTS[configured.robotModel] : undefined;
  const Workspace = configuredRobot?.Workspace;
  const canReturn = ['tianji','kuavo-5w'].includes(configured?.robotModel ?? '') ? !robotBusy : props.status?.session.state === 'idle' && props.status?.motion?.state === 'idle' && !props.action;

  async function returnToConfig() {
    if (!canReturn) return;
    if (configured?.robotModel === 'tianji') {
      setRobotBusy(true);
      try { await requestJson(props.baseUrl, '/tianji/close', { method: 'POST' }); }
      catch { setReturnError('无法断开天机 Marvin M6设备，请检查本机服务后重试'); return; }
      finally { setRobotBusy(false); }
    }
    setReturnError('');
    setStep(1);
  }

  function next() {
    if (!task.trim() || !robot) return;
    setTask(task.trim());
    setConfigured({ task: task.trim(), robotModel });
    setStep(2);
  }

  return (
    <div className="teleop-workspace">
      <section className="surface umi-stepper teleop-stepper" aria-label="遥操作采集步骤">
        {[
          { id: 1, title: '选择配置' },
          { id: 2, title: '遥操作采集' },
        ].map((item, index) => (
          <div key={item.id} aria-current={step === item.id ? 'step' : undefined} className={classNames('umi-step', step === item.id && 'active', step > item.id && 'done')}>
            <span>{step > item.id ? <Check size={15} /> : item.id}</span>
            <div><strong>{item.title}</strong></div>
            {index === 0 ? <i /> : null}
          </div>
        ))}
        <div className="teleop-stepper-actions">
          {step === 2 ? (
            <ActionButton onClick={returnToConfig} disabled={!canReturn}><ArrowLeft size={15} />返回配置</ActionButton>
          ) : null}
        </div>
      </section>
      {returnError ? <div className="inline-alert" role="alert">{returnError}</div> : null}

      {step === 1 ? (
        <section className="surface umi-stage-card">
          <SectionTitle eyebrow="STEP 01" title="选择遥操作配置" />
          <form className="umi-stage-body" onSubmit={(event) => { event.preventDefault(); next(); }}>
            {uploadTarget&&<div className="teleop-upload-summary"><div><strong>上传到</strong><p>{uploadTarget.selected==='local'?'上传到当前设备的本地平台':'上传到云端平台'} · {uploadTarget.web_url}</p></div><strong>{uploadTarget.selected==='local'?'本机平台':'云端平台'}</strong></div>}
            <div className="field-grid">
              <label className="field">
                <span>任务名称</span>
                <input className="collection-task-name" type="text" value={task} onChange={(event) => setTask(event.target.value)} maxLength={120} required placeholder="请输入任务名称" />
              </label>
              <label className="field">
                <span>机器人型号</span>
                <select value={robotModel} onChange={(event) => { setRobotModel(event.target.value); setConfigured(null); }}>
                  {Object.entries(ROBOTS).map(([id, definition]) => <option key={id} value={id}>{definition.label}</option>)}
                </select>
              </label>
            </div>
            <div className="stage-actions">
              <button type="submit" className="action-button button-primary" disabled={!task.trim() || !robot}>下一步：遥操作采集 <ArrowRight size={16} /></button>
            </div>
          </form>
        </section>
      ) : null}

      {/* Keep the same robot's settings while editing the task or navigating to
          UMI. Changing the model unmounts its workspace instead of sharing it. */}
      {configured && Workspace ? (
        <div className="teleop-workspace-view" hidden={step !== 2}>
          <Workspace key={configured.robotModel} {...props} task={configured.task} onBusyChange={setRobotBusy} />
        </div>
      ) : null}
    </div>
  );
});
