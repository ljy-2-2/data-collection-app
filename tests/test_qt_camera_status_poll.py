"""Status RPC failures must not remove independent live camera subscriptions."""
import importlib.util
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 unavailable')
class CameraStatusPollTest(unittest.TestCase):
    def test_failed_status_keeps_camera_inventory_but_invalidates_robot_feedback(self):
        from PySide6.QtCore import QCoreApplication
        from qt_ui.bridge import Bridge
        app = QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as settings:
            bridge = Bridge({}, demo=True, settings_path=settings)
            bridge.poll_timer.stop()
            bridge.demo = False
            calls = []
            def request(path, **options):
                calls.append(path)
                options['failed']('synthetic timeout')
            bridge.request = request  # Every request is intercepted; no backend/hardware access.
            cameras = [dict(id=key, connected=True, available=True) for key in ('usb_1','usb_4','usb_2')]
            bridge.update(authenticated=True, robot='tianji', teleopStep=2,
                          tianji=dict(state='running', cameras=cameras, joints={'left':[1]*7}))
            bridge.poll()
            self.assertIn('/tianji/status', calls)
            self.assertEqual(bridge._state['tianji']['cameras'], cameras)
            self.assertEqual(bridge._state['tianji']['state'], 'unknown')
            self.assertTrue(bridge._state['tianji']['control_active'])
            self.assertEqual(bridge._state['tianji']['joints'], {})
            bridge.pending.clear(); bridge.last_poll.clear()
            bridge.update(tianji=dict(state='checking',cameras=cameras,joints={}))
            bridge.poll()
            self.assertFalse(bridge._state['tianji']['control_active'])
            self.assertNotIn('控制已停止',bridge._state['tianji']['error'])
            bridge.pending.clear(); bridge.last_poll.clear()
            bridge.update(robot='agibot-g2', preview=dict(available=True, cameras=cameras,
                                                        joint_positions={'joint':1}))
            bridge.poll()
            self.assertIn('/preview', calls)
            self.assertEqual(bridge._state['preview']['cameras'], cameras)
            self.assertFalse(bridge._state['preview']['available'])
            self.assertEqual(bridge._state['preview']['joint_positions'], {})
            bridge.poll_timer.stop()
            app.processEvents()
