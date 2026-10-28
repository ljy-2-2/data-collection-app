import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'teleop_control/src'))
from teleop_control.camera_devices import zed
from teleop_control.camera_resolution import parse_modes, choose_mode, ros_parameters


def load_config():
    path = ROOT/'umi_collection/vendor/galaxis/tools/web_device_config.py'
    spec = importlib.util.spec_from_file_location('_zed_device_setup',path)
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


LISTING = """
 [0]: 'YUYV'
   Size: Discrete 3840x1080
      Interval: Discrete 0.033s (30.000 fps)
   Size: Discrete 2560x720
      Interval: Discrete 0.033s (30.000 fps)
   Size: Discrete 1344x376
      Interval: Discrete 0.033s (30.000 fps)
   Size: Discrete 4416x1242
      Interval: Discrete 0.067s (15.000 fps)
"""


def test_native_stereo_sizes_are_exposed_as_one_eye_and_default_to_hd():
    modes = zed.mono_modes(parse_modes(LISTING))
    assert [(m['width'],m['height']) for m in modes] == [(672,376),(1280,720),(1920,1080)]
    assert choose_mode(modes)['width'] == 1280
    assert choose_mode(modes,modes[-1])['width'] == 1920
    profile = load_config().build_collection_profile('dual',('left','right','center'),
        {role:ros_parameters(choose_mode(modes)) for role in ('left','right','center')},
        {'center':'zed_mini'})
    assert profile['cameras']['center']['image_width'] == 1280
    assert profile['camera_sources'] == {'center':'zed_mini'}
    with pytest.raises(ValueError):
        load_config().build_collection_profile('dual',('left','right','center'),None,{'center':'zed_mini'})


def test_hid_only_is_actionable_but_never_counted_as_video(tmp_path):
    hid = tmp_path/'1-1';hid.mkdir()
    (hid/'idVendor').write_text('2b03');(hid/'idProduct').write_text('f681')
    assert not zed.is_zed(dict(vendor='2b03',product='f681'))
    assert '视频接口未连接' in zed.connection_issues(tmp_path)[0]
    video = tmp_path/'2-1';video.mkdir()
    (video/'idVendor').write_text('2b03');(video/'idProduct').write_text('f682')
    assert zed.connection_issues(tmp_path) == []
    assert zed.is_zed(dict(vendor='2b03',product='f682'))


def calibration_text():
    return '\n'.join('[LEFT_CAM_'+section+']\nfx=700\nfy=701\ncx=300\ncy=200\n'
        'k1=.1\nk2=.2\np1=.01\np2=.02\nk3=.03\n' for section in zed.SIZES.values())


def test_calibration_download_is_cached_validated_and_never_substituted(tmp_path,monkeypatch):
    import io
    calls=[]
    def download(url,timeout):calls.append(url);return io.BytesIO(calibration_text().encode())
    monkeypatch.setattr(zed.urllib.request,'urlopen',download)
    path = zed.ensure_calibration('123456',tmp_path)
    assert zed.ensure_calibration('123456',tmp_path) == path
    assert len(calls)==1 and 'SN=123456' in calls[0]
    assert zed.calibration(path.read_text(),1280,720)['fy'] == 701
    for serial in ('../123','0','not-a-serial'):
        with pytest.raises(ValueError):zed.ensure_calibration(serial,tmp_path)
    path.write_text(calibration_text().replace('fx=700','fx=nan'))
    with pytest.raises(ValueError):zed.ensure_calibration('123456',tmp_path)
    assert len(calls)==1


def test_zed_source_uses_existing_camera_topics_and_native_dimensions(tmp_path):
    path = ROOT/'umi_collection/vendor/galaxis/galaxis_bringup/tools/build_runtime_configs.py'
    spec = importlib.util.spec_from_file_location('_zed_runtime_config',path)
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    import yaml
    profile = load_config().build_collection_profile('dual',('left','right','center'),
        {r:dict(image_width=1280,image_height=720,framerate=30.,pixel_format='yuyv2rgb')
         for r in ('left','right','center')},{'center':'zed_mini'})
    p=tmp_path/'profile.yaml';p.write_text(yaml.safe_dump(profile))
    actual = module.load_collection_profile(p)
    data={'center_camera/usb_cam':{'ros__parameters':{}},'center_camera/g2_bridge':{},
          'center_camera_flip_node':{'ros__parameters':{}},'encoder_node':{'ros__parameters':{}}}
    result=module.apply_collection_profile('galaxis_devices.yaml',data,actual)
    module.apply_camera_overrides(result,module.load_camera_overrides(p))
    assert 'center_camera/g2_bridge' not in result
    assert result['center_camera/usb_cam']['ros__parameters']['image_width']==2560
    assert result['center_camera_flip_node']['ros__parameters']['input_transport']=='raw'


def test_stereo_crop_layout_rejects_wrong_formats_and_padded_input():
    assert zed.left_image_layout(2560,720,'bgr8',2560*3+16)==(1280,3840)
    assert zed.left_image_layout(3840,1080,'rgb8',3840*3)==(1920,5760)
    assert zed.left_image_layout(1344,376,'mono8',1344)==(672,672)
    for args in [(1280,720,'bgr8',3840),(2561,720,'bgr8',8000),
                 (2560,720,'yuyv',5120),(2560,720,'bgr8',5000)]:
        with pytest.raises(ValueError):zed.left_image_layout(*args)
