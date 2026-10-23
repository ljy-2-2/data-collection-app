"""Idle-only camera settings; preserve the SDK owner and the recording boundary."""
import copy
import threading
import time

CAMERAS = {'usb_1', 'usb_2', 'usb_4'}
CAMERA_LABELS = {'usb_1': '主相机', 'usb_2': '右相机', 'usb_4': '左相机'}


class CameraSettings:
    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.RLock()
        self.cache = {}
        self.identities = {}
        self.loaded_at = {}
        self.busy = False
        self.generation = 0

    def cancel(self):
        with self.lock:
            self.generation += 1

    def inventory(self, cameras):
        identities = {c['id'].replace('-', '_'): (c.get('bus'), c.get('address'), c.get('vid'), c.get('pid'))
                      for c in cameras if c.get('available')}
        with self.lock:
            for key in set(self.identities) | set(identities):
                if self.identities.get(key) != identities.get(key):
                    self.cache.pop(key, None)
                    self.loaded_at.pop(key, None)
            self.identities = identities

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.cache), self.busy

    def _idle(self):
        manager = self.manager
        if (manager.collection.armed or manager.collection.active()
                or manager.value.get('state', 'idle') not in ('idle', 'error')):
            raise ValueError('请先停止遥操和采集，再修改或读取硬件相机设置')
        manager.require_other_idle()

    def _checked(self, key, value):
        if (not isinstance(value, dict) or value.get('camera_id') != key.replace('_', '-')
                or not isinstance(value.get('brightness'), dict) or not isinstance(value.get('resolution'), dict)):
            raise RuntimeError('相机返回无效的设置')
        resolution = value['resolution']
        if (not isinstance(resolution.get('options'), list) or not resolution['options']
                or resolution.get('selected') not in resolution['options']):
            raise RuntimeError('相机返回无效的分辨率清单')
        for option in resolution['options']:
            if (not isinstance(option, dict) or not isinstance(option.get('id'), str)
                    or option.get('format') not in ('mjpeg', 'yuyv') or option.get('fps') != 30
                    or any(type(option.get(n)) is not int or option[n] <= 0 for n in ('width', 'height'))):
                raise RuntimeError('相机返回无效的分辨率参数')
        brightness = value['brightness']
        if type(brightness.get('supported')) is not bool:
            raise RuntimeError('相机返回无效的亮度能力')
        if brightness['supported']:
            if (any(type(brightness.get(n)) is not int for n in ('min','max','step','value','default'))
                    or brightness['step'] <= 0 or not brightness['min'] <= brightness['value'] <= brightness['max']
                    or not brightness['min'] <= brightness['default'] <= brightness['max']):
                raise RuntimeError('相机返回无效的亮度范围')
        return {name: copy.deepcopy(value[name]) for name in ('brightness', 'resolution')}

    def get(self, key):
        return self.request(key)

    def observe_stream(self, metadata):
        """Populate controls from the live stream, without stopping it for GETs."""
        snapshots = metadata.get('camera_controls')
        if snapshots is None:
            return
        selected = metadata.get('cameras')
        if not isinstance(snapshots, dict) or not isinstance(selected, list) or set(snapshots) != set(selected):
            raise RuntimeError('相机流返回无效的设置快照')
        checked = {key.replace('-', '_'): self._checked(key.replace('-', '_'), value)
                   for key, value in snapshots.items()}
        with self.lock:
            for key, value in checked.items():
                device = snapshots[key.replace('_', '-')].get('device')
                if device and key in self.identities:
                    if tuple(device.get(n) for n in ('bus','address','vid','pid')) != self.identities[key]:
                        raise RuntimeError('相机设置快照与当前设备身份不一致')
            self.cache.update(checked)
            self.loaded_at.update({key:time.monotonic() for key in checked})

    def request(self, key, *, mode_id=None, brightness=None):
        if key not in CAMERAS:
            raise ValueError('未知天机 Marvin M6相机')
        changing = mode_id is not None or brightness is not None
        with self.lock:
            cached = copy.deepcopy(self.cache.get(key))
        if not changing:
            if cached:
                return cached
            raise RuntimeError('尚未收到当前相机流的设置快照，请等待视频连接；旧控制器服务需要升级')
        changes = {}
        if mode_id is not None:
            changes['mode_id'] = mode_id
        if brightness is not None:
            changes['brightness'] = brightness
        return self.apply({key: changes})[key]

    def _validate(self, key, changes, settings=None):
        if (key not in CAMERAS or not isinstance(changes, dict) or not changes
                or set(changes) - {'mode_id', 'brightness'}):
            raise ValueError('无效的相机设置')
        if 'mode_id' in changes and (not isinstance(changes['mode_id'], str) or not changes['mode_id']):
            raise ValueError('请选择支持的分辨率')
        if 'brightness' in changes and type(changes['brightness']) is not int:
            raise ValueError('亮度必须为整数')
        if settings is None:
            return
        if 'mode_id' in changes:
            resolution = settings['resolution']
            if (not resolution.get('editable')
                    or changes['mode_id'] not in [v['id'] for v in resolution['options']]):
                raise ValueError('请选择支持的分辨率')
        if 'brightness' in changes:
            value, limits = changes['brightness'], settings['brightness']
            if (not limits['supported'] or not limits['min'] <= value <= limits['max']
                    or (value - limits['min']) % limits['step']):
                raise ValueError('亮度超出支持范围或不符合步长')

    @staticmethod
    def _matches(settings, changes):
        return (('mode_id' not in changes or settings['resolution']['selected']['id'] == changes['mode_id'])
                and ('brightness' not in changes or settings['brightness'].get('value') == changes['brightness']))

    def apply(self, changes):
        """One confirmed dialog: stop once, write while idle, then verify once.

        The controller accepts TLS connections serially. Even status queries
        must wait for the existing stream's normal end and USB cooldown.
        """
        if not isinstance(changes, dict) or not changes or len(changes) > len(CAMERAS):
            raise ValueError('请提供相机设置')
        changes = copy.deepcopy(changes)
        for key, fields in changes.items():
            self._validate(key, fields)
        manager = self.manager
        with manager.command_lock:
            self._idle()
            with self.lock:
                generation = self.generation
                cached = copy.deepcopy(self.cache)
            for key, fields in changes.items():
                if key not in manager.camera_inventory:
                    raise ValueError('相机未连接，请先检查设备')
                self._validate(key, fields, cached.get(key))
            if manager.camera_phase == 'fault':
                raise RuntimeError('相机预览存在故障，暂不能应用参数。请先检查设备并恢复真实画面：' + manager.camera_error)
            pending = {key: fields for key, fields in changes.items()
                       if key not in cached or not self._matches(cached[key], fields)}
            if not pending:
                return {key: cached[key] for key in changes}
            with self.lock:
                self.busy = True
            manager.camera_preview_restarting = True
            attempted, previous = [], {}
            paused, preview_started, resume = False, False, True
            stage = '停止相机预览'
            try:
                # __enter__ also sends a status RPC. Never construct/enter a
                # controller connection here until pause_cameras has joined.
                manager.pause_cameras()
                paused = True
                if self.generation != generation:
                    raise RuntimeError('相机设置操作已取消')
                stage = '连接相机设置服务'
                with manager.new_camera_client() as client:
                    status = client.status()
                    if status.get('fault'):
                        resume = False
                        raise RuntimeError('控制器相机服务存在故障，请先检查设备：' + str(status['fault']))
                    if status.get('camera_controls') is not True:
                        raise RuntimeError('控制器相机服务需要升级以支持分辨率和硬件亮度')
                    keys = list(manager.camera_inventory)
                    # Read and validate every target before writing any target.
                    for key in keys:
                        if key not in pending and key in cached:
                            continue
                        stage = CAMERA_LABELS[key] + '读取原参数'
                        value = self._checked(key, client.camera_settings(key.replace('_', '-')))
                        with self.lock:
                            self.cache[key] = value
                        if key in pending:
                            self._validate(key, pending[key], value)
                            previous[key] = copy.deepcopy(value)
                    for key, fields in pending.items():
                        if self.generation != generation:
                            raise RuntimeError('相机设置操作已取消')
                        if self._matches(previous[key], fields):
                            continue
                        stage = CAMERA_LABELS[key] + '写入参数'
                        attempted.append(key)  # A lost reply may follow a committed write.
                        value = self._checked(key, client.camera_settings(key.replace('_', '-'), **fields))
                        if not self._matches(value, fields):
                            raise RuntimeError('硬件读回的参数与请求不一致')
                        with self.lock:
                            self.cache[key] = value
                if self.generation != generation:
                    raise RuntimeError('相机设置操作已取消')
                with self.lock:
                    expected = {key: copy.deepcopy(self.cache[key]) for key in keys}
                stage = '验证相机画面'
                manager.camera_preview_restarting = False
                manager.start_cameras()
                preview_started = True
                self._verify_preview(generation, expected)
                with self.lock:
                    self.loaded_at.update({key: time.monotonic() for key in keys})
                    return {key: copy.deepcopy(self.cache[key]) for key in changes}
            except Exception as error:
                detail = f'{stage}失败：{error}'
                if attempted and self.generation == generation:
                    manager.camera_preview_restarting = True
                    # Roll back the whole confirmed batch. No camera may be
                    # left silently applied after a later camera fails.
                    try:
                        if preview_started:
                            manager.pause_cameras()
                            preview_started = False
                        deferred = self._restore(attempted, previous, pending)
                        if deferred:
                            resume = False
                            detail += '；已恢复原采集配置，硬件故障仍需排除后验证'
                        else:
                            detail += '；已回写本次修改前的参数'
                    except Exception as restore_error:
                        resume = False
                        detail += f'；原设置恢复失败：{restore_error}'
                raise RuntimeError(detail) from error
            finally:
                with self.lock:
                    self.busy = False
                manager.camera_preview_restarting = False
                if (paused and not preview_started and resume and manager.camera_phase != 'fault'
                        and self.generation == generation):
                    manager.start_cameras()

    def _restore(self, attempted, previous, changes):
        deferred, errors = False, []
        with self.manager.new_camera_client() as client:
            fault = client.status().get('fault')
            for key in reversed(attempted):
                restored = False
                # Restore the server's original preference BEFORE another SET.
                # A SET replaces its rollback slot, even when its reply is lost.
                # Restoring that slot afterwards could reapply the failed edit.
                try:
                    result = client.restore_camera_settings(key.replace('_', '-'))
                    restored = result.get('restored_configuration') is True
                except Exception:
                    pass  # A healthy service can still read back an explicit rollback.
                try:
                    if fault:
                        raise RuntimeError('相机服务故障尚未解除')
                    # Brightness-only edits and rollback never change resolution.
                    fields = {}
                    if 'mode_id' in changes[key]:
                        fields['mode_id'] = previous[key]['resolution']['selected']['id']
                    if 'brightness' in changes[key]:
                        fields['brightness'] = previous[key]['brightness']['value']
                    value = self._checked(key, client.camera_settings(key.replace('_', '-'), **fields))
                    if not self._matches(value, fields):
                        raise RuntimeError('原参数读回不一致')
                except Exception as restore_error:
                    if restored:
                        value = previous[key]
                        deferred = True
                    else:
                        errors.append(f'{CAMERA_LABELS[key]}：{restore_error}；配置恢复未确认')
                        continue
                with self.lock:
                    self.cache[key] = copy.deepcopy(value)
        if errors:
            raise RuntimeError('；'.join(errors))
        return deferred

    def _verify_preview(self, generation, expected):
        manager = self.manager
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            with self.lock:
                if self.generation != generation:
                    raise RuntimeError('相机设置操作已取消')
                observed = copy.deepcopy(self.cache)
            with manager.lock:
                ready = bool(expected) and all(
                    key in manager.camera_inventory and key in manager.frames and
                    (manager.camera_frame_metadata.get(key, {}).get('width'),
                     manager.camera_frame_metadata.get(key, {}).get('height')) ==
                    (expected[key]['resolution']['selected']['width'], expected[key]['resolution']['selected']['height'])
                    for key in expected)
                error = manager.camera_error
            if ready:
                for key, value in expected.items():
                    fields = {'mode_id': value['resolution']['selected']['id']}
                    if value['brightness']['supported']:
                        fields['brightness'] = value['brightness']['value']
                    if key not in observed or not self._matches(observed[key], fields):
                        raise RuntimeError(CAMERA_LABELS[key] + '取流读回的参数与请求不一致')
                return
            if error and manager.camera_phase == 'fault':
                raise RuntimeError('所选相机设置未能恢复真实画面：' + error)
            time.sleep(.05)
        raise RuntimeError('等待所选分辨率的真实相机画面超时')
