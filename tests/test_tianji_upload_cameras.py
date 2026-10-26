"""Camera projection uses recorded data, never changes local files or legacy policy."""
import pytest
from teleop_control.tianji_export import select_upload_cameras, camera_file_selected, project_cameras


def recording(root,name,frames=1):
    camera=root/'camera'/name
    (camera/'frames').mkdir(parents=True)
    (camera/(name+'.txt')).write_text('0 100\n' if frames else '')
    if frames:(camera/'frames'/'0.jpg').write_bytes(b'recorded-image')
    return {'frames':frames}


@pytest.mark.parametrize('side',['left','right'])
@pytest.mark.parametrize('head',['absent','empty','recorded'])
def test_single_arm_camera_data_selection(tmp_path,side,head):
    cameras={name:recording(tmp_path,name) for name in ('left_color','right_color')}
    if head!='absent':cameras['head_color']=recording(tmp_path,'head_color',int(head=='recorded'))
    metadata={'upload_camera_policy':'selected_wrist_optional_head_v1','cameras':cameras}
    selected=select_upload_cameras(metadata,tmp_path,(side,))
    assert set(selected)=={side+'_color'}|({'head_color'} if head=='recorded' else set())
    opposite='left' if side=='right' else 'right'
    assert not camera_file_selected('camera/'+opposite+'_color/frames/0.jpg',selected)
    assert not camera_file_selected('record/'+opposite+'_color_frames.jsonl',selected)
    assert camera_file_selected('camera/'+side+'_color/frames/0.jpg',selected)
    assert (tmp_path/'camera'/(opposite+'_color')/'frames/0.jpg').read_bytes()==b'recorded-image'
    projected=project_cameras({'camera_mapping':{'usb-1':'head_color','usb-2':'right_color','usb-4':'left_color'},'camera_frame_index':{'head_color':3,'right_color':4,'left_color':5}},selected)
    assert set(projected['camera_mapping'].values())==set(selected)
    assert set(projected['camera_frame_index'])==set(selected)


def test_missing_wrist_rejects_and_old_episodes_keep_all_cameras(tmp_path):
    metadata={'upload_camera_policy':'selected_wrist_optional_head_v1','cameras':{'head_color':recording(tmp_path,'head_color')}}
    with pytest.raises(ValueError,match='腕部相机'):
        select_upload_cameras(metadata,tmp_path,('right',))
    metadata.pop('upload_camera_policy')
    assert select_upload_cameras(metadata,tmp_path,('right',)) is None
    assert camera_file_selected('camera/left_color/frames/0.jpg',None)
