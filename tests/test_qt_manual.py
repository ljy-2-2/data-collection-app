"""Manual controls against synthetic joint feedback; no robot or network calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from PySide6.QtCore import QObject, QPointF, QUrl, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow, QQuickItem
from PySide6.QtTest import QTest
from shiboken6 import delete
from qt_ui.bridge import Bridge, LIMITS

APP = QGuiApplication.instance() or QGuiApplication([])


class ManualControlsTest(unittest.TestCase):
    def setUp(self):
        self.config = tempfile.TemporaryDirectory()
        self.bridge = Bridge({}, demo=True, settings_path=self.config.name)
        self.bridge.poll_timer.stop()
        self.bridge.update(authenticated=True, paramSaved=True,
                           tianji=dict(state="idle", joints=dict(left=[0]*7, right=[0]*7)))
        self.calls = []
        self.bridge.action = lambda *args, **kwargs: self.calls.append(args)

    def tearDown(self):
        self.bridge.shutdown()
        self.config.cleanup()

    def test_all_joint_directions_and_limits(self):
        for side in ("left", "right"):
            for joint in range(1, 8):
                sign = -1 if side == "left" and joint in (1, 3, 5, 7) else 1
                for direction in (-1, 1):
                    with self.subTest(side=side, joint=joint, direction=direction):
                        self.bridge.update(tianji=dict(state="idle", joints=dict(left=[0]*7, right=[0]*7)))
                        self.calls.clear()
                        self.bridge.jog(side, joint, direction)
                        self.assertEqual(self.calls[-1][2]["delta"], sign*direction*10)
                        joints = dict(left=[0]*7, right=[0]*7)
                        joints[side][joint-1] = LIMITS[joint-1][1 if sign*direction > 0 else 0]
                        self.bridge.update(tianji=dict(state="idle", joints=joints))
                        self.calls.clear()
                        self.bridge.jog(side, joint, direction)
                        self.assertFalse(self.calls, "Must block motion beyond the actual joint limit")
                        self.bridge.jog(side, joint, -direction)
                        self.assertEqual(len(self.calls), 1, "Motion back inside the limit must remain possible")

    def test_selectable_steps_are_sent_and_invalid_values_rejected(self):
        self.assertEqual(self.bridge.state['tianjiJogStep'],10)
        for amount in (1,5,10):
            self.bridge.setValue('tianjiJogStep',amount)
            for side,sign in [('left',-1),('right',1)]:
                self.bridge.jog(side,1,1)
                self.assertEqual(self.calls[-1][2]['delta'],sign*amount)
        for value in (0,2,11,True,'5'):
            self.bridge.setValue('tianjiJogStep',value)
            self.assertEqual(self.bridge.state['tianjiJogStep'],10)

    def test_zero_survives_custom_pose_save_delete_and_reload(self):
        self.bridge.savePose("工作姿态")
        pose = self.bridge.state["poses"][0]
        self.bridge.restorePose()
        self.assertEqual(self.calls[-1][2], dict(kind="pose", mode="dual", targets=pose["targets"]))
        self.bridge.setValue("selectedPose", "zero")
        original = self.bridge.pose_path().read_bytes()
        self.bridge.deletePose()
        self.assertEqual(self.bridge.pose_path().read_bytes(), original)
        self.bridge.restorePose()
        self.assertEqual(self.calls[-1][2], dict(kind="zero", mode="dual"))
        self.bridge.load_poses()
        self.assertEqual(self.bridge.state["poses"], [pose])
        self.bridge.setValue("selectedPose", pose["id"])
        self.bridge.deletePose()
        self.assertEqual(self.bridge.state["selectedPose"], "zero")
        self.assertEqual(self.bridge.state["poses"], [])
        # A custom file cannot replace the built-in zero target.
        self.bridge.pose_path().write_text(json.dumps([{**pose, "id": "zero"}]))
        self.bridge.load_poses()
        self.assertFalse(self.bridge.state["poses"])

    def test_pose_selector_confirmation_stop_and_responsive_layout(self):
        self.bridge.savePose("工作姿态")
        pose = self.bridge.state["poses"][0]
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("backend", self.bridge)
        engine.rootContext().setContextProperty("systemDark", False)
        warnings = []
        engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
        engine.loadData(b"""
import QtQuick
import QtQuick.Controls
ApplicationWindow {
    id: app; width: 1100; height: 900; visible: true
    Manual { objectName: "manual"; width: parent.width; canMove: true; canRecover: true; live: true
        // Shadow effects need a GPU surface; test the controls offscreen.
        layer.enabled: false
    }
}
""", QUrl.fromLocalFile(str(ROOT / "scripts/qt_ui/qml/ManualTest.qml")))
        self.assertTrue(engine.rootObjects(), warnings)
        window = engine.rootObjects()[0]

        def item(name):
            result = window.findChild(QObject, name)
            if result is None:
                pending = [window.contentItem()]
                while pending:
                    candidate = pending.pop()
                    if candidate.objectName() == name:
                        return candidate
                    pending.extend(candidate.childItems())
            self.assertIsNotNone(result, name)
            return result

        def click(name):
            control = item(name)
            self.assertTrue(control.property("enabled"), name)
            point = control.mapToScene(QPointF(control.width()/2, control.height()/2)).toPoint()
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point)
            QTest.qWait(100)

        try:
            QTest.qWait(150)
            click("restore-selected-pose")
            self.assertFalse(self.calls, "Opening the confirmation must not move the robot")
            click("pose-confirm-accept")
            self.assertEqual(self.calls[-1][2]["kind"], "pose")
            self.bridge.setValue("selectedPose", "zero")
            QTest.qWait(100)
            self.assertFalse(item("delete-selected-pose").property("enabled"))
            click("restore-selected-pose")
            click("pose-confirm-accept")
            self.assertEqual(self.calls[-1][2], dict(kind="zero", mode="dual"))
            click("jog-left0-1")
            self.assertEqual(self.calls[-1][2]["delta"], -10)
            joints = dict(left=[-170, 0, 0, 0, 0, 0, 0], right=[0]*7)
            self.bridge.update(tianji=dict(state="idle", joints=joints))
            QTest.qWait(100)
            self.assertFalse(item("jog-left0-1").property("enabled"))
            self.assertTrue(item("jog-left0--1").property("enabled"))
            # The boundary guard must react when only the step changes.
            self.bridge.update(tianji=dict(state='idle',joints=dict(left=[-168,0,0,0,0,0,0],right=[0]*7)))
            QTest.qWait(30)
            self.assertEqual(item('tianjiManualStep').property('currentIndex'),2)
            self.bridge.setValue('tianjiJogStep',1);QTest.qWait(50)
            self.assertEqual(item('tianjiManualStep').property('currentIndex'),0)
            self.assertTrue(item('jog-left0-1').property('enabled'))
            click('jog-left0-1');self.assertEqual(self.calls[-1][2]['delta'],-1)
            self.bridge.setValue('tianjiJogStep',10);QTest.qWait(50)
            self.assertFalse(item('jog-left0-1').property('enabled'))
            for width in (480, 800, 1100):
                window.resize(width, 900)
                QTest.qWait(100)
                for name in ("pose-selector", "restore-selected-pose", "save-current-pose", "delete-selected-pose"):
                    control = item(name)
                    point = control.mapToScene(QPointF(0, 0))
                    self.assertGreaterEqual(point.x(), 0)
                    self.assertLessEqual(point.x()+control.width(), width)
                if os.environ.get("MANUAL_SCREENSHOTS"):
                    directory = Path(os.environ["MANUAL_SCREENSHOTS"])
                    directory.mkdir(parents=True, exist_ok=True)
                    self.assertTrue(window.grabWindow().save(str(directory/f"manual-{width}.png")))
            self.bridge.setValue("selectedPose", pose["id"])
            QTest.qWait(100)
            click("delete-selected-pose")
            click("pose-confirm-accept")
            self.assertFalse(self.bridge.state["poses"])
            self.assertEqual(self.bridge.state["selectedPose"], "zero")
            self.assertEqual(item("pose-selector").property("currentText"), "零点（默认）")
            self.assertFalse(item("delete-selected-pose").property("enabled"))
            # Stop must bypass an in-flight move request, including a late reply.
            self.bridge.demo = False
            self.bridge.action = Bridge.action.__get__(self.bridge)
            pending = []
            self.bridge.request = lambda path, payload, done, **kw: pending.append((path, done, kw.get("failed")))
            self.bridge.poll = lambda: None
            button = item("restore-selected-pose")
            original_size = (button.width(), button.height())
            original_color = button.property("background").property("color")
            click("restore-selected-pose")
            click("pose-confirm-accept")
            self.assertEqual(button.property("text"), "停止")
            self.assertTrue(button.property("enabled"))
            self.assertTrue(button.property("danger"))
            self.assertNotEqual(button.property("background").property("color"), original_color)
            self.assertEqual((button.width(), button.height()), original_size)
            self.assertIsNone(window.findChild(QObject, "stop-manual-motion"))
            click("restore-selected-pose")
            self.assertEqual(pending[-1][0], "/tianji/stop")
            self.assertEqual(button.property("text"), "复位")
            self.assertFalse(button.property("enabled"))
            pending[0][1]({"state": "moving"})
            self.assertEqual(self.bridge.state["busy"], "stop")
            pending[-1][1]({"state": "idle"})
            QTest.qWait(100)
            self.assertEqual(self.bridge.state["busy"], "")
            self.assertEqual(button.property("text"), "复位")
            self.assertFalse(button.property("danger"))
            self.assertTrue(button.property("enabled"))
            self.assertEqual((button.width(), button.height()), original_size)
            QTest.mouseMove(window, QPointF(0, 0).toPoint())
            QTest.qWait(100)
            self.assertEqual(button.property("background").property("color"), original_color)
            # A successful move acknowledgement must keep Stop visible until
            # the status reports completion; a failed stop must allow retry.
            self.bridge.savePose("测试中途停止")
            click("restore-selected-pose")
            click("pose-confirm-accept")
            pending[-1][1]({"state": "moving"})
            QTest.qWait(100)
            self.assertEqual(button.property("text"), "停止")
            click("restore-selected-pose")
            self.assertEqual(pending[-1][0], "/tianji/stop")
            pending[-1][2]("模拟停止失败")
            QTest.qWait(100)
            self.assertEqual(button.property("text"), "停止")
            self.assertTrue(button.property("enabled"))
            click("restore-selected-pose")
            pending[-1][1]({"state": "idle"})
            QTest.qWait(100)
            self.assertEqual(button.property("text"), "复位")
            self.bridge.update(tianji=dict(state="moving", joints=joints))
            QTest.qWait(100)
            self.assertEqual(button.property("text"), "停止")
            self.bridge.update(tianji=dict(state="idle", joints=joints))
            QTest.qWait(100)
            self.assertEqual(button.property("text"), "复位")
            self.assertEqual((button.width(), button.height()), original_size)
            self.assertFalse(warnings, warnings)
        finally:
            window.close()
            delete(engine)
            APP.processEvents()
