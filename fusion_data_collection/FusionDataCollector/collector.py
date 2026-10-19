"""A/X start, B/Y save episodes. This process has no robot command/CAN interface."""
import argparse
import bisect
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import io
import json
import math
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import sys
import threading
import time
import uuid

SDK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK.parent / 'fusion_camera_stream'))
from fusion_camera.stream import FusionStreamClient
from fusion_camera.client import Frame
from fusion_camera.episode import dump, line, match_bundles, percentiles, stats
from PIL import Image
from .channel_mapping import anatomical_joint_sample

SIDES = ('left', 'right')
CAMERAS = ('usb-1', 'usb-2', 'usb-4')
PORT = 28765
IMAGE_PROFILES = {'mjpeg': (1024, 768), 'yuyv': (640, 480)}
YUYV_FRAME_BYTES = 640*480*2
YUYV_THREE_CAMERA_BYTES_PER_SECOND = YUYV_FRAME_BYTES*30*3
JPEG_AND_STATE_BYTES_PER_SECOND = 15_000_000
ROBOT_STATE_ORDER = [name for side in SIDES
                     for name in [side+'_joint_'+str(i) for i in range(1,8)]+[side+'_gripper_motor']]


def finite_number(value):
    return type(value) in (float, int) and math.isfinite(value)


def record_count_check(start, final, camera_counts, joint_count):
    """Compare disk records to the server total minus the atomic A-boundary baseline."""
    adopted = bool(start and start.get('adopted_preview_stream') is True)
    result = dict(matches=False, scope='episode_after_subscription' if adopted else 'whole_stream',
                  stored=dict(frames=dict(camera_counts), joint_samples=joint_count))
    try:
        if not final or not isinstance(final.get('frames'), list) or len(final['frames']) != len(CAMERAS):
            raise ValueError('Missing server counts')
        total = dict(frames=dict(zip(CAMERAS, final['frames'])), joint_samples=final['joint_samples'])
        baseline = start.get('episode_count_baseline') if adopted else dict(frames=dict.fromkeys(CAMERAS, 0), joint_samples=0)
        if not isinstance(baseline, dict) or set(baseline.get('frames', {})) != set(CAMERAS):
            raise ValueError('Missing subscription count baseline')
        values = list(total['frames'].values())+[total['joint_samples']]
        excluded = [baseline['frames'][k] for k in CAMERAS]+[baseline['joint_samples']]
        if any(type(v) is not int or v < 0 for v in values+excluded):
            raise ValueError('Invalid nonnegative integer count')
        if any(b > a for a, b in zip(values, excluded)):
            raise ValueError('Subscription baseline exceeds server total')
        expected = dict(frames={k:total['frames'][k]-baseline['frames'][k] for k in CAMERAS},
                        joint_samples=total['joint_samples']-baseline['joint_samples'])
        result.update(server_total=total, excluded_preview=baseline, expected=expected)
        result['matches'] = expected == result['stored']
    except (KeyError, TypeError, ValueError) as exc:
        result['error'] = str(exc)
    return result


def storage_reserve_bytes(seconds, retain_native):
    # Before stream start, reserve for the larger supported raw profile.
    # Disk-space monitoring still stops safely if actual JPEG sizes exceed the estimate.
    rate = JPEG_AND_STATE_BYTES_PER_SECOND
    if retain_native:
        rate += YUYV_THREE_CAMERA_BYTES_PER_SECOND
    return seconds*rate+2_000_000_000


def image_preprocessing(start, retain_native):
    native_format = start.get('format', 'mjpeg')  # Historical MJPEG starts omitted format.
    native_size = start.get('native_size', [1024, 768] if native_format == 'mjpeg' else None)
    if native_format not in IMAGE_PROFILES or native_size != list(IMAGE_PROFILES[native_format]):
        raise ValueError('Unsupported camera stream profile: %s %s' % (native_format, native_size))
    result = dict(native_format=native_format, native_size=native_size, stored_format='jpeg',
                stored_size=[640,480], native_retained=bool(retain_native),
                native_jpeg_retained=bool(retain_native and native_format == 'mjpeg'),
                native_extension='.yuyv' if native_format == 'yuyv' else '.jpg',
                method=('YUYV 4:2:2 decoded to RGB on laptop using each frame color matrix and assumed limited range, then JPEG encoded; no resize'
                        if native_format == 'yuyv' else 'MJPEG decoded and resized on laptop (bilinear), then JPEG encoded'))
    if native_format == 'yuyv':
        result.update(camera_color_matching=start.get('camera_color_matching', {}),
                      yuv_matrix_legacy_default='bt601', yuv_range='limited',
                      yuv_range_is_assumption=True,
                      color_metadata_source='Per-frame yuv_matrix and uvc_color_matching; missing matrix uses legacy BT.601')
    return result


def gripper_statistics(rows):
    measurements = [dict(timestamp_ns=row['pc_feedback_received_monotonic_ns']) for row in rows]
    if len(measurements) > 1 and measurements[-1]['timestamp_ns'] == measurements[0]['timestamp_ns']:
        return dict(count=len(measurements), span_s=0, actual_hz=None,
                    interval_ms=percentiles([0]*(len(measurements)-1)))
    return stats(measurements)


def camera_mapping(path):
    mapping = json.loads(Path(path).read_text())
    if set(mapping) != set(CAMERAS) or len(set(mapping.values())) != 3:
        raise ValueError('相机配置须唯一映射 usb-1、usb-2、usb-4')
    if any(not isinstance(v, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', v) for v in mapping.values()):
        raise ValueError('相机名称仅允许小写字母、数字和下划线，且以字母开头')
    return mapping


def readiness(row, now_ns=None, required_sides=SIDES):
    now = (now_ns or time.monotonic_ns()) / 1e9
    if row.get('mode') != 'live':
        return '需要现有实机遥操作反馈；不会把仿真值保存为实测数据'
    if not row.get('vr_connected') or not 0 <= now - row['pc_monotonic_ns']/1e9 <= .3:
        return '遥操作/VR 反馈已过期'
    if not all(any(type(row.get('buttons', {}).get(k)) is bool for k in group)
               for group in (('A','X'), ('B','Y'))):
        return '未收到开始/结束采集按键，请使用支持 A/B/X/Y 按键的 XR SDK'
    gripper_fault = (row.get('grippers') or {}).get('fault')
    if gripper_fault:
        return '夹爪反馈报告故障：' + str(gripper_fault)
    for side in SIDES:
        q = (row.get('joints_deg') or {}).get(side, [])
        if len(q) != 7 or not all(math.isfinite(v) for v in q):
            return side + ' 缺少 7 个关节反馈'
        joint_info = row.get('joint_feedback', {}).get(side, {})
        if joint_info.get('frame', 0) == 0 or joint_info.get('unchanged_s', 999) > .15:
            return side + ' 关节反馈不新鲜'
        if side not in required_sides:
            continue
        motor = ((row.get('grippers') or {}).get('motors') or {}).get(side, {})
        fb = motor.get('feedback') or {}
        if not isinstance(fb.get('position'), (float,int)) or not math.isfinite(fb['position']):
            return side + ' 缺少夹爪实际角度'
        if not 0 <= now-fb.get('received', -1) <= .15:
            return side + ' 夹爪反馈超过 150 ms'
    return None


class Buttons:
    """Counts survive lossy telemetry; startup/reconnect held buttons never start."""
    def __init__(self):
        self.key = None
        self.previous = None

    def accept(self, row):
        key = (row['session'], row.get('vr_session'))
        counts = row['button_press_counts']
        if key != self.key or self.previous is None or not row.get('vr_connected'):
            self.key, self.previous = key, counts.copy()
            return None
        old, self.previous = self.previous, counts.copy()
        if any(counts.get(k, 0) > old.get(k, 0) for k in ('B','Y')):
            return 'stop'  # Either save button wins over simultaneous starts.
        if any(counts.get(k, 0) > old.get(k, 0) for k in ('A','X')):
            return 'start'
        return None


def encode(directory, event, index, name, retain_native):
    m, data = dict(event.metadata), event.data
    native_format = m.get('format', 'mjpeg')
    if native_format not in IMAGE_PROFILES or (m['width'], m['height']) != IMAGE_PROFILES[native_format]:
        raise ValueError('Unexpected camera format or resolution')
    filename = '%08d.jpg' % index
    native_filename = '%08d%s' % (index, '.yuyv' if native_format == 'yuyv' else '.jpg')
    base = directory / 'camera' / name
    if native_format == 'yuyv':
        if len(data) != YUYV_FRAME_BYTES:
            raise ValueError('Invalid YUYV frame size: expected 614400 bytes')
        m.update(yuv_matrix_defaulted='yuv_matrix' not in m,
                 yuv_matrix=m.get('yuv_matrix','bt601'), yuv_range=m.get('yuv_range','limited'),
                 yuv_range_is_assumption=True)
        image = Frame(dict(m, format=native_format), data).to_pil()
    else:
        with Image.open(io.BytesIO(data)) as im:
            if im.format != 'JPEG' or im.size != (1024,768):
                raise ValueError('Invalid JPEG')
            im.load()
            image = im.convert('RGB').resize((640,480),Image.Resampling.BILINEAR)
    try:
        image.save(base/'frames'/filename, format='JPEG', quality=95, subsampling=0)
    finally:
        image.close()
    if retain_native:
        (base/'native'/native_filename).write_bytes(data)
    m.update(format=native_format, native_format=native_format, native_size=list(IMAGE_PROFILES[native_format]),
             native_bytes=len(data), stored_format='jpeg',
             camera_name=name, frame_index=index, stored_width=640, stored_height=480,
             native_sha256=hashlib.sha256(data).hexdigest(),
             native_path=str(Path('camera')/name/'native'/native_filename) if retain_native else None,
             image_path=str(Path('camera')/name/'frames'/filename))
    return m


def attach_feedback(bundles, grippers, before, after):
    """Map PC feedback receive times to controller monotonic; never exposure time."""
    if before['boot_id'] != after['boot_id']:
        raise ValueError('Controller boot changed during recording')
    span = after['pc_midpoint_ns']-before['pc_midpoint_ns']
    if span <= 0:
        raise ValueError('Clock anchors not ordered')
    for side, rows in grippers.items():
        for row in rows:
            stamp = row['pc_feedback_received_monotonic_ns']
            ratio = (stamp-before['pc_midpoint_ns'])/span
            row['timestamp_ns'] = stamp+before['offset_ns']+round(ratio*(after['offset_ns']-before['offset_ns']))
            row['clock_mapping_uncertainty_ns'] = max(before['uncertainty_ns'],after['uncertainty_ns'])
        times = [r['timestamp_ns'] for r in rows]
        for bundle in bundles:
            i = bisect.bisect_left(times,bundle['timestamp_ns'])
            candidates = [j for j in (i-1,i) if 0 <= j < len(times)]
            if not candidates:
                continue
            j = min(candidates,key=lambda j:abs(times[j]-bundle['timestamp_ns']))
            delta = times[j]-bundle['timestamp_ns']
            if abs(delta) > 50_000_000:
                continue
            row = rows[j]
            bundle.setdefault('grippers',{})[side] = row
            bundle.setdefault('gripper_delta_ms',{})[side] = delta/1e6
            prefix = 'state__'+side+'_effector__'
            bundle['tensors'][prefix+'name'] = [side+'_gripper_motor']
            bundle['tensors'][prefix+'position'] = [row['motor_position_rad']]
            bundle['tensors'][prefix+'opening_angle'] = [row['opening_angle_rad']]
    return bundles


def aligned_joint_states(joints, grippers, max_delta_ns=50_000_000, required_sides=SIDES):
    """One row per actual joint sample; nearest gripper reuse is explicitly labelled."""
    times = {side:[row['timestamp_ns'] for row in grippers[side]] for side in SIDES}
    previous = dict.fromkeys(SIDES)
    for index, joint in enumerate(joints):
        stamp = joint['timestamp_ns']
        positions, sources = [], {}
        for side, offset in (('left',0), ('right',7)):
            positions.extend(math.radians(value) for value in joint['position_deg'][offset:offset+7])
            insertion = bisect.bisect_left(times[side], stamp)
            candidates = [i for i in (insertion-1,insertion) if 0 <= i < len(times[side])]
            sample = None
            if candidates:
                nearest = min(candidates, key=lambda i:abs(times[side][i]-stamp))
                if abs(times[side][nearest]-stamp) <= max_delta_ns:
                    sample = grippers[side][nearest]
            if sample is None:
                positions.append(None)
                sources[side] = dict(valid=False, sample_index=None, source_sequence=None,
                                     timestamp_ns=None, pc_feedback_received_monotonic_ns=None,
                                     delta_ms=None, reused_from_previous_joint=False)
                previous[side] = None
                continue
            positions.append(sample['motor_position_rad'])
            sources[side] = dict(valid=True, sample_index=sample['sequence'],
                                source_sequence=sample.get('feedback_sequence'),
                                timestamp_ns=sample['timestamp_ns'],
                                pc_feedback_received_monotonic_ns=sample['pc_feedback_received_monotonic_ns'],
                                delta_ms=(sample['timestamp_ns']-stamp)/1e6,
                                reused_from_previous_joint=previous[side] == sample['sequence'])
            previous[side] = sample['sequence']
        yield dict(sequence=index, timestamp_ns=stamp, joint_sample_index=index,
                   joint_frame_serial=joint.get('frame_serial'),
                   names=ROBOT_STATE_ORDER, position_rad=positions,
                   grippers=sources, valid=all(sources[side]['valid'] for side in required_sides),
                   max_gripper_delta_ms=max_delta_ns/1e6, hardware_synchronized=False,
                   timestamp_kind='actual_joint_sample_with_nearest_real_gripper_feedback')


def attach_actions(rows, bundles, before, after, required_sides):
    if before['boot_id'] != after['boot_id']:
        raise ValueError('Controller boot changed during action alignment')
    span = after['pc_midpoint_ns'] - before['pc_midpoint_ns']
    if span <= 0:
        raise ValueError('Clock anchors not ordered')
    for row in rows:
        stamp = row['pc_monotonic_ns']
        ratio = (stamp - before['pc_midpoint_ns']) / span
        row['timestamp_ns'] = stamp + before['offset_ns'] + round(ratio * (after['offset_ns'] - before['offset_ns']))
        row['joint_names'] = [side+'_joint_'+str(i) for side in required_sides for i in range(1,8)]
        row['position_rad'] = [math.radians(v) for side in required_sides for v in row['joints_deg'][side]]
        row['clock_mapping_uncertainty_ns'] = max(before['uncertainty_ns'], after['uncertainty_ns'])
    times = [r['timestamp_ns'] for r in rows]
    for bundle in bundles:
        stamp = bundle['timestamp_ns']
        insertion = bisect.bisect_right(times, stamp)
        candidates = [i for i in (insertion-1, insertion) if 0 <= i < len(times)]
        if not candidates:
            continue
        # Offline alignment may select either side of the frame timestamp.
        # Ties keep the earlier command; exact duplicates keep the last record.
        index = min(candidates, key=lambda i: abs(times[i] - stamp))
        if abs(times[index] - stamp) > 150_000_000:
            continue
        command = rows[index]
        bundle['action_sample_index'] = index
        bundle['action_timestamp_ns'] = times[index]
        bundle['action_alignment_kind'] = 'nearest_timestamp'
        bundle['action_delta_ms'] = (times[index] - bundle['timestamp_ns']) / 1e6
        bundle['tensors']['action__joint__name'] = command['joint_names']
        bundle['tensors']['action__joint__position'] = command['position_rad']
        for side, target in command.get('gripper_targets_rad', {}).items():
            bundle['tensors']['action__'+side+'_effector__position'] = [target]
        if 'gripper_target_sources' in command:
            bundle['gripper_target_sources'] = command['gripper_target_sources']


class Episode(threading.Thread):
    def __init__(self, args, mapping, trigger, client_factory=None, *, episode_index=1):
        super().__init__(name='episode-recorder',daemon=True)
        self.args, self.mapping, self.trigger = args, mapping, trigger
        self.episode_index = episode_index
        self.required_sides = tuple(getattr(args, 'controlled_sides', SIDES))
        self.action_rows = []
        self.started_monotonic = None
        self.frame_counts = {}
        self.client_factory = client_factory or (lambda: FusionStreamClient(
            args.host, credentials=args.credentials, timeout=5))
        self.stopping = threading.Event()
        self.finished = threading.Event()
        self.telemetry = queue.Queue(maxsize=1024)
        self.stop_reason, self.error, self.directory = None, None, None
        self.stop_pc_ns = None
        self.state = 'starting'
        self.last_telemetry_ns = time.monotonic_ns()
        self.first_sequence = trigger['sequence']
        self.telemetry_gaps = 0
        self.last_sequence = None
        self.control_cycles = []
        self.latest_gripper_stamp = dict.fromkeys(SIDES)
        self.latest_gripper_sequence = dict.fromkeys(SIDES)
        self.recent_gripper_sequences = {side:{} for side in SIDES}
        self.gripper_integrity = {side:dict(
            samples=0, duplicates=0, sequence_gaps=0, sequence_regressions=0,
            timestamp_regressions=0, stale_samples=0, future_samples=0,
            invalid_samples=0, source_queue_dropped=0, publisher_queue_dropped=0,
            publisher_send_queue_dropped=0, unreceived_tail_samples=0)
            for side in SIDES}
        self.gripper_drop_baseline = {}
        self.latest_gripper_snapshot_sequence = dict.fromkeys(SIDES)
        self.first_gripper_snapshot_sequence = dict.fromkeys(SIDES)
        self.submit(trigger)

    def notice(self, message):
        print('[采集 第 %d 轮] %s' % (self.episode_index, message),flush=True)

    def submit(self, row):
        if self.finished.is_set() or self.state == 'finalizing':
            return
        self.last_telemetry_ns = time.monotonic_ns()
        if row['session'] != self.trigger['session']:
            self.stop('teleop_restarted', '遥操作程序重启')
            return
        try:
            self.telemetry.put_nowait(row)
        except queue.Full:
            self.stop('telemetry_overflow','本机反馈队列溢出')

    def stop(self, reason='B/Y', error=None):
        if self.state == 'finalizing' or self.finished.is_set():
            return
        if error:
            self.error = self.error or error
        if not self.stopping.is_set():
            self.stop_reason, self.stop_pc_ns = reason, time.monotonic_ns()
            self.stopping.set()
            self.notice('结束采集（%s），正在保存已有数据…' % (error or reason))

    def _telemetry(self, file, grippers):
        while True:
            try:
                row = self.telemetry.get_nowait()
            except queue.Empty:
                return
            if self.last_sequence is not None:
                if row['sequence'] <= self.last_sequence:
                    self.stop('telemetry_order','遥操作反馈序号回退')
                    continue
                self.telemetry_gaps += max(0,row['sequence']-self.last_sequence-1)
            self.last_sequence = row['sequence']
            self.control_cycles.append({'timestamp_ns':row['pc_monotonic_ns']})
            line(file,row)
            if getattr(self.args, 'record_actions', False):
                command = row.get('joint_command')
                if command:
                    if getattr(self.args, 'gripper_action_policy', '') == 'initial_feedback_then_last_target':
                        references = row.get('gripper_action_reference', {})
                        self.action_rows.append(dict(command,
                            gripper_targets_rad={side:references.get(side, {}).get('position_rad')
                                                 for side in self.required_sides},
                            gripper_target_sources={side:dict(references.get(side, {}))
                                                    for side in self.required_sides}))
                    else:
                        self.action_rows.append(dict(command, gripper_targets_rad={
                            side: ((row.get('grippers') or {}).get('motors', {}).get(side) or {}).get('target_rad')
                            for side in self.required_sides}))
            for side in SIDES:
                motor = ((row.get('grippers') or {}).get('motors') or {}).get(side,{})
                snapshot = motor.get('feedback') or {}
                snapshot_sequence = snapshot.get('sequence')
                if type(snapshot_sequence) is int and snapshot_sequence >= 0:
                    self.latest_gripper_snapshot_sequence[side] = snapshot_sequence
                    if snapshot_sequence > 0 and self.first_gripper_snapshot_sequence[side] is None:
                        self.first_gripper_snapshot_sequence[side] = snapshot_sequence
                self._feedback_drops(row, side)
                # An empty batch means no new measurement. Do not fall back to cache.
                batched = 'gripper_feedback' in row
                samples = row['gripper_feedback'].get(side, ()) if batched else ([snapshot] if snapshot else [])
                for fb in samples:
                    self._gripper_sample(row, side, fb, motor, grippers[side], batched)
                latest = self.latest_gripper_sequence[side]
                observed = self.latest_gripper_snapshot_sequence[side]
                if latest is None and batched:
                    first = self.first_gripper_snapshot_sequence[side]
                    latest = first-1 if first is not None else None
                self.gripper_integrity[side]['unreceived_tail_samples'] = max(0, observed-latest) \
                    if latest is not None and observed is not None else 0

    def _feedback_drops(self, row, side):
        sources = {'source_queue_dropped':(row.get('grippers') or {}).get('feedback_queue_dropped', {}),
                   'publisher_queue_dropped':row.get('gripper_feedback_dropped', {}),
                   'publisher_send_queue_dropped':row.get('gripper_feedback_send_dropped', {})}
        for name, values in sources.items():
            count = values.get(side, 0)
            if type(count) is not int or count < 0:
                raise ValueError('Invalid gripper loss counter: '+name)
            key = (side, name)
            if key not in self.gripper_drop_baseline:
                self.gripper_drop_baseline[key] = count
            delta = count-self.gripper_drop_baseline[key]
            if delta < self.gripper_integrity[side][name]:
                self.stop('gripper_loss_counter_regression', side+' 夹爪丢帧计数回退')
            self.gripper_integrity[side][name] = max(delta, self.gripper_integrity[side][name])

    def _gripper_sample(self, row, side, fb, motor, samples, batched):
        integrity = self.gripper_integrity[side]
        sequence, stamp = fb.get('sequence'), fb.get('received')
        has_sequence = type(sequence) is int and sequence >= 0
        if (batched and not has_sequence) or not finite_number(stamp) or not finite_number(fb.get('position')):
            integrity['invalid_samples'] += 1
            return
        previous_sequence = self.latest_gripper_sequence[side]
        previous_stamp = self.latest_gripper_stamp[side]
        if has_sequence and previous_sequence is not None:
            if self.recent_gripper_sequences[side].get(sequence) == stamp:
                integrity['duplicates'] += 1
                return
            if sequence <= previous_sequence:
                integrity['sequence_regressions'] += 1
                self.stop('gripper_sequence_regression', side+' 夹爪反馈序号回退')
                return
        elif not has_sequence and previous_stamp == stamp:
            integrity['duplicates'] += 1
            return
        if previous_stamp is not None and stamp < previous_stamp:
            integrity['timestamp_regressions'] += 1
            self.stop('gripper_timestamp_regression', side+' 夹爪时间戳回退')
            return
        age_ns = row['pc_monotonic_ns']-round(stamp*1e9)
        if age_ns < 0:
            integrity['future_samples'] += 1
            return
        stale = age_ns > 150_000_000
        if stale:
            integrity['stale_samples'] += 1
        if has_sequence and previous_sequence is not None:
            integrity['sequence_gaps'] += max(0, sequence-previous_sequence-1)
        self.latest_gripper_sequence[side] = sequence if has_sequence else None
        self.latest_gripper_stamp[side] = stamp
        if has_sequence:
            recent = self.recent_gripper_sequences[side]
            recent[sequence] = stamp
            if len(recent) > 1024:
                del recent[next(iter(recent))]
        endpoints = dict(fb.get('calibration_endpoints_rad', motor.get('display_endpoints_rad') or {}))
        opened, closed = endpoints.get('open_rad'), endpoints.get('closed_rad')
        calibrated = finite_number(opened) and finite_number(closed) and abs(opened-closed)>1e-6
        fraction = (fb['position']-closed)/(opened-closed) if calibrated else None
        valid = calibrated and 0 <= fraction <= 1
        samples.append({
            'sequence':len(samples), 'feedback_sequence':sequence if has_sequence else None, 'side':side,
            'motor_position_rad':fb['position'],
            'opening_angle_rad':(fb['position']-closed)*(1 if opened>closed else -1) if valid else None,
            'opening_fraction':fraction if valid else None,
            'opening_calibration_valid':bool(valid), 'calibration_endpoints_rad':endpoints,
            'pc_feedback_received_monotonic_ns':round(stamp*1e9),
            'pc_telemetry_monotonic_ns':row['pc_monotonic_ns'],
            'telemetry_delivery_age_ns':age_ns, 'telemetry_delivery_stale':stale,
            'feedback':fb, 'feedback_source':'batch' if batched else 'legacy_snapshot',
            'timestamp_kind':'teleop_existing_motor_feedback_received',
            'measurement_transport_latency_known':False})
        integrity['samples'] += 1

    def run(self):
        try:
            self.notice('开始准备，正在连接相机与建立数据流…')
            with closing(self.client_factory()) as client:
                if getattr(self.args, 'adopt_camera_stream', False):
                    client.validate_stream()
                else:
                    check_service(client, recover=getattr(self.args, 'recover_camera', True), notice=self.notice)
                if self.stopping.is_set():
                    raise RuntimeError(self.error or '开始录制前已收到结束指令，未采集数据')
                self._record(client)
        except Exception as exc:
            self.error = self.error or str(exc)
            if self.directory is not None:
                try:
                    p = self.directory/'meta_info.json'
                    meta = json.loads(p.read_text())
                    meta.update(complete=False,status='incomplete',error=self.error)
                    dump(p,meta)
                except Exception:
                    pass
        finally:
            self.state = 'finalizing'
            if self.error and self.directory is None:
                self.notice('未开始录制：'+self.error)
            else:
                self.notice('保存失败／数据不完整：'+self.error if self.error else '保存成功')
            self.notice('保存目录：'+str(self.directory) if self.directory else '未创建保存目录')
            self.notice('本轮已结束；松开按键后按 A/X 开始第 %d 轮。' % (self.episode_index+1))
            self.state = 'incomplete' if self.error else 'saved'
            self.finished.set()

    def _record(self, client):
        root = Path(self.args.output).expanduser().resolve()
        root.mkdir(parents=True,exist_ok=True)
        reserved_bytes = storage_reserve_bytes(self.args.max_seconds, self.args.retain_native)
        if shutil.disk_usage(root).free < reserved_bytes:
            rate_mb = (reserved_bytes-2_000_000_000)/self.args.max_seconds/1e6
            raise RuntimeError('磁盘空间不足：为最长本轮预留每秒 %.3f MB，另留 2 GB%s' % (
                rate_mb, '（含三路原始 YUYV 55.296 MB/s）' if self.args.retain_native else ''))
        name = time.strftime('%Y%m%d_%H%M%S')+'_fusion_'+uuid.uuid4().hex[:8]
        self.directory = root/name
        self.directory.mkdir()
        self.notice('保存目录：'+str(self.directory))
        record = self.directory/'record'
        record.mkdir()
        cameras = {name:[] for name in self.mapping.values()}
        counts = dict.fromkeys(cameras,0)
        for name in cameras:
            for sub in (('frames','native') if self.args.retain_native else ('frames',)):
                (self.directory/'camera'/name/sub).mkdir(parents=True)
        meta = {
            'schema_version':2, 'episode_id':self.directory.name,
            'episode_index':self.episode_index,
            'task':self.args.task, 'robot_type':getattr(self.args,'robot_type','fusion_dual_arm'),
            'controlled_sides':list(self.required_sides), 'parameters':getattr(self.args,'parameters',{}),
            'controller_arm_channels':getattr(self.args, 'controller_arm_channels', None),
            'collector':'fusion_teleop_passive_collection', 'complete':False,'status':'starting',
            'pc_start_ns':time.time_ns(),'start_trigger':self.trigger,
            'requested_image_hz':30,'requested_joint_hz':100,
            'native_image_format':None, 'native_image_size':None,
            'storage_reserve_bytes':reserved_bytes,
            'requested_gripper_hz':(self.trigger.get('grippers') or {}).get('control_timing', {}).get('requested_hz',100),
            'gripper_source':'existing teleop motor feedback; no additional SDK or CAN requests',
            'joint_order':[s+'_joint_'+str(i) for s in SIDES for i in range(1,8)],
            'aligned_state_order':ROBOT_STATE_ORDER,
            'aligned_state_file':'record/aligned_joints.jsonl',
            'aligned_state_note':'One row per real joint sample; nearest real gripper samples may be reused. Source times, sequences, deltas and missing values are explicit; no hardware synchronization.',
            'position_unit':'rad','velocity_unit':'rad/s','effort_unit':'Nm',
            'actions_recorded':False,'timestamp_is_exposure_time':False,
            'gripper_action_policy':getattr(self.args, 'gripper_action_policy', 'command_only'),
            'hardware_synchronized':False,
            'b_also_emergency_stop':self.trigger.get('b_also_emergency_stop',True),
            'emergency_buttons':self.trigger.get('emergency_buttons',['B','Y']),
            'collection_start_buttons':self.trigger.get('collection_start_buttons',['A']),
            'collection_stop_buttons':self.trigger.get('collection_stop_buttons',['B']),
            'camera_mapping':self.mapping, 'max_seconds':self.args.max_seconds,
            'opening_angle_note':'Motor angle relative to configured closed endpoint; not finger gap or independent linkage measurement.',
        }
        dump(self.directory/'meta_info.json',meta)
        preprocessing = None
        joints, pending = [], deque()
        grippers = {side:[] for side in SIDES}
        before = after = final = stream_start = None
        pool = ThreadPoolExecutor(max_workers=3,thread_name_prefix='image-writer')
        def finish_one():
            row = pending.popleft().result(timeout=15)
            cameras[row['camera_name']].append(row)
            callback = getattr(self.args, 'on_frame', None)
            if callback:
                callback(row['camera_name'], (self.directory/row['image_path']).read_bytes())
        last_disk = last_progress = time.monotonic()
        try:
            before = client.clock_sync(samples=3)
            dump(record/'clock_before.json',before)
            if self.stopping.is_set():
                raise RuntimeError(self.error or '开始录制前已收到结束指令，未采集数据')
            with (record/'teleop_feedback.jsonl').open('x') as tf, (record/'joints.jsonl').open('x') as jf, (record/'health.jsonl').open('x') as hf:
                with closing(client.stream(seconds=self.args.max_seconds,hz=30,joint_hz=100,mode='teleop_observation')) as stream:
                    for event in stream:
                        m, kind = event.metadata, event.metadata['type']
                        self._telemetry(tf,grippers)
                        if time.monotonic_ns()-self.last_telemetry_ns > 300_000_000:
                            self.stop('teleop_timeout','遥操作反馈中断超过 300 ms')
                        if self.stopping.is_set():
                            client.request_stop()
                        if kind == 'start':
                            stream_start = m
                            if m.get('observation_mode') != 'teleop_observation' or not m.get('graceful_stop'):
                                raise RuntimeError('控制器仍是静止测试接口，需要先更新独立相机服务到 v2.2')
                            if m['boot_id'] != before['boot_id']:
                                raise RuntimeError('Controller restarted during startup')
                            preprocessing = image_preprocessing(m,self.args.retain_native)
                            meta.update(native_image_format=preprocessing['native_format'],
                                        native_image_size=preprocessing['native_size'])
                            dump(record/'image_preprocessing.json',preprocessing)
                            dump(self.directory/'meta_info.json',meta)
                            dump(record/'stream_start.json',m)
                            self.notice('数据流已连接，等待三路相机首帧和关节反馈…')
                        elif kind == 'joints':
                            m = anatomical_joint_sample(m, getattr(self.args, 'controller_arm_channels', None))
                            if len(m['position_deg']) != 14 or m['joint_names'] != meta['joint_order']:
                                raise RuntimeError('Unexpected joint layout')
                            if not all(math.isfinite(v) for v in m['position_deg']+m['velocity_deg_s']+m['torque_nm']):
                                raise RuntimeError('Nonfinite joint sample')
                            if joints and m['timestamp_ns'] <= joints[-1]['timestamp_ns']:
                                raise RuntimeError('Joint timestamp regression')
                            line(jf,m); joints.append(m)
                        elif kind == 'image':
                            if (preprocessing is None or
                                    m.get('format','mjpeg') != preprocessing['native_format'] or
                                    [m.get('width'),m.get('height')] != preprocessing['native_size']):
                                raise RuntimeError('Camera image differs from negotiated stream profile')
                            cname = self.mapping[m['camera_id']]
                            while pending and pending[0].done():
                                finish_one()
                            if len(pending) >= 48:
                                raise RuntimeError('图像写入跟不上，停止采集以保留已收数据')
                            pending.append(pool.submit(encode,self.directory,event,counts[cname],cname,self.args.retain_native))
                            counts[cname] += 1
                            self.frame_counts = dict(counts)
                        elif kind == 'health':
                            line(hf,m)
                        elif kind == 'end':
                            final = m; dump(record/'stream_end.json',m)
                            if not self.stopping.is_set():
                                self.notice('结束采集（%s），正在保存已有数据…' %
                                            (m.get('error') if not m.get('ok') else
                                             '达到本轮时长上限' if m.get('stop_reason') == 'duration_limit'
                                             else '数据流结束'))
                        if (self.state != 'recording' and not self.stopping.is_set()
                                and final is None and joints and all(counts.values())):
                            self.state = 'recording'
                            self.started_monotonic = time.monotonic()
                            self.notice('三路相机和关节反馈已就绪，正在录制；按 B/Y 结束并保存。')
                        if time.monotonic()-last_disk >= 2:
                            if shutil.disk_usage(self.directory).free < 2_000_000_000:
                                raise RuntimeError('Disk reserve reached')
                            tf.flush(); jf.flush(); hf.flush()
                            last_disk = time.monotonic()
                        if time.monotonic()-last_progress >= 5:
                            if not self.stopping.is_set() and final is None:
                                self.notice('采集中：图像 '+json.dumps(counts,ensure_ascii=False)+
                                            '；关节样本 '+str(len(joints)))
                            last_progress = time.monotonic()
                self._telemetry(tf,grippers)
            if not final or not final.get('ok'):
                raise RuntimeError((final or {}).get('error') or '未收到正常结束与资源清理确认')
            after = client.clock_sync(samples=3)
            dump(record/'clock_after.json',after)
            # Real stream end and the final clock RPC are complete. Preview may
            # resume during disk finalization; it must not publish stored frames.
            released = getattr(self.args, 'on_stream_released', None)
            if released:
                released()
        except Exception as exc:
            self.error = self.error or str(exc)
        finally:
            while pending:
                try:
                    finish_one()
                except Exception as exc:
                    self.error = self.error or str(exc)
            pool.shutdown(wait=True)
        self.state = 'finalizing'
        for cname, rows in cameras.items():
            rows.sort(key=lambda r:r['frame_index'])
            with (self.directory/'camera'/cname/(cname+'.txt')).open('x') as index, (record/(cname+'_frames.jsonl')).open('x') as f:
                for row in rows:
                    index.write('%d %d\n' % (row['timestamp_ns'],row['frame_index']))
                    line(f,row)
        bundles = match_bundles(joints,cameras)
        if before and after:
            attach_feedback(bundles,grippers,before,after)
        else:
            self.error = self.error or '缺少完整时钟映射；夹爪保留原始 PC 接收时间'
        alignment = dict(rows=0, fully_matched=0, missing={side:0 for side in SIDES},
                         reused={side:0 for side in SIDES},
                         max_abs_delta_ms={side:None for side in SIDES}, hardware_synchronized=False)
        mapped_grippers = grippers if before and after else {side:[] for side in SIDES}
        with (record/'aligned_joints.jsonl').open('x') as af:
            for aligned in aligned_joint_states(joints, mapped_grippers, required_sides=self.required_sides):
                line(af, aligned)
                alignment['rows'] += 1
                alignment['fully_matched'] += int(aligned['valid'])
                for side, source in aligned['grippers'].items():
                    alignment['missing'][side] += int(not source['valid'])
                    alignment['reused'][side] += int(source['reused_from_previous_joint'])
                    if source['valid']:
                        alignment['max_abs_delta_ms'][side] = max(
                            alignment['max_abs_delta_ms'][side] or 0, abs(source['delta_ms']))
        for side, rows in grippers.items():
            with (record/(side+'_gripper.jsonl')).open('x') as f:
                for row in rows:
                    line(f,row)
        if getattr(self.args, 'record_actions', False):
            if before and after:
                attach_actions(self.action_rows, bundles, before, after, self.required_sides)
            if not self.action_rows:
                self.error = self.error or '未收到实际下发的遥操作关节指令'
            if any('action__joint__position' not in bundle['tensors'] for bundle in bundles):
                self.error = self.error or '部分图像组合缺少新鲜的实际控制指令，已保留原始记录'
            if meta['gripper_action_policy'] == 'initial_feedback_then_last_target' and any(
                    not finite_number(bundle['tensors'].get('action__'+side+'_effector__position', [None])[0])
                    for bundle in bundles for side in self.required_sides):
                self.error = self.error or '夹爪记录目标尚未从有效反馈初始化，本轮数据不完整'
            with (record/'actions.jsonl').open('x') as af:
                for action in self.action_rows:
                    line(af, action)
            meta['actions_recorded'] = bool(self.action_rows)
            meta['action_alignment'] = {'kind':'nearest_timestamp', 'max_abs_delta_ms':150,
                                        'tie_break':'earlier', 'delta_sign':'action_minus_bundle'}
            meta['action_note'] = ('Actual SDK-accepted joint targets. Gripper recording references initialize from fresh measured positions, '
                'then retain the latest controller targets across episodes in the same teleop session; sources and target availability are explicit. '
                'Receive clocks are software aligned.' if meta['gripper_action_policy'] == 'initial_feedback_then_last_target' else
                'Actual SDK-accepted joint targets, not measured joint states. Missing gripper targets remain null; receive clocks are software aligned.')
        with (record/'bundles.jsonl').open('x') as f:
            for row in bundles:
                for side, offset in (('left',0),('right',7)):
                    row['tensors']['state__'+side+'_arm__position'] = row['tensors']['state__joint__position'][offset:offset+7]
                line(f,row)
        camera_stats = {k:stats(v) for k,v in cameras.items()}
        for name, rows in cameras.items():
            camera_stats[name]['sequence_gaps'] = sum((b['sequence']-a['sequence']-1)%2**32 for a,b in zip(rows,rows[1:]))
        count_comparison = record_count_check(stream_start, final,
            {k:len(cameras[self.mapping[k]]) for k in CAMERAS}, len(joints))
        count_ok = count_comparison['matches']
        both = sum(all(side in b.get('grippers',{}) for side in self.required_sides) for b in bundles)
        if not bundles or both != len(bundles):
            self.error = self.error or '部分三路图像组合缺少 50 ms 内的所选夹爪反馈，详见 validation.json'
        if not count_ok:
            self.error = self.error or '服务器和落盘数量不一致'
        validation = {
            'error':self.error,'joints':stats(joints),'cameras':camera_stats,'bundles':stats(bundles),
            'grippers':{side:gripper_statistics(rows) for side,rows in grippers.items()},
            'gripper_feedback_integrity':self.gripper_integrity,
            'teleop_loop':stats(self.control_cycles),
            'aligned_state':alignment,
            'both_grippers_bundle_count':both,'telemetry_missing_packets':self.telemetry_gaps,
            'record_count_matches_server':count_ok,
            'count_comparison':count_comparison,
            'camera_span_ms':percentiles([b['camera_span_ms'] for b in bundles]),
            'image_joint_abs_delta_ms':percentiles([abs(v['delta_ms']) for b in bundles for v in b['joint_per_camera'].values()]),
            'gripper_joint_abs_delta_ms':percentiles([abs(v) for b in bundles for v in b.get('gripper_delta_ms',{}).values()]),
            'unmatched_images':{k:len(v)-len(bundles) for k,v in cameras.items()},
            'opening_calibration_valid':{side:bool(rows) and all(r['opening_calibration_valid'] for r in rows) for side,rows in grippers.items()},
            'timestamp_note':'Image timestamps use the semantics declared by stream_start.timestamp_kind and share the controller clock with DCSS read times. Gripper PC receive times use two-anchor affine mapping. Sensor/exposure/transport latency uncalibrated.',
        }
        validation['target_rates_met'] = bool(
            (validation['joints']['actual_hz'] or 0) >= 98
            and all((r['actual_hz'] or 0) >= 29 for r in camera_stats.values())
            and all((r['actual_hz'] or 0) >= .98*meta['requested_gripper_hz'] for r in (validation['grippers'][side] for side in self.required_sides)))
        feedback_quality = all(not any(count for name,count in integrity.items()
                               if name not in ('samples','duplicates'))
                               for integrity in (self.gripper_integrity[side] for side in self.required_sides))
        validation['quality_passed'] = bool(not self.error and validation['target_rates_met']
            and not self.telemetry_gaps and all(r['sequence_gaps'] == 0 for r in camera_stats.values())
            and feedback_quality and alignment['fully_matched'] == len(joints)
            and all(validation['opening_calibration_valid'][side] for side in self.required_sides))
        meta.update(complete=self.error is None,status='complete' if self.error is None else 'incomplete', error=self.error,
            pc_stop_ns=time.time_ns(),stop_reason=self.stop_reason or (
                'stream_error' if not final or not final.get('ok') else final.get('stop_reason','duration_limit')),
            stop_requested_pc_monotonic_ns=self.stop_pc_ns,frames_recorded=len(bundles),
            first_bundle_ts_ns=bundles[0]['timestamp_ns'] if bundles else None,
            last_bundle_ts_ns=bundles[-1]['timestamp_ns'] if bundles else None,
            cameras={k:{'frames':len(v),'encoding':'jpeg','width':640,'height':480,
                        'native_format':meta['native_image_format'],'native_size':meta['native_image_size']}
                     for k,v in cameras.items()},
            data_validate=validation['quality_passed'],integrity=validation)
        dump(record/'validation.json',validation)
        dump(self.directory/'meta_info.json',meta)
        self.notice('本轮数据：三路图像组合 %d 组；关节 %d 条；左/右夹爪 %d/%d 条。' %
                    (len(bundles),len(joints),len(grippers['left']),len(grippers['right'])))
        def measured_hz(info):
            return '样本或时间跨度不足' if info['actual_hz'] is None else '%.2f Hz'%info['actual_hz']
        self.notice('实测遥操主循环：%s；以下接收帧率不代表控制指令频率。' %
                    measured_hz(validation['teleop_loop']))
        self.notice('实测反馈接收帧率：关节 %s；左夹爪 %s；右夹爪 %s（夹爪控制目标 %g Hz）。' % (
            measured_hz(validation['joints']), measured_hz(validation['grippers']['left']),
            measured_hz(validation['grippers']['right']), meta['requested_gripper_hz']))
        if not validation['quality_passed']:
            self.notice('质量检查未全通过（频率/丢帧/开合标定），请查看 record/validation.json。')


def check_service(client, recover=False, notice=None):
    status = client.status()
    if recover and status.get('fault') and status.get('idle') and status.get('recoverable_camera_fault') is True:
        if notice: notice('检查并恢复上一轮相机故障…')
        client.recover_camera()
        status = client.status()
        if not status.get('fault') and notice: notice('相机已恢复，继续开始本轮采集。')
    if 'teleop_observation' not in status.get('observation_modes',[]) or not status.get('graceful_stop'):
        raise RuntimeError('独立相机服务需要 v2.2（当前 %s）；请看 README 的服务更新说明，旧静止接口不能用于运动采集。' % status.get('version','unknown'))
    if status.get('fault'):
        previous = status.get('last_stream') or {}
        detail = str(status['fault'])
        if previous.get('native_exit') is not None:
            detail += '；原生采集退出码：'+str(previous['native_exit'])
        reason = previous.get('native_failure_reason')
        if reason and reason not in detail:
            detail += '；'+reason
        raise RuntimeError('相机服务故障尚未恢复：'+detail)
    if not status.get('idle'):
        raise RuntimeError('相机服务正被另一轮采集占用，请等待其结束')
    cameras = client.list_cameras()
    if set(c['id'] for c in cameras) != set(CAMERAS) or any(not c['available'] for c in cameras):
        raise RuntimeError('三路相机未全部就绪')
    return status


def main(argv=None):
    p = argparse.ArgumentParser(description='A/X 开始、B/Y 保存一轮：双臂各 7 关节 + 1 夹爪角度 + 三路相机；不发送机器人命令')
    p.add_argument('--output',metavar='DIR',default=str(Path.home()/'桌面'/'1'),
                   help='保存根目录（支持绝对路径、相对路径和 ~）；每轮自动建立独立子目录，默认 ~/桌面/1')
    p.add_argument('--host',default='192.168.1.190')
    p.add_argument('--credentials',default=str(SDK/'FusionCameraSDK'/'client.json'))
    p.add_argument('--camera-map',default=str(Path(__file__).with_name('camera_map.json')))
    p.add_argument('--max-seconds',type=int,default=600)
    p.add_argument('--task',default='双臂遥操作数据采集')
    p.add_argument('--retain-native',action='store_true',help='另存原始 YUYV (.yuyv) 或 MJPEG (.jpg)；默认只存 640x480 JPEG')
    p.add_argument('--check',action='store_true',help='只检查相机服务和本地依赖，不启动采集')
    args = p.parse_args(argv)
    if not 1 <= args.max_seconds <= 600:
        p.error('--max-seconds 必须为 1..600')
    args.output = str(Path(args.output).expanduser().resolve())
    print('数据保存根目录：'+args.output,flush=True)
    mapping = camera_mapping(args.camera_map)
    try:
        with closing(FusionStreamClient(args.host,credentials=args.credentials,timeout=5)) as client:
            status = check_service(client)
    except (OSError,RuntimeError) as exc:
        if args.check:
            raise
        print('[采集未就绪] '+str(exc)+'。当前没有录制；修复相机服务后按 A/X 重试，无需重启遥操。',flush=True)
    else:
        print('相机服务 '+status['version']+' 就绪；映射 '+json.dumps(mapping,ensure_ascii=False),flush=True)
    if args.check:
        return 0
    shutdown = threading.Event()
    def stop(*_):
        shutdown.set()
    signal.signal(signal.SIGINT,stop); signal.signal(signal.SIGTERM,stop)
    episode = None
    episode_count = 0
    buttons = Buttons()
    last_seen = time.monotonic()
    telemetry_waiting = True
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1',PORT))  # Exclusive: a second collector cannot steal feedback.
        sock.settimeout(.1)
        print('等待遥操作 --collection-telemetry。先松开 A/B/X/Y，按 A/X 开始，B/Y 保存后可继续下一轮；四个按键均不影响遥操。',flush=True)
        try:
            while not shutdown.is_set():
                row = None
                try:
                    data, peer = sock.recvfrom(65536)
                    row = json.loads(data)
                    if peer[0] != '127.0.0.1' or row.get('schema') != 'fusion_collection_telemetry_v1':
                        continue
                    if not 0 <= time.monotonic_ns()-row['pc_monotonic_ns'] <= 300_000_000:
                        continue
                    last_seen = time.monotonic()
                    if telemetry_waiting:
                        print('[采集] 已收到遥操作反馈；按 A/X 检查相机并开始第 %d 轮。' % (episode_count+1),flush=True)
                        telemetry_waiting = False
                    action = buttons.accept(row)
                    active = episode is not None and not episode.finished.is_set()
                    if active:
                        episode.submit(row)
                        if action == 'start' and episode.stopping.is_set():
                            print('[采集 第 %d 轮] 仍在保存；请等“本轮已结束”后松开并重新按 A/X。' % episode.episode_index,flush=True)
                        if action == 'stop':
                            episode.stop('B/Y')
                        if not episode.stopping.is_set():
                            reason = readiness(row)
                            if reason:
                                episode.stop('feedback_not_ready',reason)
                    elif action == 'start':
                        reason = readiness(row)
                        if reason:
                            print('[采集 第 %d 轮] 未开始：%s' % (episode_count+1,reason),flush=True)
                        else:
                            episode_count += 1
                            episode = Episode(args,mapping,row,episode_index=episode_count)
                            episode.start()
                except socket.timeout:
                    pass
                except (ValueError,KeyError,TypeError) as exc:
                    if episode and not episode.finished.is_set():
                        episode.stop('invalid_telemetry',str(exc))
                if time.monotonic()-last_seen > .3 and episode and not episode.finished.is_set():
                    episode.stop('teleop_timeout','遥操作反馈中断超过 300 ms')
                if time.monotonic()-last_seen > 3 and not telemetry_waiting:
                    print('[采集] 遥操作反馈已中断，等待恢复。',flush=True)
                    telemetry_waiting = True
        finally:
            if episode and not episode.finished.is_set():
                episode.stop('process_exit','用户退出/进程停止，本轮标为不完整')
                episode.join(timeout=25)
                if episode.is_alive():
                    print('写入尚未结束，请检查 meta_info.json；不能视为成功采集。',flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError,RuntimeError,ValueError) as exc:
        print('采集未启动：'+str(exc),file=sys.stderr)
        raise SystemExit(1)
