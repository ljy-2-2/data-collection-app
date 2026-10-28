"""Operator-guided, disabled gripper measurements; no motion capability.

An exclusive execution lock and verified current context are supplied by the
entry. This diagnostic deliberately does not require the old angle reference:
that relation is what it measures. It never creates a runtime calibration.
"""
import copy
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import struct
import time

from .kuavo5_gripper_live import OwnerClient, REGISTERS, inspect_motor, capture_calibration_sample
from .kuavo5_gripper_survey_compare import validate_sample, validate_motor, validate_survey


class SurveyReadClient(OwnerClient):
    """Whitelist read requests even if a caller accidentally invokes a writer."""
    def send(self, cid, data, *, deadline_ns=None):
        if not (cid == 0x7ff and isinstance(data, bytes) and len(data) == 8
                and data[:2] == struct.pack('<H', self.mid) and data[4:] == bytes(4)
                and ((data[2] == 0x33 and data[3] in set(REGISTERS) | {0x50, 0x51})
                     or (data[2] == 0xcc and data[3] == 0))):
            raise ValueError('angle survey only permits status/register reads')
        return super().send(cid, data, deadline_ns=deadline_ns)

    def commission(self, *args, **kwargs):
        raise ValueError('angle survey cannot commission motor control')


def save_new(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def checked_values(side, values, motor):
    expected_bus, expected_id = ('canbus0', 8) if side == 'left' else ('canbus1', 1)
    profile = motor['profile']
    if (profile['bus'], profile['motor_id'], profile['master_id']) != (expected_bus, expected_id, 0):
        raise ValueError('formal motor route differs from reviewed survey routes')
    if (values['motor_id'], values['master_id'], values['bitrate_code']) != (expected_id, 0, 4):
        raise ValueError(side + ': motor route/bitrate changed')
    for key in ('hardware_version', 'software_version', 'sub_version'):
        if type(values[key]) is not int or values[key] != motor['readback'][key]:
            raise ValueError(side + ': motor identity changed: ' + key)
    for key in ('pmax', 'vmax', 'tmax', 'imax_a'):
        value = values[key]
        if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
            raise ValueError(side + ': invalid read-back ' + key)
        if not math.isclose(value, profile[key], rel_tol=1e-5, abs_tol=1e-6):
            raise ValueError(side + ': motor limits differ from formal configuration: ' + key)
    if values['mode'] not in (1, 4):
        raise ValueError(side + ': unsupported mode; survey never changes mode')


def collect(context, output_directory, *, baseline=None, read_key=input, say=print,
            client_factory=SurveyReadClient, inspect=inspect_motor,
            sample=capture_calibration_sample, clock=time.monotonic_ns):
    """Caller holds the common lock for the entire call, including finally."""
    if baseline is not None:
        validate_survey(baseline)
        if baseline['context']['boot_id'] == context.context['boot_id']:
            raise ValueError('仍是同一次开机；重新上电前后的对比需要不同 boot_id')
        for key in ('release_id', 'installation_sha256', 'gripper_config_sha256', 'owner_directory'):
            if baseline['context'][key] != context.context[key]:
                raise ValueError('两轮之间正式版本或配置已变；不能作为单一重启对照')
        answer = read_key('确认第一轮后，机器人电脑和左右夹爪电源均已按厂家流程重新上电。属实输入 YES，否则 q：')
        if answer.strip() != 'YES':
            raise ValueError('尚未确认夹爪实际重新上电；未连接 CAN owner')
    output = Path(output_directory)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    document = dict(schema_version=1, kind='disabled_gripper_angle_survey', complete=False,
                    context=copy.deepcopy(context.context), motors={}, motor_control_sent=False,
                    motor_parameters_written=False, compensation_applied=False,
                    physical_target_verified=False)
    document['motor_power_cycle_operator_confirmed'] = baseline is not None
    document['motor_power_cycle_independently_verified'] = False
    save_new(output/'context.json', document)
    clients = {}
    stream = (output/'raw.jsonl').open('x', encoding='utf-8')

    def log(event, **fields):
        stream.write(json.dumps(dict(event=event, monotonic_ns=clock(), **fields),
                                ensure_ascii=False, allow_nan=False)+'\n')
        stream.flush()

    def read_one(side, opening=None):
        result = sample(clients[side], document['motors'][side]['readback'])
        result = dict(result)
        result['feedback'] = asdict(result['feedback'])
        result['sampled_ns'] = clock()
        result['measured_opening_mm'] = opening
        validate_sample(result, document['motors'][side]['readback'])
        return result

    try:
        context.check()
        for side in ('left', 'right'):
            record = context.document['motors'][side]
            # Validate the route before even constructing the socket client.
            profile = record['profile']
            route = ('canbus0', 8, 0) if side == 'left' else ('canbus1', 1, 0)
            if (profile['bus'], profile['motor_id'], profile['master_id']) != route:
                raise ValueError('unexpected formal motor route')
            client = client_factory(context.context['owner_directory'], side,
                lambda event, _side=side, **fields: log(event, side=_side, **fields))
            clients[side] = client
            client.connect()
            if client.pid != context.context['controller_pid']:
                raise ValueError('CAN peer is not the checked current controller')
            values = inspect(client)
            checked_values(side, values, record)
            document['motors'][side] = dict(bus=route[0], motor_id=route[1], master_id=0,
                                           readback=values, points={})
            initial = read_one(side)
            log('survey_initial_disabled', side=side, sample=initial)
        context.check()
        say('左右夹爪均已读回失能。工具只读取，不使能、不改参数。')
        say('轻轻手动摆放空夹爪，不要强扳或顶死止挡。若推不动，请退出。')
        names = {'o': ('open', '正常张开'), 'm': ('middle', '大约半开'), 'c': ('closed', '空爪正常合拢')}
        for side, label in (('left', '左'), ('right', '右')):
            say('\n现在记录'+label+'夹爪：o=张开，m=中间，c=闭合，v=只看当前位置，s=已记位置，w=完成这侧，q=退出。')
            say('每个位置停稳后再按键回车；同一个键可重记，原始记录仍保留。')
            if baseline:
                targets = {point: sorted(s['xout_rad'] for s in samples)[1]
                           for point, samples in baseline['motors'][side]['points'].items()}
                say('上一轮输出轴参考（用于对照，不是发给电机的目标）：'+json.dumps(targets))
                say('可按 v 查看当前值，手动调整到接近上一轮同一位置；程序不会带动夹爪。')
            while True:
                key = read_key(label+'夹爪 [o/m/c/v/s/w/q] > ').strip().lower()
                if key == 'q':
                    return document
                context.check()
                if key == 's':
                    say('已记录：'+', '.join(document['motors'][side]['points']))
                    continue
                if key == 'w':
                    try:
                        validate_motor(side, document['motors'][side])
                    except ValueError as exc:
                        say('这侧尚未完成：'+str(exc)+'；可按对应键重记。')
                        continue
                    break
                if key not in names and key != 'v':
                    say('请输入 o、m、c、v、s、w 或 q。')
                    continue
                # An enabled/faulted motor is a fatal error: never prompt the
                # operator to move it or silently retry a potentially unsafe state.
                clients[side].refresh_status(expected_state=0)
                if key == 'v':
                    current = read_one(side)
                    log('survey_view', side=side, sample=current)
                    say('程序角度 %.6f；输出轴角度 %.6f rad。' %
                        (current['position_rad'], current['xout_rad']))
                    continue
                point, description = names[key]
                width = read_key(description+'；如已量爪间距，填毫米数；无量具直接回车：').strip()
                opening = None
                if width:
                    try:
                        opening = float(width)
                        if not math.isfinite(opening) or not 0 <= opening <= 1000:
                            raise ValueError('width range')
                    except ValueError:
                        say('间距应为 0 到 1000 的毫米数；该位置未记录，请重选。')
                        continue
                context.check()
                samples = []
                for _ in range(3):
                    value = read_one(side, opening)
                    samples.append(value)
                    log('survey_point_sample', side=side, point=point, sample=value)
                context.check()
                document['motors'][side]['points'][point] = samples
                say(description+'已记录三次：程序角度 %.6f，输出轴角度 %.6f rad。' %
                    (samples[-1]['position_rad'], samples[-1]['xout_rad']))
        document['complete'] = True
        try:
            validate_survey(document)
        except BaseException:
            document['complete'] = False
            raise
        context.check()
        save_new(output/'survey.json', document)
        log('survey_completed', survey=str(output/'survey.json'))
        return document
    except BaseException as exc:
        document['complete'] = False
        log('survey_interrupted', error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        # Closing pure-read clients does not set native active/seeded/orderly-stop.
        errors = []
        for side, client in clients.items():
            try:
                client.close()
            except Exception as exc:
                errors.append(side+': '+str(exc))
        log('survey_closed', cleanup_errors=errors, motor_control_sent=False,
            motor_parameters_written=False, can_read_submissions=sum(c.host_writes for c in clients.values()))
        if not (output/'survey.json').exists():
            save_new(output/'partial.json', document)
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
