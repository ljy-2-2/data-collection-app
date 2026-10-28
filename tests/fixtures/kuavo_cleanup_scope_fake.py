"""Pure fake ROS: no network, processes, devices or robot discovery."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'teleop_control/vendor/kuavo_runtime/src'))
from teleop.robots.kuavo5_session_worker import (
    _CleanupObserver, _packet_digest, cleanup_failure_checks, cleanup_report_ok)

SHA = 'a' * 64


def observed_case(*, topic='/cmd_vel', nonbaseline=False, publish_error=False, resolved=None):
    def publisher(name, *args, **kwargs):
        def publish(*args, **kwargs):
            if publish_error:
                raise RuntimeError('fake publication failed')
        return NS(resolved_name=resolved or name, publish=publish, unregister=lambda: None)
    ros = NS(Publisher=publisher, Subscriber=lambda *a, **k: NS(unregister=lambda: None),
        ServiceProxy=lambda *a, **k: lambda **fields: NS(
            success=True, result=True, mode=fields.get('control_mode')),
        signal_shutdown=lambda *a: None)
    observer = _CleanupObserver(ros)
    observer.install()
    try:
        if nonbaseline:
            ros.ServiceProxy('/wheel_arm_change_arm_ctrl_mode')(control_mode=2)
        for name, fields in observer.BASELINE.items():
            ros.ServiceProxy(name)(**fields)
        if topic:
            handle = ros.Publisher(topic)
            try:
                handle.publish(object())
            except RuntimeError:
                if not publish_error:
                    raise
            handle.unregister()
        ros.signal_shutdown('fake complete')
        evidence = observer.evidence()
    finally:
        observer.restore()
    packet = dict(event='closed', exit_code=0, transport=dict(
        publish_attempts=0, ros_message_attempts=0, profile_configured=True,
        hold_acknowledged=False, mode='unknown', closed=True, desired_mode=0,
        vendor_fast_profile_active=False, mode_request_inflight=None, mode_request_pending=None),
        base_transport=dict(closed=True, errors=[], physical_stop_verified=False,
                            publish_attempts=1, stop_observed=True))
    receipt = dict(event='worker_cleanup', version=1, bundle_sha256=SHA, core_returned=True,
        core_exit_code=0, closed_sha256=_packet_digest(packet), physical_stop_verified=False,
        evidence=evidence)
    return packet, receipt


def accepted(packet, receipt, **options):
    # Model the worker hashing its actual final packet, never stale fixtures.
    receipt['closed_sha256'] = _packet_digest(packet)
    return cleanup_report_ok(packet, receipt, SHA, base_mode='vendor_vr_x', **options)


class CleanupScopeTests(unittest.TestCase):
    def test_reproduces_legacy_base_only_false_arm_failure(self):
        packet, receipt = observed_case()
        old = copy.deepcopy(receipt)
        del old['evidence']['activity_scope']
        self.assertFalse(accepted(packet, old))
        self.assertEqual(cleanup_failure_checks(packet, old, SHA, base_mode='vendor_vr_x'),
                         ['arm_hold_unconfirmed'])
        self.assertTrue(accepted(packet, receipt))
        self.assertEqual(cleanup_failure_checks(packet, receipt, SHA, base_mode='vendor_vr_x'), [])
        self.assertFalse(receipt['physical_stop_verified'])

    def test_actual_arm_and_unknown_publications_still_require_hold(self):
        for topic in ('/kuavo_arm_traj', '/unrecognized', 'cmd_vel'):
            with self.subTest(topic=topic):
                packet, receipt = observed_case(topic=topic)
                self.assertFalse(accepted(packet, receipt))

    def test_topic_remapping_cannot_hide_arm_publication(self):
        packet, receipt = observed_case(resolved='/kuavo_arm_traj')
        self.assertEqual(receipt['evidence']['activity_scope']['arm_publications'], 1)
        self.assertFalse(accepted(packet, receipt))

    def test_failed_publication_is_still_counted(self):
        packet, receipt = observed_case(topic='/kuavo_arm_traj', publish_error=True)
        self.assertEqual(receipt['evidence']['activity_scope']['arm_publications'], 1)
        self.assertFalse(accepted(packet, receipt))

    def test_mode_entry_requires_hold_even_after_baseline_and_without_publications(self):
        for topic in (None, '/cmd_vel'):
            packet, receipt = observed_case(topic=topic, nonbaseline=True)
            self.assertTrue(receipt['evidence']['baseline_services_confirmed'])
            self.assertFalse(accepted(packet, receipt))
            packet['transport'].update(mode='hold', hold_acknowledged=True)
            self.assertTrue(accepted(packet, receipt))

    def test_malformed_scoped_evidence_fails_closed(self):
        for key, value in [('version', True), ('version', 2), ('base_publications', -1),
                           ('arm_publications', True), ('other_publications', 1),
                           ('nonbaseline_control_calls', 100), ('extra', 0)]:
            with self.subTest(key=key, value=value):
                packet, receipt = observed_case()
                receipt['evidence']['activity_scope'][key] = value
                packet['transport'].update(mode='hold', hold_acknowledged=True)
                self.assertFalse(accepted(packet, receipt))
        for invalid in (None, {}, [], 1):
            packet, receipt = observed_case()
            receipt['evidence']['activity_scope'] = invalid
            self.assertFalse(accepted(packet, receipt))
            self.assertIn('ros_activity_scope_invalid', cleanup_failure_checks(
                packet, receipt, SHA, base_mode='vendor_vr_x'))

    def test_base_exemption_requires_all_independent_cleanup_checks(self):
        changes = [('transport', 'ros_message_attempts', 1),
                   ('transport', 'ros_message_attempts', False),
                   ('transport', 'publish_attempts', 1),
                   ('transport', 'mode', 'external'),
                   ('transport', 'mode_request_inflight', 0),
                   ('transport', 'vendor_fast_profile_active', True),
                   ('base_transport', 'publish_attempts', 2),
                   ('base_transport', 'stop_observed', False),
                   ('base_transport', 'closed', False),
                   ('base_transport', 'errors', ['fake error'])]
        for part, key, value in changes:
            with self.subTest(part=part, key=key):
                packet, receipt = observed_case()
                packet[part][key] = value
                self.assertFalse(accepted(packet, receipt))
        for key in ('ros_shutdown_returned', 'ros_handles_unregistered',
                    'control_calls_completed', 'child_processes_exited',
                    'observation_complete', 'baseline_services_confirmed'):
            packet, receipt = observed_case()
            receipt['evidence'][key] = False
            self.assertFalse(accepted(packet, receipt))

    def test_base_exemption_never_applies_to_arm_only_contract(self):
        packet, receipt = observed_case()
        self.assertFalse(cleanup_report_ok(packet, receipt, SHA))

    def test_gripper_proof_and_receipt_identity_remain_required(self):
        packet, receipt = observed_case()
        self.assertFalse(accepted(packet, receipt, gripper_mode='damiao', gripper_config_sha256=SHA))
        packet, receipt = observed_case()
        receipt['closed_sha256'] = 'b' * 64
        self.assertFalse(cleanup_report_ok(packet, receipt, SHA, base_mode='vendor_vr_x'))


if __name__ == '__main__':
    unittest.main()
