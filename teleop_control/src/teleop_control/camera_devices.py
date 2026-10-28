"""Use the same camera identity rules as the bundled ROS acquisition process."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[3] / 'umi_collection/vendor/galaxis/galaxis_bringup/galaxis_bringup/camera_devices.py'
_spec = importlib.util.spec_from_file_location('_umi_camera_devices', _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
discover = _module.discover
identity = _module.identity
match = _module.match
read_selection = _module.read_selection
resolve_roles = _module.resolve_roles

_zed_path = _path.with_name('zed_camera.py')
_zed_spec = importlib.util.spec_from_file_location('_umi_zed_camera', _zed_path)
zed = importlib.util.module_from_spec(_zed_spec)
_zed_spec.loader.exec_module(zed)
