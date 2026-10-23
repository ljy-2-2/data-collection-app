#!/usr/bin/env python3
"""Check the UMI + Tianji board UI using demo state, with no hardware/API access."""
from __future__ import annotations
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "teleop_control/src"))
os.environ.setdefault("QSG_RHI_BACKEND", "opengl")
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
from PySide6.QtTest import QTest
from shiboken6 import delete
from qt_ui.bridge import Bridge
from qt_ui.visuals import CameraItem, CurveItem, Backdrop, G2View, RobotScene


def main():
    from board_graphics import prepare_desktop
    prepare_desktop({})
    from desktop_app import configure_rendering, configure_surface_format
    configure_rendering({})
    configure_surface_format()
    app = QGuiApplication(sys.argv[:1])
    for name, cls in (("CameraItem", CameraItem), ("CurveItem", CurveItem), ("Backdrop", Backdrop), ("G2View", G2View)):
        qmlRegisterType(cls, "Collection.Native", 1, 0, name)
    with tempfile.TemporaryDirectory() as config:
        bridge = Bridge({"APP_COLLECTION_MODE": "umi-tianji"}, demo=True, settings_path=config)
        bridge.poll_timer.stop()
        bridge.poll = lambda: None
        bridge.update(ready=True, authenticated=True, username="Board UI test")
        scene = RobotScene(ROOT)
        engine = QQmlApplicationEngine()
        context = engine.rootContext()
        context.setContextProperty("backend", bridge)
        context.setContextProperty("robotScene", scene)
        context.setContextProperty("systemDark", False)
        engine.load(QUrl.fromLocalFile(str(ROOT / "scripts/qt_ui/qml/Main.qml")))
        assert engine.rootObjects(), "Board QML failed to load"
        window = engine.rootObjects()[0]
        QTest.qWait(300)
        selector = window.findChild(QObject, "robotType")
        assert selector is not None and selector.property("count") == 1
        assert selector.property("currentText") == "天机 Marvin M6"
        assert not bridge.umiOnly and bridge.tianjiOnly
        bridge.configureTeleop("test", "agibot-g2")
        assert bridge.state["teleopStep"] == 1
        bridge.configureTeleop("test", "kuavo-5w")
        assert bridge.state["teleopStep"] == 1
        bridge.navigate("umi")
        QTest.qWait(100)
        assert bridge.state["page"] == "umi"
        bridge.navigate("teleop")
        QTest.qWait(100)
        assert bridge.state["page"] == "teleop"
        window.setVisible(False)
        delete(engine)  # QML must be destroyed before its Python context objects.
        print("PASS: Orange Pi UI keeps UMI and Tianji, excludes unsupported G2, loads QML and CAD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
