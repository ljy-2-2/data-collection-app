"""Read-only validation of saved files, timestamps, 14-joint schema and audit."""
import argparse
import bisect
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
from PIL import Image

def verify(directory):
    meta=json.loads((directory/'meta_info.json').read_text())
    assert meta['complete']
    final=json.loads((directory/'record/stream_end.json').read_text())
    assert final['ok'] and final['native_exit']==0
    js=[json.loads(l) for l in (directory/'record/joints.jsonl').open()]
    bundles=[json.loads(l) for l in (directory/'record/bundles.jsonl').open()]
    assert len(js)==final['joint_samples']
    assert all(len(j['position_deg'])==len(j['velocity_deg_s'])==len(j['torque_nm'])==14 for j in js)
    assert all(len(set(j['joint_names']))==14 for j in js)
    assert all(a['timestamp_ns']<b['timestamp_ns'] for a,b in zip(js,js[1:]))
    assert len({tuple(j['frame_serial']) for j in js})==len(js)
    indexed={}
    jobs=[]
    for cam,info in meta['cameras'].items():
        rows=[json.loads(l) for l in (directory/'record'/(cam+'_frames.jsonl')).open()]
        index=[tuple(map(int,l.split())) for l in (directory/'camera'/cam/(cam+'.txt')).open()]
        assert len(index)==len(rows)==info['frames']
        assert index==[(r['timestamp_ns'],r['frame_index']) for r in rows]
        assert len({t for t,i in index})==len(index)
        assert all(a[0]<b[0] for a,b in zip(index,index[1:]))
        indexed[cam]={i:t for t,i in index}
        assert len(list((directory/'camera'/cam/'frames').glob('*.jpg')))==len(index)
        assert len(list((directory/'camera'/cam/'native').glob('*.jpg')))==len(index)
        jobs.extend((cam,r) for r in rows)
    def check_image(job):
        cam,row=job
        name='%08d.jpg'%row['frame_index']
        base=directory/'camera'/cam
        assert hashlib.sha256((base/'native'/name).read_bytes()).hexdigest()==row['native_sha256']
        with Image.open(base/'frames'/name) as im:
            assert im.size==(640,480) and im.format=='JPEG'
            im.load()
        return 1
    with ThreadPoolExecutor(max_workers=3) as pool:
        decoded=sum(pool.map(check_image,jobs))
    used={cam:set() for cam in indexed}
    joint_times=[j['timestamp_ns'] for j in js]
    for b in bundles:
        assert len(b['tensors']['state__joint__position'])==14
        assert all(math.isfinite(v) for v in b['tensors']['state__joint__position'])
        original=js[b['joint_sample_index']]
        assert b['timestamp_ns']==original['timestamp_ns']
        assert b['tensors']['state__joint__position']==[math.radians(v) for v in original['position_deg']]
        for cam,i in b['camera_frame_index'].items():
            assert i not in used[cam];used[cam].add(i)
            assert indexed[cam][i]==b['cam_ts'][cam]
            assert abs(b['cam_ts'][cam]-b['timestamp_ns'])<=25_000_000
            if 'joint_per_camera' in b:
                nearest=b['joint_per_camera'][cam]
                sample=js[nearest['joint_sample_index']]
                assert sample['timestamp_ns']==nearest['joint_timestamp_ns']
                delta=b['cam_ts'][cam]-sample['timestamp_ns']
                assert nearest['delta_ms']==delta/1e6
                insertion=bisect.bisect_left(joint_times,b['cam_ts'][cam])
                candidates=[joint_times[i] for i in (insertion-1,insertion) if 0<=i<len(js)]
                assert abs(delta)==min(abs(b['cam_ts'][cam]-t) for t in candidates)
    before,after=final['audit_before'],final['audit_after']
    for key in ['robot_pids','modules','mounts','config_hashes','dmesg_sha256']:
        assert before[key]==after[key],key
    health=[before['xenomai'],after['xenomai']]+[json.loads(l)['xenomai'] for l in (directory/'record/health.jsonl').open()]
    for state in health:
        seen=set()
        for l in state.splitlines():
            values=l.split()
            if values and values[-1] in ('RTPSI','FX_RtTask'):
                assert int(values[2])==0 and int(values[4])==0
                seen.add(values[-1])
        assert len(seen)==2
    return {'verified':True,'scaled_jpegs_fully_decoded':decoded,'native_jpegs_hash_verified':decoded,
            'joint_samples':len(js),'joint_dimensions':14,'bundles':len(bundles),
            'no_duplicate_camera_references':True,'all_indexes_and_bundle_timestamps_consistent':True,
            'robot_audit_unchanged':True,'rt_mode_switch_and_page_fault_counts':0}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('episode',type=Path)
    print(json.dumps(verify(p.parse_args().episode),indent=2))
