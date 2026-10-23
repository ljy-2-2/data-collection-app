"""Local KUAVO settings. Saving never connects to or changes the robot."""
from __future__ import annotations
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from .runtime_paths import APP_ROOT

BASELINE = APP_ROOT / 'teleop_control/assets/kuavo/gripper_config.json'
FIELDS = ('open_rad', 'closed_rad', 'speed_rad_s', 'accel_rad_s2')


class GripperValues(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    open_rad: float = Field(gt=-3.121592653589793, lt=3.121592653589793, allow_inf_nan=False)
    closed_rad: float = Field(gt=-3.121592653589793, lt=3.121592653589793, allow_inf_nan=False)
    speed_rad_s: float = Field(ge=.01, le=15, allow_inf_nan=False)
    accel_rad_s2: float = Field(gt=0, le=200, allow_inf_nan=False)


class GripperSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    left: GripperValues
    right: GripperValues


class ConnectionSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    host: str = Field(min_length=1, max_length=64)
    port: int = Field(ge=1, le=65535)
    certificate_sha256: str = Field(max_length=128)
    token: SecretStr


def digest(document):
    # Same encoding as the original immutable service profile.
    return hashlib.sha256(json.dumps(document, sort_keys=True, allow_nan=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


class LocalSettings:
    def __init__(self, root):
        self.directory = Path(root)/'teleop_control/runtime/kuavo'
        self.gripper_path = self.directory/'gripper_config.json'
        self.connection_path = self.directory/'connection.json'
        self.baseline = json.loads(BASELINE.read_text())
        self.document = copy.deepcopy(self.baseline)
        self.error = ''
        if self.gripper_path.exists():
            try:
                candidate = json.loads(self.gripper_path.read_text())
                # Only our four editable fields and their provenance may differ.
                values = {s:{k:candidate['motors'][s]['profile'][k] for k in FIELDS} for s in ('left','right')}
                rebuilt = self.build(GripperSettings.model_validate(values).model_dump())
                if candidate != rebuilt: raise ValueError('配置中含有不可编辑字段的变化')
                self.document = candidate
            except (ValueError, KeyError, TypeError):
                self.error = '本地夹爪配置无法读取；请重新保存参数后再开始遥操'

    def build(self, values):
        document = copy.deepcopy(self.baseline)
        for side in ('left','right'):
            motor = document['motors'][side]
            motor['profile'].update(values[side])
            for endpoint in ('open','closed'):
                field = endpoint+'_rad'
                if values[side][field] != self.baseline['motors'][side]['profile'][field]:
                    motor[endpoint+'_endpoint_source'] = 'operator_adjusted'
            p = motor['profile']
            measured = motor['calibration']['marks']
            travel = p['closed_rad']-p['open_rad']
            minimum = max(motor['calibration']['minimum_travel_rad'],
                          2*p['margin_rad']+p['closed_target_reserve_rad']+p['open_target_reserve_rad'])
            if (travel*(measured['closed']['position_rad']-measured['open']['position_rad']) <= 0
                    or abs(travel) <= minimum):
                raise ValueError(('左' if side=='left' else '右')+'夹爪：张开、闭合须保持原方向并留足行程')
        return document

    def save_grippers(self, document):
        atomic_json(self.gripper_path, document)
        self.document, self.error = document, ''

    def expected_gripper_hash(self):
        if self.error: raise ValueError(self.error)
        return digest(self.document) if self.gripper_path.exists() else None

    def snapshot(self, service):
        expected = digest(self.document)
        observed = ((service or {}).get('service') or {}).get('profile', {}).get('gripper_config_sha256')
        verified = bool((service or {}).get('management_ready') and observed == expected)
        saved = self.gripper_path.exists()
        message = (self.error or ('最近检查：机器人服务配置与此参数一致' if verified else
                   '已保存到本机，待机器人端应用；当前服务不支持在线修改' if saved else
                   '乐聚原项目参数，尚未核验机器人当前配置'))
        return dict(values={s:{k:self.document['motors'][s]['profile'][k] for k in FIELDS} for s in ('left','right')},
                    saved=saved, matches_service=verified, error=self.error, message=message,
                    sha256=expected, file=str(self.gripper_path) if saved else '')

    def connection_snapshot(self):
        result = dict(configured=False, host='192.168.26.1', port=7443, certificate_sha256='')
        try:
            value = json.loads(self.connection_path.read_text())
            result.update(configured=True, host=value['network_host'], port=value['network_port'],
                          certificate_sha256=value['network_cert_sha256'])
        except (OSError, ValueError, KeyError, TypeError): pass
        return result

    def save_connection(self, settings):
        try: host = str(ipaddress.IPv4Address(settings.host.strip()))
        except ValueError: raise ValueError('请输入机器人服务的 IPv4 地址') from None
        if ipaddress.IPv4Address(host).is_unspecified or ipaddress.IPv4Address(host).is_multicast:
            raise ValueError('请输入明确的机器人服务地址')
        certificate = settings.certificate_sha256.strip().lower()
        if not re.fullmatch('[0-9a-f]{64}', certificate): raise ValueError('证书指纹需要 64 位十六进制字符')
        token = settings.token.get_secret_value().strip()
        if not re.fullmatch('[0-9a-fA-F]{64}', token): raise ValueError('连接密钥需要 64 位十六进制字符')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # New token first, then atomically switch the config. An interrupted save
        # leaves the old connection usable. Never echo or return credentials.
        token_path = self.directory/('token-'+uuid.uuid4().hex)
        with open(token_path, 'x', opener=lambda p,f:os.open(p,f,0o600)) as stream:
            stream.write(token+'\n'); stream.flush(); os.fsync(stream.fileno())
        config = dict(network_host=host, network_port=settings.port, network_cert_sha256=certificate,
                      network_token_file=token_path.name, connection_policy='compatible_service_v1',
                      required_contract=dict(protocol='kuavo_robot_session_v1',
                          capabilities=['arms_v63_0p7_elbow','damiao_trigger_v1','return_yb_v1']))
        try: atomic_json(self.connection_path, config)
        except Exception:
            token_path.unlink(); raise
