"""A camera gap may reconnect a preview, but never validate or resume an episode."""
from types import SimpleNamespace
from unittest.mock import Mock
import time
import threading
import pytest
from teleop_control.tianji_preview_recovery import recover_preview_gap,inventory_changed


def setup_manager():
    return SimpleNamespace(collection=SimpleNamespace(armed=False,active=lambda:False),
        camera_stop=Mock(is_set=lambda:False,wait=Mock(return_value=False)),
        lock=threading.RLock(),frames={},camera_cooldown=2.,last_access=time.monotonic(),camera_phase='fault',
        camera_inventory={'usb_2':'usb-2','usb_4':'usb-4'},_diagnostic_event=Mock())


@pytest.mark.parametrize('fault', ['clean','hard','busy','no_capability','recording','armed','attached','cancelled','stale','budget','manual'])
def test_only_clean_passive_preview_gap_recovers(fault):
    m=setup_manager();session=SimpleNamespace(attached=fault=='attached')
    c=Mock();c.list_cameras.return_value=[dict(id='usb-2',available=True)];bad=dict(idle=True,fault='camera gap',camera_hotplug_recovery=True,recoverable_camera_fault=True)
    if fault=='hard':bad['recoverable_camera_fault']=False
    if fault=='manual':bad['camera_recovery_requires_manual']=True
    if fault=='busy':bad['idle']=False
    if fault=='no_capability':bad.pop('camera_hotplug_recovery')
    if fault=='recording':m.collection.active=lambda:True
    if fault=='armed':m.collection.armed=True
    if fault=='cancelled':m.camera_stop.is_set=lambda:True
    if fault=='stale':m.last_access-=10
    c.status.side_effect=[bad,dict(idle=True,fault=None)]
    assert recover_preview_gap(m,c,session,3 if fault=='budget' else 0) is (fault in ('clean','armed'))
    if fault in ('clean','armed'):c.recover_camera.assert_called_once();m._diagnostic_event.assert_called_once()
    else:c.recover_camera.assert_not_called()


def test_close_interrupts_reconnect_backoff():
    m=setup_manager();m.camera_stop.wait.return_value=True;c=Mock()
    assert not recover_preview_gap(m,c,None,0)
    c.status.assert_not_called()


@pytest.mark.parametrize('during_wait', [False, True])
def test_settings_transaction_owns_failure_handling_without_automatic_recovery(during_wait):
    m = setup_manager()
    m.camera_settings = SimpleNamespace(busy=not during_wait)
    if during_wait:
        def wait(_):
            m.camera_settings.busy = True
            return False
        m.camera_stop.wait.side_effect = wait
    c = Mock()
    assert not recover_preview_gap(m, c, None, 0)
    c.status.assert_not_called()
    c.recover_camera.assert_not_called()


@pytest.mark.parametrize('attached',[False,True])
def test_inventory_events_restart_only_unattached_preview(attached):
    m=setup_manager();session=SimpleNamespace(attached=attached)
    assert not inventory_changed(m,session,dict(type='health'))
    assert not inventory_changed(m,session,dict(cameras=[dict(id=s,available=True) for s in ('usb-2','usb-4')]))
    assert inventory_changed(m,session,dict(cameras=[dict(id='usb-2',available=True)])) is (not attached)
    assert inventory_changed(m,session,dict(cameras=[dict(id=s,available=True) for s in ('usb-1','usb-2','usb-4')])) is (not attached)


def test_all_cameras_removed_waits_for_reenumeration_without_capturing():
    m=setup_manager();m.frames={'usb_2':(b'old',0)};c=Mock()
    c.status.side_effect=[dict(idle=True,fault='gap',camera_hotplug_recovery=True,recoverable_camera_fault=True),dict(idle=True,fault=None)]
    c.list_cameras.side_effect=[[],[],[dict(id='usb-4',available=True)]]
    assert recover_preview_gap(m,c,None,0)
    assert m.camera_inventory=={'usb_4':'usb-4'} and not m.frames
    assert c.list_cameras.call_count==3
    c.stream.assert_not_called();c.recover_camera.assert_called_once()
