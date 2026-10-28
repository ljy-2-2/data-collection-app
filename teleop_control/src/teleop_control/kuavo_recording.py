"""Decode measured batches and recorded targets; never import robot controls."""
import math
import json
from .kuavo_preview import ARM_NAMES, measured_grippers

SIDES = ('left', 'right')


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_received_upload(directory, metadata):
    """Check structural integrity only; no rate, duration or missing-source gate."""
    if metadata.get('error') or (metadata.get('recording_integrity') or {}).get('errors'):
        raise ValueError('乐聚记录包含严重数据错误，拒绝上传')
    counts={'joints':metadata.get('joint_samples'),'actions':metadata.get('action_samples'),
            'bundles':metadata.get('frames_recorded')}
    counts.update({s+'_gripper':(metadata.get('gripper_statistics') or {}).get(s,{}).get('samples') for s in SIDES})
    total=0
    for name,expected in counts.items():
        path=directory/'record'/(name+'.jsonl')
        if type(expected) is not int or expected<0 or not path.is_file():
            raise ValueError('乐聚记录文件或计数无效：'+name)
        count=0
        with path.open() as stream:
            for line in stream:
                row=json.loads(line)
                if not isinstance(row,dict):raise ValueError('乐聚记录格式损坏：'+name)
                if name in ('joints','actions'):
                    values=row.get('position_rad')
                    if row.get('joint_names')!=list(ARM_NAMES) or not isinstance(values,list) or len(values)!=14 or not all(map(finite,values)):
                        raise ValueError('乐聚关节名称或数值损坏：'+name)
                elif name.endswith('_gripper') and not finite(row.get('position_rad')):
                    raise ValueError('乐聚夹爪数值损坏：'+name)
                count+=1
        if count!=expected:raise ValueError('乐聚实际记录条数与元数据不一致：'+name)
        if name!='bundles':total+=count
    total+=sum(c.get('frames',0) for c in metadata.get('cameras',{}).values())
    if metadata.get('data_available') is not True or total<=0:
        raise ValueError('乐聚回合没有实际数据，拒绝上传')


def decode_events(events, start_ns, stop_ns):
    """Map robot monotonic stamps via receive anchors, retaining original stamps.

    This is software receive-time alignment, not exposure synchronization. Each
    episode uses one offset (minimum observed transit offset) so packet batching
    does not turn 100 real samples into 20 artificial simultaneous samples.
    """
    batches = [r for r in events if r['packet'].get('event') == 'gripper_trace'
               and r['packet'].get('kind') in ('collection_batch', 'feedback_batch')]
    full = any(r['packet'].get('kind') == 'collection_batch' for r in batches)
    if full:
        batches = [r for r in batches if r['packet'].get('kind') == 'collection_batch']
    anchors = []
    for row in batches:
        p = row['packet']
        stamp = p.get('robot_monotonic_ns')
        if not full:
            stamp = max((s.get('received_ns', 0) for samples in p.get('samples', {}).values()
                         for s in samples), default=0)
        if type(stamp) is int and stamp > 0:
            anchors.append(row['pc_monotonic_ns']-stamp)
    offset = min(anchors) if anchors else None
    errors, warnings = [], []
    actions, grippers = [], {s: [] for s in SIDES}
    references, motors = {}, {}
    seen = {s: {} for s in SIDES}
    previous = {}
    last_batch = None
    drops = None
    action_seen = set()
    action_last = None
    arm_references = {}
    def record_action(p, received, unsampled=False):
        nonlocal action_last
        names, values = p.get('joint_order'), p.get('command_rad')
        if names != list(ARM_NAMES) or not isinstance(values, list) or len(values) != 14 or not all(map(finite, values)):
            errors.append('目标指令名称或数值无效'); return
        source = p.get('publication_completed_monotonic_ns')
        if unsampled and (type(source) is not int or source <= 0):
            errors.append('目标指令缺少原始时间戳'); return
        stamp = source+offset if unsampled and offset is not None else received
        sequence = p.get('publication_sequence_id')
        if unsampled:
            if type(sequence) is not int:
                errors.append('目标指令缺少发布序号'); return
            identity = (sequence, source)
            if identity in action_seen: return
            action_seen.add(identity)
            if action_last is not None and sequence != action_last+1:
                (warnings if sequence>action_last else errors).append('目标指令发布序号不连续')
            action_last = sequence
        if not start_ns <= stamp <= stop_ns: return
        actions.append(dict(timestamp_ns=stamp, source_timestamp_ns=source,
            publication_sequence_id=sequence, joint_names=list(ARM_NAMES), position_rad=values,
            delivery_scope=p.get('delivery_scope', 'publisher_enqueue_only'),
            sampling='every_publication' if unsampled else 'legacy_diagnostic',
            diagnostic_sampling_hz=p.get('diagnostic_sampling_hz'),
            diagnostic_samples_omitted_before=p.get('diagnostic_samples_omitted_before', 0)))

    for row in events:
        p, received = row['packet'], row['pc_monotonic_ns']
        if p.get('event') == 'status':
            motors.update((p.get('grippers') or {}).get('motors') or {})
            # Compatibility only: new firmware always supplies real batches.
            if not batches:
                for side, value in measured_grippers(p).items():
                    source = value['timestamp_ns']
                    if source in seen[side]: continue
                    seen[side][source] = source
                    stamp = received-round(value['age_s']*1e9)
                    target = motors.get(side, {}).get('target_rad')
                    if side not in references:
                        references[side] = dict(position_rad=value['position_rad'], source='initial_feedback', source_timestamp_ns=source)
                    if finite(target):
                        references[side] = dict(position_rad=target, source='last_controller_target', source_timestamp_ns=source)
                    if start_ns <= stamp <= stop_ns:
                        grippers[side].append(dict(value, timestamp_ns=stamp, source_timestamp_ns=source,
                            target_rad=references[side]['position_rad'], target_reference=dict(references[side]),
                            sampling='legacy_status_snapshot'))
        if p.get('event') == 'control_trace' and not full:
            record_action(p, received)
        if p.get('event') != 'gripper_trace' or p.get('kind') not in ('collection_batch', 'feedback_batch'):
            continue
        if full and p.get('kind') != 'collection_batch': continue
        if offset is None:
            errors.append('夹爪批量反馈缺少有效时间锚点'); continue
        motors.update(p.get('motors') or {})
        if full:
            seq = p.get('sequence')
            if type(seq) is not int or (last_batch is not None and seq != last_batch+1):
                (warnings if type(seq) is int and last_batch is not None and seq>last_batch else errors).append('采集批次丢失或序号回退')
            last_batch = seq
            for a in p.get('actions', []): record_action(a, received, True)
            # A held target is recording evidence, never a new publication.
            # This matches Tianji's last accepted command snapshot policy.
            for a in [*(p.get('actions') or []), p.get('latest_action')]:
                if not isinstance(a,dict):continue
                source=a.get('publication_completed_monotonic_ns');values=a.get('command_rad')
                if (type(source) is int and source>0 and a.get('joint_order')==list(ARM_NAMES)
                        and isinstance(values,list) and len(values)==14 and all(map(finite,values))):
                    arm_references[source]=dict(timestamp_ns=source+offset, source_timestamp_ns=source,
                        position_rad=list(values), publication_sequence_id=a.get('publication_sequence_id'),
                        source='last_observed_publication', delivery_scope=a.get('delivery_scope','publisher_enqueue_only'))
        counters = {'actions': p.get('dropped_actions', 0), **(p.get('dropped_feedback') or {})}
        if any(type(v) is not int or v < 0 for v in counters.values()):
            errors.append('采集源丢帧计数无效')
        if drops is not None and counters != drops:
            warnings.append('采集源反馈或目标缓存溢出')
        drops = counters
        for side in SIDES:
            for sample in p.get('samples', {}).get(side, []):
                source, sequence = sample.get('received_ns'), sample.get('sequence')
                position = sample.get('position_rad')
                if type(source) is not int or source <= 0 or type(sequence) is not int or not finite(position):
                    errors.append(side+' 夹爪实测帧无效'); continue
                if sequence in seen[side]:
                    if seen[side][sequence] != source: errors.append(side+' 夹爪序号冲突')
                    continue
                seen[side][sequence] = source
                prev = previous.get(side)
                if prev and (sequence != prev[0]+1 or source <= prev[1]):
                    (warnings if sequence>prev[0] and source>prev[1] else errors).append(side+' 夹爪反馈丢帧或时间回退')
                previous[side] = (sequence, source)
                if side not in references:
                    references[side] = dict(position_rad=position, source='initial_feedback', source_timestamp_ns=source)
                target = sample.get('recording_target') or {}
                if (finite(target.get('position_rad')) and type(target.get('source_timestamp_ns')) is int
                        and 0 < target['source_timestamp_ns'] <= source
                        and target.get('source') == 'owner_acknowledged_target'):
                    references[side] = dict(target)
                elif not full:
                    value = motors.get(side, {}).get('target_rad')
                    if finite(value):
                        references[side] = dict(position_rad=value, source='last_controller_target', source_timestamp_ns=source)
                stamp = source+offset
                if not start_ns <= stamp <= stop_ns: continue
                motor = motors.get(side, {})
                opened, closed = motor.get('open_rad'), motor.get('closed_rad')
                closure = (max(0., min(1., (position-opened)/(closed-opened)))
                           if finite(opened) and finite(closed) and opened != closed else None)
                grippers[side].append(dict(timestamp_ns=stamp, source_timestamp_ns=source,
                    sequence=sequence, position_rad=position, closure_fraction=closure,
                    velocity_rad_s=sample.get('velocity_rad_s'), torque_nm=sample.get('torque_nm'),
                    state=sample.get('state'), target_rad=references[side]['position_rad'],
                    target_reference=dict(references[side]), sampling='real_feedback_batch'))
    covered_until = max((r['packet'].get('robot_monotonic_ns',0)+(offset or 0)
                         for r in batches), default=0) if full else None
    if full and covered_until < stop_ns:
        warnings.append('保存前未收到覆盖结束时刻的采集批次')
    actions.sort(key=lambda r:r['timestamp_ns'])
    stats = {}
    duration = max((stop_ns-start_ns)/1e9, 1e-9)
    for side, rows in grippers.items():
        rows.sort(key=lambda r:r['timestamp_ns'])
        hz = ((len(rows)-1)*1e9/(rows[-1]['source_timestamp_ns']-rows[0]['source_timestamp_ns'])
              if len(rows)>1 and rows[-1]['source_timestamp_ns']>rows[0]['source_timestamp_ns'] else 0.)
        stats[side] = dict(samples=len(rows), actual_hz=hz, samples_per_episode_second=len(rows)/duration,
                           target_hz=100, target_rate_met=hz>=98 and len(rows)/duration>=98)
    return actions, grippers, dict(full_publication_stream=full, errors=list(dict.fromkeys(errors)),warnings=list(dict.fromkeys(warnings)),
        arm_target_references=sorted(arm_references.values(), key=lambda r:r['timestamp_ns']),
        grippers=stats, robot_to_pc_offset_ns=offset, covered_until_ns=covered_until,
        clock_mapping='minimum_receive_anchor_offset' if full else 'legacy_receive_estimate',
        gripper_action_policy='initial_feedback_then_last_target')
