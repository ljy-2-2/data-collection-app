"""Record the TLS stream in the user's agibot episode directory convention."""
import bisect
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import time
import uuid
from PIL import Image
from .grippers import GripperSampler

CAMERAS = {'usb-1':'usb_1_color','usb-2':'usb_2_color','usb-4':'usb_4_color'}

def dump(path, obj):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(path)

def line(handle, obj):
    handle.write(json.dumps(obj,ensure_ascii=False,separators=(',',':'))+'\n')

def percentiles(values):
    a = sorted(values)
    if not a: return None
    return {k:a[round((len(a)-1)*q)] for k,q in [('p50',.5),('p95',.95),('p99',.99),('max',1)]}

def stats(rows):
    ts = [r['timestamp_ns'] for r in rows]
    return {'count':len(ts), 'span_s':(ts[-1]-ts[0])/1e9 if len(ts)>1 else 0,
            'actual_hz':(len(ts)-1)*1e9/(ts[-1]-ts[0]) if len(ts)>1 else None,
            'interval_ms':percentiles([(b-a)/1e6 for a,b in zip(ts,ts[1:])])}

def encode_image(directory, event, index):
    metadata, data = dict(event.metadata), event.data
    camera = CAMERAS[metadata['camera_id']]
    if metadata.get('width') != 1024 or metadata.get('height') != 768:
        raise ValueError('Unexpected native dimensions')
    with Image.open(io.BytesIO(data)) as original:
        if original.format != 'JPEG' or original.size != (1024,768):
            raise ValueError('Invalid camera JPEG')
        original.load()
        rgb = original.convert('RGB').resize((640,480),Image.Resampling.BILINEAR)
    filename = '%08d.jpg' % index
    base = directory/'camera'/camera
    # Native JPEG is retained; the reference-format frames are actual 640x480.
    (base/'native'/filename).write_bytes(data)
    rgb.save(base/'frames'/filename,format='JPEG',quality=95,subsampling=0)
    metadata.update(camera_name=camera,frame_index=index,stored_width=640,stored_height=480,
                    native_sha256=hashlib.sha256(data).hexdigest())
    return metadata

def tensors(joint):
    return {'state__joint__name':joint['joint_names'],
            'state__joint__position':[math.radians(v) for v in joint['position_deg']],
            'state__joint__velocity':[math.radians(v) for v in joint['velocity_deg_s']],
            'state__joint__effort':joint['torque_nm']}

def match_bundles(joints, cameras, tolerance_ns=25_000_000):
    """Match camera triplets, then the joint nearest their timestamp midpoint.

    Unmatched original camera frames and all joint samples remain in the episode.
    A skipped matching row is not represented as a successful synchronized sample.
    """
    if not joints or any(not rows for rows in cameras.values()): return []
    names=list(cameras)
    timestamps={k:[r['timestamp_ns'] for r in rows] for k,rows in cameras.items()}
    joint_times=[j['timestamp_ns'] for j in joints]
    cursor={k:0 for k in names}
    bundles=[]
    while all(cursor[k]<len(timestamps[k]) for k in names):
        cam_ts={k:timestamps[k][cursor[k]] for k in names}
        low,high=min(cam_ts.values()),max(cam_ts.values())
        if high-low>tolerance_ns:
            cursor[min(cam_ts,key=cam_ts.get)]+=1
            continue
        anchor=(low+high)//2
        insertion=bisect.bisect_left(joint_times,anchor)
        candidates=[i for i in (insertion-1,insertion) if 0<=i<len(joints)]
        ji=min(candidates,key=lambda i:abs(joint_times[i]-anchor))
        joint=joints[ji];t=joint['timestamp_ns']
        per_camera={}
        for k,ct in cam_ts.items():
            j=bisect.bisect_left(joint_times,ct)
            candidates=[i for i in (j-1,j) if 0<=i<len(joints)]
            j=min(candidates,key=lambda i:abs(joint_times[i]-ct))
            per_camera[k]={'joint_sample_index':joints[j]['sequence'],
                'joint_timestamp_ns':joint_times[j],'delta_ms':(ct-joint_times[j])/1e6}
        if all(abs(ct-t)<=tolerance_ns for ct in cam_ts.values()):
            bundles.append({'seq':len(bundles)+1,'timestamp_ns':t,'pc_recv_ns':joint['pc_recv_ns'],
                'cam_ts':cam_ts,'cam_delta_ms':{k:(v-t)/1e6 for k,v in cam_ts.items()},
                'tensors':tensors(joint),'joint_frame_serial':joint['frame_serial'],
                'joint_sample_index':joint['sequence'],'joint_tick':joint['tick'],
                'camera_frame_index':cursor.copy(),'camera_span_ms':(high-low)/1e6,
                'joint_per_camera':per_camera,
                'alignment_kind':'camera_triplet_midpoint_nearest_joint','hardware_synchronized':False})
        for k in names:cursor[k]+=1
    return bundles


def attach_grippers(bundles, samples):
    """Keep gripper feedback separate from the 14 arm joints; no fake zero state."""
    timestamps={side:[g['motors'][side]['fields']['position_rad']['controller_timestamp_estimate_ns'] for g in samples] for side in ('left','right')}
    for bundle in bundles:
        bundle['grippers']={}
        bundle['gripper_delta_ms']={}
        for side in ('left','right'):
            times=timestamps[side]
            j=bisect.bisect_left(times,bundle['timestamp_ns'])
            choices=[i for i in (j-1,j) if 0<=i<len(times)]
            if not choices:continue
            i=min(choices,key=lambda i:abs(times[i]-bundle['timestamp_ns']))
            delta=times[i]-bundle['timestamp_ns']
            if abs(delta)>100_000_000:continue
            motor=samples[i]['motors'][side]
            bundle['grippers'][side]={'sample_index':samples[i]['sequence'],**motor}
            bundle['gripper_delta_ms'][side]=delta/1e6
            prefix='state__'+side+'_effector__'
            bundle['tensors'][prefix+'name']=[side+'_gripper_motor']
            bundle['tensors'][prefix+'position']=[motor['fields']['position_rad']['value']]
            bundle['tensors'][prefix+'output_encoder_position']=[motor['fields']['output_encoder_rad']['value']]

def map_gripper_clocks(samples, before, after):
    """Use two clock anchors to compensate measured PC/controller frequency drift.
    Raw request/reply clocks remain intact; this is still a software estimate.
    """
    if before['boot_id']!=after['boot_id']:raise RuntimeError('Clock anchors span a controller reboot')
    span=after['pc_midpoint_ns']-before['pc_midpoint_ns']
    if span<=0:raise ValueError('Clock anchors are not ordered')
    offset_change=after['offset_ns']-before['offset_ns']
    for sample in samples:
        for motor in sample['motors'].values():
            for field in motor['fields'].values():
                start,end=field['pc_query_start_ns'],field['pc_reply_ns']
                midpoint=(start+end)//2
                fraction=(midpoint-before['pc_midpoint_ns'])/span
                offset=before['offset_ns']+round(fraction*offset_change)
                field['controller_timestamp_estimate_ns']=midpoint+offset
                field['timestamp_model']='two_anchor_affine_clock_estimate'
                field['clock_mapping_uncertainty_ns']=max(before['uncertainty_ns'],after['uncertainty_ns'])
                field['timestamp_uncertainty_estimate_ns']=(end-start)//2+field['clock_mapping_uncertainty_ns']
        sample['timestamp_ns']=max(m['fields']['position_rad']['controller_timestamp_estimate_ns'] for m in sample['motors'].values())

def record_episode(client, output_root, seconds=60, hz=30, joint_hz=100, progress=print,
                   gripper_source=None, gripper_hz=20):
    root = Path(output_root).expanduser()
    root.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(root).free < seconds*15_000_000+2_000_000_000:
        raise RuntimeError('Insufficient disk space for estimated JPEG recording plus reserve')
    episode_id = time.strftime('%Y%m%d_%H%M%S')+'_fusion_'+uuid.uuid4().hex[:8]
    directory = root/episode_id
    directory.mkdir(exist_ok=False)
    record = directory/'record'
    record.mkdir()
    cameras = {name:[] for name in CAMERAS.values()}
    counts = {name:0 for name in CAMERAS.values()}
    for name in cameras:
        (directory/'camera'/name/'frames').mkdir(parents=True)
        (directory/'camera'/name/'native').mkdir()
    meta = {'schema_version':1,'episode_id':episode_id,'task':'FUSION stationary dual-arm and three-camera validation',
        'robot_type':'fusion_dual_arm','collector':'fusion_camera_tls','pc_start_ns':time.time_ns(),
        'requested_seconds':seconds,'target_hz':hz,'joint_hz':joint_hz,
        'grippers_requested':gripper_source is not None,'gripper_hz':gripper_hz if gripper_source else None,
        'complete':False,'status':'recording',
        'timestamp_clock':'FUSION CLOCK_MONOTONIC','timestamp_is_exposure_time':False,
        'joint_source':'/dev/FXDcss O_RDONLY + PROT_READ','joint_order':'left 1..7, right 1..7',
        'position_unit':'rad','velocity_unit':'rad/s','effort_unit':'Nm',
        'native_joint_position_unit':'degree','native_joint_velocity_unit':'degree/s',
        'actions_recorded':False,'action_backfilled':[],
        'reference_episode':'/home/yeap/agibot_episodes/right_arm/20260725_150042_62c1cdc4',
        'config':{'robot_type':'fusion_dual_arm','output_root':str(root),'target_hz':hz,
                  'cameras':[{'name':n,'usb_port_id':k,'essential':True} for k,n in CAMERAS.items()]}}
    dump(directory/'meta_info.json',meta)
    dump(record/'image_preprocessing.json',{'native_size':[1024,768],'stored_size':[640,480],
        'method':'Pillow BILINEAR resize on laptop','output':'JPEG quality95 subsampling0',
        'native_jpeg_retained':True,'physical_camera_roles':'USB port labels, mounting roles not assumed'})
    joints, pending = [], deque()
    final = None
    grip = None
    grip_file = None
    clock_map = None
    clock_after = None
    error = None
    pool = ThreadPoolExecutor(max_workers=3,thread_name_prefix='jpeg-save')
    last_progress, last_disk = time.monotonic(), time.monotonic()
    last_clock_sec = None
    def finish_one():
        row = pending.popleft().result()
        cameras[row['camera_name']].append(row)
    try:
        if gripper_source is not None:
            clock_map=client.clock_sync()
            dump(record/'gripper_clock_mapping.json',clock_map)
            grip=GripperSampler(gripper_source,gripper_hz)
            grip_file=(record/'grippers.jsonl').open('x',buffering=1)
            def save_gripper(sample):
                for motor in sample['motors'].values():
                    for field in motor['fields'].values():
                        start,end=field['pc_query_start_ns'],field['pc_reply_ns']
                        field['controller_timestamp_estimate_ns']=(start+end)//2+clock_map['offset_ns']
                        field['query_roundtrip_ns']=end-start
                        field['clock_mapping_uncertainty_ns']=clock_map['uncertainty_ns']
                        field['timestamp_uncertainty_estimate_ns']=(end-start)//2+clock_map['uncertainty_ns']
                sample['timestamp_ns']=max(m['fields']['position_rad']['controller_timestamp_estimate_ns'] for m in sample['motors'].values())
                line(grip_file,sample)
            grip.sink=save_gripper
        with (record/'joints.jsonl').open('x',buffering=1) as jf, (record/'health.jsonl').open('x',buffering=1) as hf, (record/'clock_offset.jsonl').open('x',buffering=1) as cf:
            with closing(client.stream(seconds=seconds,hz=hz,joint_hz=joint_hz)) as stream:
                for event in stream:
                    if grip is not None and grip.error:raise RuntimeError('Gripper stream failed: '+grip.error)
                    kind = event.metadata['type']
                    if kind == 'start':
                        meta['stream_start'] = event.metadata
                        dump(record/'stream_start.json',event.metadata)
                        if grip is not None:
                            if event.metadata['boot_id']!=clock_map['boot_id']:raise RuntimeError('Controller restarted after clock estimate')
                            grip.start()
                    elif kind == 'image':
                        name = CAMERAS[event.metadata['camera_id']]
                        if counts[name]>(seconds+3)*35: raise RuntimeError('Unexpected camera frame rate')
                        while pending and pending[0].done(): finish_one()
                        if len(pending)>=48: finish_one()
                        pending.append(pool.submit(encode_image,directory,event,counts[name]))
                        counts[name]+=1
                    elif kind == 'joints':
                        joint = event.metadata
                        if len(joint['position_deg'])!=14 or len(joint['joint_names'])!=14:
                            raise ValueError('Expected exactly 14 joint positions')
                        if joints and joint['timestamp_ns']<=joints[-1]['timestamp_ns']:
                            raise ValueError('Joint timestamp regression')
                        line(jf,joint)
                        joints.append(joint)
                        sec = joint['timestamp_ns']//1_000_000_000
                        if sec!=last_clock_sec:
                            line(cf,{'robot_ts_ns':joint['timestamp_ns'],'pc_ts_ns':joint['pc_recv_ns'],
                                     'pc_monotonic_ns':joint['pc_recv_monotonic_ns'],
                                     'kind':'receive_correspondence_includes_transport_delay_not_clock_calibration'})
                            last_clock_sec=sec
                    elif kind == 'health': line(hf,event.metadata)
                    elif kind == 'end':
                        final = event.metadata
                        dump(record/'stream_end.json',final)
                    if time.monotonic()-last_progress>=5:
                        if progress: progress(json.dumps({'episode':str(directory),'images':counts,'joints':len(joints)},ensure_ascii=False))
                        last_progress=time.monotonic()
                    if time.monotonic()-last_disk>=2:
                        if shutil.disk_usage(directory).free<2_000_000_000: raise RuntimeError('Disk reserve reached')
                        last_disk=time.monotonic()
        if grip is not None:grip.stop()
        while pending: finish_one()
        if not final or not final.get('ok'): raise RuntimeError('Stream did not finish successfully')
        if final['frames']!=[len(cameras[CAMERAS[k]]) for k in CAMERAS] or final['joint_samples']!=len(joints):
            raise RuntimeError('Controller/client record count mismatch')
        if grip is not None:
            clock_after=client.clock_sync(samples=3)
            dump(record/'gripper_clock_mapping_after.json',clock_after)
    except Exception as exc:
        error = str(exc)
    finally:
        if grip is not None and grip.ident is not None:
            grip.stop()
            if grip.error:error=error or grip.error
        if grip_file is not None:grip_file.close()
        if grip is not None and clock_after is not None:
            map_gripper_clocks(grip.samples,clock_map,clock_after)
            temporary=record/'grippers.jsonl.tmp'
            with temporary.open('w') as f:
                for sample in grip.samples:line(f,sample)
            temporary.replace(record/'grippers.jsonl')
        # Preserve successful pending writes even if acquisition ended abnormally.
        while pending:
            try: finish_one()
            except Exception as exc: error = error or str(exc)
        pool.shutdown(wait=True)
        for name, rows in cameras.items():
            rows.sort(key=lambda r:r['frame_index'])
            with (directory/'camera'/name/(name+'.txt')).open('x') as index, (record/(name+'_frames.jsonl')).open('x') as log:
                for row in rows:
                    index.write('%d %d\n' % (row['timestamp_ns'],row['frame_index']))
                    line(log,row)
        bundles = match_bundles(joints,cameras)
        if grip is not None:attach_grippers(bundles,grip.samples)
        with (record/'bundles.jsonl').open('x') as f:
            for row in bundles: line(f,row)
        camera_stats = {k:stats(v) for k,v in cameras.items()}
        for name,rows in cameras.items():
            camera_stats[name]['sequence_gaps'] = sum((b['sequence']-a['sequence']-1)%2**32 for a,b in zip(rows,rows[1:]))
        validation = {'error':error,'joints':stats(joints),'cameras':camera_stats,'bundles':stats(bundles),
            'image_joint_abs_delta_ms':percentiles([abs(d) for b in bundles for d in b['cam_delta_ms'].values()]),
            'three_camera_span_ms':percentiles([b['camera_span_ms'] for b in bundles]),
            'per_camera_nearest_joint_abs_delta_ms':percentiles([abs(v['delta_ms']) for b in bundles for v in b['joint_per_camera'].values()]),
            'unmatched_joint_samples':len(joints)-len(bundles),
            'unmatched_images':{k:len(v)-len(bundles) for k,v in cameras.items()},
            'duplicate_image_references':0,'hardware_synchronized':False,
            'alignment_note':'Shared-state read and image completion use one controller clock; exposure/sample latency not calibrated.'}
        if grip is not None:
            validation['grippers']=stats(grip.samples)
            validation['gripper_error']=grip.error
            validation['bundles_with_both_grippers']=sum(len(b['grippers'])==2 for b in bundles)
            validation['gripper_abs_delta_ms']=percentiles([abs(v) for b in bundles for v in b['gripper_delta_ms'].values()])
            validation['gripper_calibration_outside_samples']={s:sum(not g['motors'][s]['within_calibrated_travel'] for g in grip.samples) for s in ('left','right')}
            validation['gripper_clock_mapping']=clock_map
            validation['gripper_clock_mapping_after']=clock_after
            validation['gripper_clock_offset_change_ns']=clock_after['offset_ns']-clock_map['offset_ns'] if clock_after else None
            validation['gripper_clock_drift_ppm']=(clock_after['offset_ns']-clock_map['offset_ns'])/(clock_after['pc_midpoint_ns']-clock_map['pc_midpoint_ns'])*1e6 if clock_after else None
            validation['gripper_timestamp_model']='two_anchor_affine_clock_estimate' if clock_after else 'initial_offset_only'
            validation['gripper_opening_calibration_valid']=all(
                v==0 for v in validation['gripper_calibration_outside_samples'].values()) and bool(grip.samples)
            validation['gripper_production_ready']=False
            validation['gripper_readiness_note']='Experimental stationary reader; CAN ownership/freshness and physical opening calibration still require validation.'
            dump(record/'gripper_read_audit.json',{'requests':getattr(gripper_source,'trace',[]),
                'duplicate_old_register_replies':getattr(gripper_source,'duplicate_replies',None),
                'unexpected_reply':getattr(gripper_source,'unexpected_reply',None),
                'forbidden_motor_operations_sent':False if getattr(gripper_source,'query_only_reader',False) else None})
        enough = len(joints)>=max(1,seconds*joint_hz*.98) and all(len(v)>=max(1,seconds*29) for v in cameras.values())
        span_ok = all(s['span_s']>=max(0,seconds-2) for s in [validation['joints']]+list(camera_stats.values()))
        complete = error is None and final is not None and final.get('ok') and enough and span_ok
        if grip is not None:
            complete=complete and len(grip.samples)>=seconds*gripper_hz*.95 and all(len(b.get('grippers',{}))==2 for b in bundles)
        validation['acquisition_passed'] = bool(complete)
        meta.update(complete=bool(complete),status='complete' if complete else 'incomplete',pc_stop_ns=time.time_ns(),
            frames_recorded=len(bundles),first_bundle_ts_ns=bundles[0]['timestamp_ns'] if bundles else None,
            last_bundle_ts_ns=bundles[-1]['timestamp_ns'] if bundles else None,
            cameras={k:{'frames':len(v),'encoding':'jpeg','width':640,'height':480,'essential':True} for k,v in cameras.items()},
            counters={'joints':len(joints),'bundles':len(bundles),'images':sum(len(v) for v in cameras.values())},
            integrity=validation,data_validate=bool(complete))
        dump(record/'validation.json',validation)
        dump(directory/'meta_info.json',meta)
    if error: raise RuntimeError('Recording incomplete at %s: %s' % (directory,error))
    return directory, validation
