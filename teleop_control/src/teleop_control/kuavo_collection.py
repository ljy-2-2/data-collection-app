"""Local A/B episodes. Recording never opens a robot channel or writes controls.

Record real sensor feedback and unsampled accepted targets. Camera alignment uses
PC receive time, not an invented shared/exposure clock. Missing measurements stay
null. Save received data without duration, rate or continuity acceptance gates.
"""
from __future__ import annotations
import bisect
import copy
from datetime import datetime, timezone
from collections import deque
import json
import math
from pathlib import Path
import queue
import threading
import time
import uuid
from pydantic import BaseModel, ConfigDict
from typing import Literal
from .collection_gate import collection_available, collection_context
from .collection_uploader import READY_MARKER
from .kuavo_preview import ARM_NAMES
from .kuavo_recording import decode_events

ROLES=('head','left','right')
RECORDING_POLICY='received_data_v1'

def dump(path, value):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2))
    temp.replace(path)

def line(file,value):file.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n')

def utc():return datetime.now(timezone.utc).isoformat()

class CollectionSettings(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    enabled:bool=True
    start_button:Literal['A','X']='A'
    save_button:Literal['B']='B'

class KuavoCollection:
    def __init__(self, manager, output):
        self.manager=manager;self.output=Path(output);self.path=self.output.parent/'collection.json'
        self.settings=CollectionSettings().model_dump();self.error=''
        try:
            if self.path.exists():self.settings=CollectionSettings(**json.loads(self.path.read_text())).model_dump()
        except (OSError,ValueError) as exc:self.error='采集设置读取失败：'+str(exc)
        self.lock=threading.RLock();self.armed=False;self.phase='idle';self.job=None;self.session=''
        self.completed=0;self.last_episode=None;self.started=None;self.frames=0;self.uploader=None
        self.queue=queue.Queue(maxsize=2048);self.overflow=threading.Event();self.quit=threading.Event()

    def active(self):return self.phase in ('recording','finalizing')

    def configure(self,body):
        with self.lock:
            if self.armed or self.active():raise ValueError('请结束遥操作后修改采集按键')
            value=body.model_dump();self.path.parent.mkdir(parents=True,exist_ok=True);dump(self.path,value);self.settings=value
        return self.snapshot()

    def arm(self, options):
        with self.lock:
            if self.job and self.job.is_alive():raise ValueError('上一会话采集仍在保存')
            self.options=copy.deepcopy(options);self.session=uuid.uuid4().hex;self.armed=True
            self.recent_packets=deque(maxlen=8)
            self.queue=queue.Queue(maxsize=2048);self.overflow.clear();self.quit.clear()
            self.phase='ready' if self.settings['enabled'] else 'disabled';self.error='';self.started=None
            self.job=threading.Thread(target=self._run,name='kuavo-collection',daemon=True);self.job.start()

    def accept(self, events, overflow=False):
        if not self.armed:return
        if overflow:self.overflow.set()
        for event in events:
            try:
                self.queue.put_nowait(event)
            except queue.Full:self.overflow.set();break

    def disarm(self):
        self.armed=False;self.quit.set()

    def close(self):
        self.disarm()
        if self.job:self.job.join(timeout=20)

    def snapshot(self):
        with self.lock:
            return dict(state=self.phase,armed=self.armed,recording=self.phase=='recording',busy=self.active(),
                settings=dict(self.settings),error=self.error,completed=self.completed,frames=self.frames,
                elapsed_s=round(time.monotonic()-self.started,1) if self.started and self.active() else 0,
                last_episode=self.last_episode,upload=self.uploader.status() if self.uploader else None)

    def _run(self):
        episode=None;last_buttons=None
        try:
            while not self.quit.is_set() or not self.queue.empty():
                try:row=self.queue.get(timeout=.01)
                except queue.Empty:row=None
                now=time.monotonic_ns()
                if row and row.get('kind')=='input':
                    buttons=row['buttons'];stamp=row['pc_monotonic_ns']
                    valid=row.get('enabled') and row.get('fresh')
                    edges=[] if not valid or last_buttons is None else [k for k,v in buttons.items() if v and not last_buttons.get(k)]
                    last_buttons=buttons if valid else None
                    # Stop wins simultaneous presses. Y is exclusively robot return.
                    if self.settings['save_button'] in edges:
                        if episode and 'stop_ns' not in episode:
                            self.phase='finalizing';episode['stop_ns']=stamp
                            episode['stop_unix_ns']=time.time_ns()
                            episode['flush_until_ns']=now+250_000_000
                        elif not self.settings['enabled']:
                            self.error='采集已关闭，请在按键设置中启用采集'
                        elif self.phase not in ('saved','incomplete'):
                            # Preserve a failed A preflight instead of silently
                            # accepting B when no recording was ever created.
                            self.error=self.error or f"尚未开始采集，请先按 {self.settings['start_button']}，看到正在采集后再按 B 保存"
                    elif self.settings['start_button'] in edges and episode is None and self.settings['enabled'] and not self.quit.is_set():
                        try:
                            if not collection_available():raise RuntimeError('请先登录所选采集平台')
                            episode=self._begin(collection_context());self.overflow.clear();self.phase='recording';self.started=time.monotonic();self.error='';self.frames=0
                        except Exception as exc:self.error=str(exc)
                elif row and row.get('kind')=='fault':
                    if episode:
                        self._warn(episode,row.get('error') or '遥操作异常')
                elif row and row.get('kind')=='joint_sample':
                    if episode and row['pc_monotonic_ns']>=episode['start_ns']:episode['joint_samples'].append(row)
                elif row and row.get('kind')=='packet':
                    packet=row['packet']
                    if packet.get('event')=='status' or packet.get('kind') in ('collection_batch','feedback_batch'):
                        self.recent_packets.append(row)
                    if episode:
                        episode['events'].append(row)
                        motors=(packet.get('grippers') or {}).get('motors') or {}
                        if any(m.get('state')=='FAULT' for m in motors.values() if isinstance(m,dict)):
                            self._warn(episode,'夹爪反馈报告故障')
                if episode and 'flush_until_ns' in episode and now>=episode['flush_until_ns']:
                    self._finish(episode,True);episode=None
                if episode:
                    self._frames(episode)
                    if self.overflow.is_set():self._warn(episode,'采集缓存溢出，部分数据缺失')
            if episode:
                if 'stop_ns' not in episode:episode['closure_reason']='session_end'
                self._finish(episode,True);episode=None
        except Exception as exc:
            self.error=str(exc);self.phase='incomplete'
        finally:
            if episode:
                episode['error']=self.error or '采集异常退出'
                try:self._finish(episode,False)
                except Exception:pass
            if self.phase=='ready':self.phase='idle'

    @staticmethod
    def _warn(ep, message):
        warnings=ep.setdefault('warnings',[])
        if message not in warnings:warnings.append(message)

    def _begin(self, context):
        with self.manager.cameras.lock:feeds=dict(self.manager.cameras.feeds)
        directory=self.output/(time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]);directory.mkdir(parents=True)
        (directory/'record').mkdir()
        for role in ROLES:(directory/'camera'/(role+'_color')/'frames').mkdir(parents=True)
        self.last_episode=str(directory)
        return dict(directory=directory,context=context,started=utc(),start_unix_ns=time.time_ns(),start_ns=time.monotonic_ns(),feeds=feeds,
                    sequence={r:None for r in ROLES},cameras={r:[] for r in ROLES},events=list(getattr(self,'recent_packets',[])),joint_samples=[],error='',warnings=[])

    def _frames(self,ep):
        with self.manager.cameras.lock:
            feeds=dict(self.manager.cameras.feeds)
        for role in ROLES:
            feed=feeds.get(role)
            if feed is not ep['feeds'].get(role):
                self._warn(ep,role+' 相机连接更换');ep['sequence'][role]=None
            ep['feeds'][role]=feed
            if feed is None or not feed.snapshot()['connected']:
                self._warn(ep,role+' 相机画面缺失');continue
            with feed.lock:
                seq=feed.sequence;stamp=round(feed.received*1e9)
                if seq==ep['sequence'][role] or stamp<ep['start_ns'] or stamp>ep.get('stop_ns',stamp):continue
                jpeg=feed.frame;width=feed.width;height=feed.height;source_stamp=getattr(feed,'source_stamp',None)
            if ep['sequence'][role] is not None and seq!=ep['sequence'][role]+1:self._warn(ep,role+' 相机部分帧缺失')
            ep['sequence'][role]=seq
            rows=ep['cameras'][role];index=len(rows);name=role+'_color'
            filename=f'{index:08d}.jpg';(ep['directory']/'camera'/name/'frames'/filename).write_bytes(jpeg)
            rows.append(dict(timestamp_ns=stamp,pc_recv_ns=stamp,source_monotonic_ns=source_stamp,sequence=seq,
                             frame_index=index,filename=filename,stored_width=width,stored_height=height,
                             timestamp_is_exposure_time=False))
        self.frames=max(len(rows) for rows in ep['cameras'].values())

    def _finish(self,ep,saved):
        self.phase='finalizing';ended=utc();stop_unix_ns=ep.get('stop_unix_ns',time.time_ns());directory=ep['directory'];record=directory/'record'
        stop_ns=ep.get('stop_ns',time.monotonic_ns())
        duration_s=max((stop_ns-ep['start_ns'])/1e9,.000001)
        actions=[];joints=[];grippers={side:[] for side in ('left','right')};previous_source=None;previous_seq=None
        for sample in ep.get('joint_samples',[]):
            if not ep['start_ns']<=sample['pc_monotonic_ns']<=stop_ns:continue
            source=sample['source_timestamp_ns'];sequence=sample['source_sequence']
            positions=sample['position_rad'];velocity=sample.get('velocity_rad_s') or [None]*14
            if (sample.get('joint_names')!=list(ARM_NAMES) or len(positions)!=14
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in positions)
                or len(velocity)!=14):
                ep['error']=ep['error'] or '关节采集格式不匹配';continue
            if type(source) is not int or source<=0 or type(sequence) is not int:
                ep['error']=ep['error'] or '关节时间戳或序号格式无效';continue
            if previous_source is not None:
                if source<=previous_source or sequence<=previous_seq:
                    ep['error']=ep['error'] or '关节时间戳或序号回退';continue
                if sequence!=previous_seq+1:self._warn(ep,'关节采集部分帧缺失')
            previous_source,previous_seq=source,sequence
            joints.append(dict(timestamp_ns=sample['pc_monotonic_ns'],source_timestamp_ns=source,sequence=sequence,
                source_age_ms=sample.get('source_age_ms'),
                joint_names=list(ARM_NAMES),position_rad=positions,position_deg=[math.degrees(v) for v in positions],
                velocity_deg_s=[math.degrees(v) if type(v) in (int,float) and math.isfinite(v) else None for v in velocity],torque_nm=[None]*14))
        joint_hz=len(joints)/duration_s
        source_hz=(len(joints)-1)*1e9/(joints[-1]['source_timestamp_ns']-joints[0]['source_timestamp_ns']) if len(joints)>1 else 0
        joint_quality=len(joints)>1 and joint_hz>=98 and 98<=source_hz<=102
        with (record/'teleop_feedback.jsonl').open('w') as raw:
            for row in ep['events']:line(raw,row)
        actions,grippers,recording=decode_events(ep['events'],ep['start_ns'],stop_ns)
        arm_references=recording.pop('arm_target_references',[])
        if recording['errors']:ep['error']=ep['error'] or '; '.join(recording['errors'])
        for warning in recording['warnings']:self._warn(ep,warning)
        for name,rows in [('joints',joints),('actions',actions),('arm_target_references',arm_references),*[(s+'_gripper',r) for s,r in grippers.items()]]:
            with (record/(name+'.jsonl')).open('w') as f:
                for row in rows:line(f,row)
        for role,rows in ep['cameras'].items():
            name=role+'_color'
            with (directory/'camera'/name/(name+'.txt')).open('w') as index,(record/(name+'_frames.jsonl')).open('w') as f:
                for row in rows:index.write(f"{row['timestamp_ns']} {row['frame_index']}\n");line(f,row)
        for rows in grippers.values():rows.sort(key=lambda r:r['timestamp_ns'])
        mode=self.options.get('arm_mode','dual');sides=[mode] if mode in ('left','right') else ['left','right']
        time_index={id(rows):[r['timestamp_ns'] for r in rows] for rows in [joints,actions,*grippers.values(),*ep['cameras'].values()]}
        def nearest(rows,stamp,limit=150_000_000):
            times=time_index[id(rows)];i=bisect.bisect_left(times,stamp)
            choices=[rows[j] for j in (i-1,i) if 0<=j<len(rows)]
            best=min(choices,key=lambda r:abs(r['timestamp_ns']-stamp)) if choices else None
            return best if best and abs(best['timestamp_ns']-stamp)<=limit else None
        reference_times=[r['timestamp_ns'] for r in arm_references]
        def action_at(stamp):
            if recording['full_publication_stream']:
                index=bisect.bisect_right(reference_times,stamp)-1
                return arm_references[index] if index>=0 else None
            return nearest(actions,stamp)
        bundles=[]
        # Sparse preview rows never determine whether raw data may be saved.
        # A missing/unaligned source stays null; all real rows remain in their
        # own files with original timestamps, including long gaps.
        anchors=(ep['cameras']['head'] or next((r for r in ep['cameras'].values() if r),None)
                 or joints or grippers['left'] or grippers['right'] or actions)
        for head in anchors:
            stamp=head['timestamp_ns'];cams={r:nearest(rows,stamp,50_000_000) for r,rows in ep['cameras'].items()}
            state=nearest(joints,stamp);action=action_at(stamp)
            positions=state['position_rad'] if state else [None]*14
            tensors={'state__joint__name':list(ARM_NAMES),'state__joint__position':positions,'action__joint__name':list(ARM_NAMES),'action__joint__position':action['position_rad'] if action else [None]*14}
            for side,offset in (('left',0),('right',7)):
                tensors['state__'+side+'_arm__position']=positions[offset:offset+7]
                motor=nearest(grippers[side],stamp)
                tensors['state__'+side+'_effector__position']=[motor['position_rad'] if motor else None]
                tensors['action__'+side+'_effector__position']=[motor.get('target_rad') if motor else None]
            bundles.append(dict(seq=len(bundles)+1,timestamp_ns=stamp,tensors=tensors,camera_frame_index={r+'_color':(v['frame_index'] if v else None) for r,v in cams.items()},cam_ts={r+'_color':(v['timestamp_ns'] if v else None) for r,v in cams.items()},cam_delta_ms={r+'_color':((v['timestamp_ns']-stamp)/1e6 if v else None) for r,v in cams.items()},gripper_target_sources={side:(nearest(grippers[side],stamp) or {}).get('target_reference') for side in sides},gripper_delta_ms={side:((nearest(grippers[side],stamp)['timestamp_ns']-stamp)/1e6 if nearest(grippers[side],stamp) else None) for side in sides},action_reference_source=action.get('source','observed_publication') if action else None,action_source_timestamp_ns=action.get('source_timestamp_ns') if action else None,action_observed=action is not None,action_delta_ms=(action['timestamp_ns']-stamp)/1e6 if action else None,joint_delta_ms=(state['timestamp_ns']-stamp)/1e6 if state else None,alignment_kind='nearest_pc_receive_time',hardware_synchronized=False))
        with (record/'bundles.jsonl').open('w') as f:
            for row in bundles:line(f,row)
        with (record/'aligned_joints.jsonl').open('w') as aligned:
            for state in joints:
                stamp=state['timestamp_ns'];action=action_at(stamp)
                line(aligned,dict(**state,action_position_rad=action['position_rad'] if action else [None]*14,
                    action_observed=action is not None,action_reference_source=action.get('source','observed_publication') if action else None,
                    action_source_timestamp_ns=action.get('source_timestamp_ns') if action else None,action_delta_ms=(action['timestamp_ns']-stamp)/1e6 if action else None,
                    grippers={side:nearest(rows,stamp) for side,rows in grippers.items()},
                    alignment_kind='nearest_pc_receive_time',hardware_synchronized=False))
        grippers_complete=bool(bundles) and all(b['tensors']['state__'+side+'_effector__position'][0] is not None for b in bundles for side in sides)
        if not grippers_complete:self._warn(ep,'部分预览帧缺少可对齐的夹爪实测记录')
        data_available=bool(joints or actions or any(grippers.values()) or any(ep['cameras'].values()))
        if not data_available:ep['error']=ep['error'] or '本轮没有收到可保存的数据'
        complete=bool(saved and not ep['error'] and data_available)
        meta=dict(schema_version=4,recording_policy=RECORDING_POLICY,action_samples=len(actions),action_bundles=sum(b['action_observed'] for b in bundles),robot_type='kuavo',robot_name='乐聚 KUAVO 5-W',task=self.options.get('task','KUAVO 遥操作'),started_at=ep['started'],ended_at=ended,pc_start_ns=ep['start_unix_ns'],pc_stop_ns=stop_unix_ns,complete=complete,actions_recorded=bool(actions),controlled_sides=sides,joint_order=list(ARM_NAMES),parameters=self.options.get('parameters',{}),camera_mapping={r:r+'_color' for r in ROLES},cameras={r+'_color':dict(frames=len(rows),width=rows[0]['stored_width'] if rows else None,height=rows[0]['stored_height'] if rows else None) for r,rows in ep['cameras'].items()},frames_recorded=len(bundles),requested_image_hz=30,requested_joint_hz=100,actual_joint_hz=joint_hz,source_joint_hz=source_hz,joint_samples=len(joints),joint_quality_passed=joint_quality,actual_action_hz=len(actions)/duration_s,requested_gripper_hz=100,gripper_statistics=recording['grippers'],gripper_quality_passed=all(recording['grippers'][s]['target_rate_met'] for s in sides),recording_integrity=recording,gripper_action_policy='initial_feedback_then_last_target',arm_action_policy='last_observed_publication' if recording['full_publication_stream'] else 'nearest_legacy_diagnostic',sample_clock='pc_monotonic_ns',timestamp_is_exposure_time=False,hardware_synchronized=False,full_rate_dataset=False,joint_sampling='sensors_data_raw_100hz',action_sampling='every_publication' if recording['full_publication_stream'] else 'legacy_diagnostic_stream_up_to_10hz',action_delivery_scope='publisher_enqueue_only',missing_measurements_are_null=True,error=ep['error'] or ('' if complete else '本轮未完成保存'),**ep['context'])
        meta.update(acceptance_policy='received_data_without_rate_or_duration_gate',
            data_available=data_available,quality_warnings=ep.get('warnings',[]),
            closure_reason=ep.get('closure_reason','save_button'),
            alignment_windows_ms={'joint_and_gripper':150,'camera':50},
            alignment_windows_scope='preview_only_raw_samples_not_filtered')
        dump(directory/'meta_info.json',meta)
        if complete:
            dump(directory/READY_MARKER,dict(import_task_id='kuavo-'+self.session,import_folder=self.options.get('task','KUAVO 遥操作'),teleop_session_id=self.session,requested_cameras=[r+'_color' for r in ROLES if ep['cameras'][r]],source='teleop',collection_trigger='controller',**ep['context']))
            self.completed+=1;self.phase='saved';self.error=''
        else:self.phase='incomplete';self.error=meta['error']
