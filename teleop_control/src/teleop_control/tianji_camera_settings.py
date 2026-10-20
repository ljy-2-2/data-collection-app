"""Idle-only camera settings; preserve the SDK owner and the recording boundary."""
import copy
import threading
import time

CAMERAS = {'usb_1', 'usb_2', 'usb_4'}


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

    def request(self, key, *, mode_id=None, brightness=None):
        if key not in CAMERAS:
            raise ValueError('未知天机相机')
        changing = mode_id is not None or brightness is not None
        with self.lock:
            cached = copy.deepcopy(self.cache.get(key))
            recent = time.monotonic() - self.loaded_at.get(key, -1e9) < 2
        if not changing and cached and (recent or self.manager.value.get('state') not in ('idle', 'error')):
            return cached
        manager = self.manager
        with manager.command_lock:
            # Two UI controls can ask concurrently. The first query populates
            # every connected camera, so the next request never restarts video.
            with self.lock:
                cached = copy.deepcopy(self.cache.get(key))
                recent = time.monotonic() - self.loaded_at.get(key, -1e9) < 2
                generation = self.generation
            if not changing and cached and recent:
                return cached
            self._idle()
            if key not in manager.camera_inventory:
                raise ValueError('相机未连接，请先检查设备')
            previous = cached
            with self.lock:
                self.busy = True
            manager.camera_preview_restarting = True
            applied = False
            try:
                manager.pause_cameras()
                if self.generation != generation:
                    raise RuntimeError('相机设置操作已取消')
                with manager.new_camera_client() as client:
                    if client.status().get('camera_controls') is not True:
                        raise RuntimeError('控制器相机服务需要升级以支持分辨率和硬件亮度')
                    keys = list(manager.camera_inventory)
                    for camera_key in keys:
                        if changing and camera_key != key and camera_key in self.cache:
                            continue
                        value = client.camera_settings(camera_key.replace('_', '-'))
                        value = self._checked(camera_key, value)
                        with self.lock:
                            self.cache[camera_key] = value
                    if changing:
                        previous = copy.deepcopy(self.cache[key])
                        applied = True  # A lost reply can still follow a committed write.
                        value = client.camera_settings(key.replace('_', '-'), mode_id=mode_id, brightness=brightness)
                        with self.lock:
                            self.cache[key] = self._checked(key, value)
                if self.generation != generation:
                    raise RuntimeError('相机设置操作已取消')
                manager.camera_preview_restarting = False
                manager.start_cameras()
                self._verify_preview(generation)
                with self.lock:
                    for camera_key in keys:
                        self.loaded_at[camera_key] = time.monotonic()
                    return copy.deepcopy(self.cache[key])
            except Exception as error:
                if applied and previous and self.generation == generation:
                    # Restore the previous known settings if real negotiated
                    # frames do not match. Unknown USB faults are never cleared.
                    manager.camera_preview_restarting = True
                    try:
                        manager.pause_cameras()
                        with manager.new_camera_client() as client:
                            value = client.camera_settings(key.replace('_', '-'),
                                mode_id=previous['resolution']['selected']['id'],
                                brightness=previous['brightness'].get('value') if previous['brightness'].get('supported') else None)
                        with self.lock:
                            self.cache[key] = self._checked(key, value)
                    except Exception as restore_error:
                        try:
                            with manager.new_camera_client() as client:
                                restored = client.restore_camera_settings(key.replace('_', '-'))
                            if restored.get('restored_configuration') is not True:
                                raise RuntimeError('配置恢复未确认')
                            with self.lock:
                                self.cache[key] = previous
                        except Exception:
                            raise RuntimeError(f'{error}；原设置恢复失败：{restore_error}') from error
                        raise RuntimeError(f'{error}；已恢复原采集配置，硬件故障仍需排除后验证') from error
                raise
            finally:
                with self.lock:
                    self.busy = False
                manager.camera_preview_restarting = False
                if self.generation == generation:
                    manager.start_cameras()

    def _verify_preview(self, generation):
        manager = self.manager
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            with self.lock:
                if self.generation != generation:
                    raise RuntimeError('相机设置操作已取消')
                expected = copy.deepcopy(self.cache)
            with manager.lock:
                ready = bool(manager.camera_inventory) and all(
                    key in manager.frames and key in expected and
                    (manager.camera_frame_metadata.get(key, {}).get('width'),
                     manager.camera_frame_metadata.get(key, {}).get('height')) ==
                    (expected[key]['resolution']['selected']['width'], expected[key]['resolution']['selected']['height'])
                    for key in manager.camera_inventory)
                error = manager.camera_error
            if ready:
                return
            if error and manager.camera_phase == 'fault':
                raise RuntimeError('所选相机设置未能恢复真实画面：' + error)
            time.sleep(.05)
        raise RuntimeError('等待所选分辨率的真实相机画面超时')
