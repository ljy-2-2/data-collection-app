"""Normal camera hotplug may retry; xHCI/robot errors must remain faults."""
from copy import deepcopy
import pytest
from test_tianji_camera_recovery import streaming, audit
import camera_audit
import update_tianji_camera_stream as installer

BASE = b'[ 1.000001] baseline\n'


@pytest.mark.parametrize('port', ['1-2', '1-4'])
def test_clean_camera_hotplug_is_retryable_but_does_not_validate_failed_stream(port):
    before, after = audit(), audit()
    events = [f'usb {port}: USB disconnect, device number 3',
              f'usb {port}: new high-speed USB device number 8 using xhci_hcd',
              f'usb {port}: New USB device found, idVendor=32e4, idProduct=9230',
              f'usb {port}: New USB device strings: Mfr=2, Product=1, SerialNumber=0',
              f'usb {port}: Product: HD USB Camera', f'usb {port}: Manufacturer: HD USB Camera']
    after.update(camera_audit.dmesg_fingerprint(BASE+b''.join(
        ('[ 2.000001] '+event+'\n').encode() for event in events)))
    assert not streaming.unchanged_for_recovery(before, after, 'teleop_observation')
    assert streaming.unchanged_for_recovery(before, after, 'teleop_observation', camera_hotplug=True)
    for key in ('modules', 'mounts', 'robot_pids', 'config_hashes'):
        bad = deepcopy(after); bad[key] = 'changed'
        assert not streaming.unchanged_for_recovery(before, bad, 'teleop_observation', camera_hotplug=True)


@pytest.mark.parametrize('line', [
    'xhci_hcd 0000:00:14.0: Setup ERROR: setup context command for slot 9.',
    "usb 1-2: can't restore configuration #1 (error=-32)",
    'usb 1-2: reset high-speed USB device number 10 using xhci_hcd',
    'usb 1-3: USB disconnect, device number 3',
    'usb 1-2: New USB device found, idVendor=ffff, idProduct=9230',
    'Mot[0][0] process reset ERROR', 'EtherCAT: link down'])
def test_kernel_and_robot_errors_never_become_ordinary_hotplug(line):
    before, after = audit(), audit()
    after.update(camera_audit.dmesg_fingerprint(BASE+('[ 2.000001] '+line+'\n').encode()))
    assert not streaming.unchanged_for_recovery(before, after, 'teleop_observation', camera_hotplug=True)


def test_installer_accepts_only_reviewed_negotiation_failure():
    reason = 'Camera open/start or negotiation failed'
    error = 'Native helper failed (exit 1): '+reason
    status = dict(fault='Stream stopped: '+error, last_stream=dict(error=error,
        native_failure_reason=reason, native_exit=1, native_cleanup_timed_out=False))
    assert installer.reviewed_stream_fault(status)
    for key, value in [('native_exit', -9), ('native_cleanup_timed_out', True),
                       ('error', error+'; Audit changed: dmesg_sha256')]:
        changed=deepcopy(status);changed['last_stream'][key]=value
        assert not installer.reviewed_stream_fault(changed)
