#!/usr/bin/env python3
"""Check board robot selection and KUAVO routing with no hardware/API access."""
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
from collection_modes import default_collection_mode


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
        mode = os.environ.get("APP_COLLECTION_MODE", default_collection_mode("arm64"))
        assert mode == "umi-tianji-kuavo", "ARM64 package must enable KUAVO"
        bridge = Bridge({"APP_COLLECTION_MODE": mode}, demo=True, settings_path=config)
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
        assert selector is not None and selector.property("count") == 2
        assert selector.property("currentText") == "天机 Marvin M6"
        assert not bridge.umiOnly and not bridge.tianjiOnly
        assert bridge.robotKeys == ["tianji", "kuavo-5w"]
        bridge.setValue("robot", "agibot-g2")
        assert bridge.state["robot"] == "tianji"
        bridge.configureTeleop("test", "agibot-g2")
        assert bridge.state["teleopStep"] == 1
        selector.setProperty("currentIndex", 1)
        selector.activated.emit(1)
        assert bridge.state["robot"] == "kuavo-5w"
        assert selector.property("currentText") == "乐聚 KUAVO 5-W"
        calls = []
        bridge.action = lambda *args, **kwargs: calls.append((args, kwargs))
        bridge.configureTeleop("test", "kuavo-5w")
        assert bridge.state["teleopStep"] == 2
        assert not bridge.save_timer.isActive(), "KUAVO must not save Tianji parameters"
        QTest.qWait(150)
        assert window.findChild(QObject, "kuavoWorkspace").property("visible")
        assert not window.findChild(QObject, "teleopWorkspace").property("visible")
        bridge.teleop("check")
        assert calls[-1][0][1] == "/kuavo/check"
        if os.environ.get("BOARD_UI_SCREENSHOT"):
            window.grabWindow().save(os.environ["BOARD_UI_SCREENSHOT"])
        bridge.returnTeleop()
        assert bridge.state["teleopStep"] == 1
        bridge.configureTeleop("test", "tianji")
        assert bridge.state["teleopStep"] == 2 and bridge.save_timer.isActive()
        bridge.save_timer.stop()
        bridge.returnTeleop()
        bridge.navigate("umi")
        QTest.qWait(100)
        assert bridge.state["page"] == "umi"
        bridge.navigate("teleop")
        QTest.qWait(100)
        assert bridge.state["page"] == "teleop"
        window.setVisible(False)
        delete(engine)  # QML must be destroyed before its Python context objects.
        bridge.shutdown()
        print("PASS: Orange Pi selector offers Tianji + KUAVO; KUAVO page/model and request routing work; UMI remains available; G2 stays excluded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
