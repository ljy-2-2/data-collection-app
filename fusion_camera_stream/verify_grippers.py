"""Read-only saved gripper format/timing audit; not a physical calibration test."""
import argparse
import json
from pathlib import Path
from fusion_camera.episode import stats, percentiles


def verify(directory):
    record=directory/'record'
    load=lambda name:json.loads((record/name).read_text())
    rows=[json.loads(l) for l in (record/'grippers.jsonl').open()]
    bundles=[json.loads(l) for l in (record/'bundles.jsonl').open()]
    joints=[json.loads(l) for l in (record/'joints.jsonl').open()]
    validation=load('validation.json')
    assert validation['acquisition_passed'] and not validation['gripper_error']
    before,after=load('gripper_clock_mapping.json'),load('gripper_clock_mapping_after.json')
    assert before['boot_id']==after['boot_id']
    audit=load('gripper_read_audit.json')
    assert audit['unexpected_reply'] is None
    assert audit['forbidden_motor_operations_sent'] is False
    assert all(r['can_id']==0x7ff and r['function']==0x33 and r['register'] in (0x50,0x51,0x3d,0x3e) for r in audit['requests'])
    assert [r['sequence'] for r in rows]==list(range(len(rows)))
    assert all(a['tick']<b['tick'] for a,b in zip(rows,rows[1:]))
    side_stats={};cycle=[]
    for row in rows:
        fields=[row['motors'][s]['fields'][k] for s in ('left','right') for k in ('position_rad','output_encoder_rad')]
        cycle.append((max(f['pc_reply_ns'] for f in fields)-min(f['pc_query_start_ns'] for f in fields))/1e6)
        for motor in row['motors'].values():
            for name,field in motor['fields'].items():
                assert field['pc_query_start_ns']<=field['pc_reply_ns']
                midpoint=(field['pc_query_start_ns']+field['pc_reply_ns'])//2
                fraction=(midpoint-before['pc_midpoint_ns'])/(after['pc_midpoint_ns']-before['pc_midpoint_ns'])
                expected=midpoint+before['offset_ns']+round(fraction*(after['offset_ns']-before['offset_ns']))
                assert field['controller_timestamp_estimate_ns']==expected
                assert field['value']==(field['raw_value'] if field['valid'] else None)
            if not motor['within_calibrated_travel']:assert motor['closure_fraction'] is None
    for side in ('left','right'):
        stamps=[r['motors'][side]['fields']['position_rad']['controller_timestamp_estimate_ns'] for r in rows]
        assert all(a<b for a,b in zip(stamps,stamps[1:]))
        side_stats[side]=stats([{'timestamp_ns':t} for t in stamps])
    for b in bundles:
        assert set(b['grippers'])=={'left','right'}
        for side,g in b['grippers'].items():
            raw=rows[g['sample_index']]['motors'][side]
            assert all(g[k]==v for k,v in raw.items())
            delta=(raw['fields']['position_rad']['controller_timestamp_estimate_ns']-b['timestamp_ns'])/1e6
            assert b['gripper_delta_ms'][side]==delta and abs(delta)<=100
            assert b['tensors']['state__'+side+'_effector__position']==[raw['fields']['position_rad']['value']]
    inside=[r for r in rows if joints[0]['timestamp_ns']<=r['timestamp_ns']<=joints[-1]['timestamp_ns']]
    return {'saved_format_verified':True,'gripper_samples':len(rows),'per_side':side_stats,
            'within_joint_recording_window':stats(inside),'scheduled_ticks_skipped':sum(b['tick']-a['tick']-1 for a,b in zip(rows,rows[1:])),
            'two_sides_query_span_ms':percentiles(cycle),'all_bundles_have_both_grippers':True,
            'clock_mapping_arithmetic_verified':True,'whitelisted_read_requests':len(audit['requests']),
            'duplicate_old_register_replies':audit['duplicate_old_register_replies'],
            'physical_calibration_valid':validation['gripper_opening_calibration_valid'],
            'feedback_hardware_sampling_time_or_freshness_proven':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('episode',type=Path)
    print(json.dumps(verify(parser.parse_args().episode),indent=2))
