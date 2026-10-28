"""Recording fixtures only: no sockets, ROS, motor or platform connections."""
from copy import deepcopy
import pytest
from teleop_control.kuavo_recording import decode_events
from teleop_control.kuavo_preview import ARM_NAMES

BASE=1_000_000_000
OFFSET=10_000_000_000


def stream():
    rows=[]
    for batch in range(21):
        stamp=BASE+batch*50_000_000
        samples={side:[] for side in ('left','right')}
        actions=[]
        for i in range(5):
            n=batch*5+i
            source=stamp-40_000_000+i*10_000_000
            for side in samples:
                samples[side].append(dict(received_ns=source, sequence=n+1, position_rad=-1.5+n*.001,
                    velocity_rad_s=.1, torque_nm=.01, state=0,
                    recording_target=(dict(position_rad=-1.3,source='owner_acknowledged_target',
                        source_timestamp_ns=source-1_000_000) if side=='left' and n>=50 else None)))
            actions.append(dict(joint_order=list(ARM_NAMES), command_rad=[.2+n*.001]*14,
                publication_sequence_id=n+1, publication_completed_monotonic_ns=source))
        rows.append(dict(kind='packet',pc_monotonic_ns=stamp+OFFSET,packet=dict(event='gripper_trace',
            kind='collection_batch',sequence=batch+1,robot_monotonic_ns=stamp,samples=samples,
            actions=actions,dropped_actions=0,dropped_feedback={'left':0,'right':0},
            motors={side:dict(open_rad=-2.,closed_rad=-1.) for side in samples})))
    return rows


def decode(rows):return decode_events(rows,BASE+OFFSET+1,BASE+OFFSET+1_000_000_000)


def test_every_feedback_and_publication_preserved_not_ui_rate():
    a,g,q=decode(stream())
    assert len(a)==100 and all(len(v)==100 for v in g.values())
    assert not q['errors'] and q['full_publication_stream']
    assert all(v['target_rate_met'] for v in q['grippers'].values())
    assert all(b['timestamp_ns']-a['timestamp_ns']==10_000_000 for a,b in zip(g['left'],g['left'][1:]))
    assert g['right'][-1]['target_reference']['source']=='initial_feedback'
    assert g['right'][-1]['target_rad']==-1.5
    assert g['left'][-1]['target_rad']==-1.3
    assert g['left'][-1]['target_reference']['source']=='owner_acknowledged_target'


def test_static_feedback_does_not_manufacture_arm_commands():
    rows=stream()
    for r in rows:
        r['packet']['actions']=[]
        for side,values in r['packet']['samples'].items():
            for v in values:v['position_rad']=-1.5;v['recording_target']=None
    a,g,q=decode(rows)
    assert a==[] and len(g['left'])==100 and not q['errors']


@pytest.mark.parametrize('damage',['batch','motor','action','overflow','timestamp','tail'])
def test_loss_is_reported_but_only_corruption_is_fatal(damage):
    rows=stream()
    if damage=='batch':rows.pop(10)
    if damage=='motor':rows[10]['packet']['samples']['left'].pop(2)
    if damage=='action':rows[10]['packet']['actions'].pop(2)
    if damage=='overflow':rows[10]['packet']['dropped_actions']=1
    if damage=='timestamp':rows[10]['packet']['samples']['left'][2]['received_ns']=1
    if damage=='tail':rows.pop()
    quality=decode(rows)[2]
    assert bool(quality['errors']) is (damage=='timestamp')
    if damage!='timestamp':assert quality['warnings']


def test_duplicate_measurements_do_not_increase_sample_count():
    rows=stream()
    for r in rows:
        for samples in r['packet']['samples'].values():samples.extend(deepcopy(samples))
    a,g,q=decode(rows)
    assert len(g['left'])==100 and not q['errors']


def test_legacy_snapshot_and_diagnostic_do_not_duplicate_new_stream():
    rows=stream()
    rows.insert(2,dict(pc_monotonic_ns=BASE+OFFSET+50_000_000,packet=dict(event='control_trace',
        joint_order=list(ARM_NAMES),command_rad=[99.]*14)))
    a,g,q=decode(rows)
    assert len(a)==100 and all(r['position_rad'][0]<1 for r in a)


def test_real_low_rate_reported_not_filled_to_100hz():
    rows=stream()
    for r in rows:
        for side in r['packet']['samples']:
            r['packet']['samples'][side]=r['packet']['samples'][side][-1:]
            r['packet']['samples'][side][0]['sequence']=r['packet']['sequence']
    a,g,q=decode(rows)
    assert len(g['left'])==20
    assert q['grippers']['left']['actual_hz']==20
    assert not q['grippers']['left']['target_rate_met']


def test_packet_delivery_jitter_does_not_change_source_intervals():
    rows=stream()
    for i,r in enumerate(rows):r['pc_monotonic_ns']+=(i%3)*1_000_000
    a,g,q=decode(rows)
    assert not q['errors'] and q['grippers']['left']['actual_hz']==100
