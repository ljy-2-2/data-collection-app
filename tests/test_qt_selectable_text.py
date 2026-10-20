"""Mouse selection and clipboard feedback, using the real Qt text/control types."""
import os
from pathlib import Path
import subprocess
import sys


def test_readonly_labels_copy_unicode_and_do_not_intercept_control_clicks():
    source = r'''
        from pathlib import Path
        from PySide6.QtCore import QObject,QUrl,QPoint,Qt,QMetaObject
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtTest import QTest
        app=QGuiApplication([])
        from qt_ui.bridge import Bridge
        from tempfile import TemporaryDirectory
        settings=TemporaryDirectory()
        backend=Bridge({},demo=True,settings_path=settings.name);backend.poll_timer.stop()
        engine=QQmlApplicationEngine()
        engine.rootContext().setContextProperty('backend',backend)
        engine.rootContext().setContextProperty('systemDark',False)
        engine.loadData(b"""import QtQuick
import QtQuick.Controls
import "qml"
ApplicationWindow {
 id: window
 visible: true;width: 600;height: 320
 property int clicks: 0
 Txt {objectName: "message";x: 20;y: 20;width: 190;height: 90;wrapMode: Text.Wrap;text: "UnicodeError: \\u76f8\\u673a\\u8d85\\u65f6 <usb-2>\\nsecond line "+"long detail ".repeat(40)}
 Btn {objectName: "button";x: 20;y: 150;width: 180;height: 40;text: "Run action";onClicked: window.clicks++}
 Select {objectName: "choice";x: 240;y: 150;width: 180;height: 40;model: ["left","right"]}
}
""",QUrl.fromLocalFile(str(Path('scripts/qt_ui/selection-test.qml').resolve())))
        assert engine.rootObjects()
        w=engine.rootObjects()[0];w.requestActivate();QTest.qWait(100)
        text=w.findChild(QObject,'message');button=w.findChild(QObject,'button');choice=w.findChild(QObject,'choice')
        original=text.property('text')
        QTest.mousePress(w,Qt.LeftButton,Qt.NoModifier,QPoint(23,31))
        QTest.mouseMove(w,QPoint(156,31),80)
        QTest.mouseRelease(w,Qt.LeftButton,Qt.NoModifier,QPoint(156,31))
        assert text.property('selectedText'), 'Drag selects text'
        QTest.keyClick(w,Qt.Key_C,Qt.ControlModifier)
        assert app.clipboard().text()==text.property('selectedText')
        QTest.keyClick(w,Qt.Key_A,Qt.ControlModifier);QTest.keyClick(w,Qt.Key_C,Qt.ControlModifier)
        assert app.clipboard().text()==original, 'Copy includes clipped content/newlines/Unicode'
        QTest.keyClick(w,Qt.Key_Z)
        assert text.property('text')==original
        QTest.mouseClick(w,Qt.RightButton,Qt.NoModifier,QPoint(80,40));QTest.qWait(60)
        actions=[o for o in w.findChildren(QObject) if o.inherits('QQuickMenuItem') and o.property('text')=='复制全部文字']
        assert actions, 'Right click opens the lazy copy menu'
        app.clipboard().setText('')
        assert QMetaObject.invokeMethod(actions[0],'triggered')
        assert app.clipboard().text()==original
        QTest.keyClick(w,Qt.Key_Escape);QTest.qWait(30)
        QTest.mouseClick(w,Qt.LeftButton,Qt.NoModifier,QPoint(110,170))
        assert w.property('clicks')==1, 'Button caption passes click through'
        QTest.mouseClick(w,Qt.LeftButton,Qt.NoModifier,QPoint(320,170));QTest.qWait(50)
        assert choice.property('down'), 'Selector caption opens popup'
        QTest.keyClick(w,Qt.Key_Escape)
        w.hide();backend.shutdown()
    '''
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,'-c','\n'.join(line[8:] if line.startswith('        ') else line for line in source.splitlines())],cwd=root,
        env={**os.environ,'QT_QPA_PLATFORM':'offscreen'},capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
