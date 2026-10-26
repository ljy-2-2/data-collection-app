"""The shared orbit input pans G2 without changing any robot joints."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_g2_right_drag_and_reset():
    root=Path(__file__).resolve().parents[1]
    script=textwrap.dedent('''
        from pathlib import Path
        from PySide6.QtCore import QUrl,QPoint,Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine,qmlRegisterType
        from PySide6.QtTest import QTest
        from shiboken6 import delete
        from qt_ui.visuals import G2View
        app=QGuiApplication([])
        qmlRegisterType(G2View,'Collection.Native',1,0,'G2View')
        engine=QQmlApplicationEngine();warnings=[]
        engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
        engine.loadData(b'import QtQuick\\nimport Collection.Native 1.0\\nWindow {width:480;height:420;visible:true;G2View {id:g2;objectName:"g2";anchors.fill:parent} RobotOrbitArea {scene:g2}}',
            QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/g2-pan-test.qml').resolve())))
        assert engine.rootObjects(),warnings
        window=engine.rootObjects()[0];scene=window.findChild(G2View,'g2');QTest.qWait(100)
        baseline=window.grabWindow();pixels=bytes(baseline.constBits())
        before=(scene.yaw,scene.pitch,scene.zoom,dict(scene.values))
        QTest.mousePress(window,Qt.RightButton,Qt.NoModifier,QPoint(200,200))
        QTest.mouseMove(window,QPoint(230,220),20)
        QTest.mouseRelease(window,Qt.RightButton,Qt.NoModifier,QPoint(230,220));QTest.qWait(80)
        assert before==(scene.yaw,scene.pitch,scene.zoom,scene.values)
        assert scene.pan_x==30 and scene.pan_y==20
        panned=window.grabWindow();assert bytes(panned.constBits())!=pixels
        scene.resetView();QTest.qWait(80)
        restored=window.grabWindow();assert bytes(restored.constBits())==pixels
        QTest.mousePress(window,Qt.LeftButton,Qt.NoModifier,QPoint(200,200))
        QTest.mouseMove(window,QPoint(215,210),20)
        QTest.mouseRelease(window,Qt.LeftButton,Qt.NoModifier,QPoint(215,210))
        assert scene.yaw!=before[0] and scene.pitch!=before[1]
        assert scene.pan_x==scene.pan_y==0 and scene.values==before[3]
        window.close();delete(engine)
        assert not warnings,warnings
    ''')
    env={**os.environ,'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software',
         'PYTHONPATH':str(root/'scripts')+os.pathsep+os.environ.get('PYTHONPATH','')}
    result=subprocess.run([sys.executable,'-c',script],cwd=root,env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
