"""Shared usage cards stay compact and show complete images on narrow windows."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_usage_guide_popup_size_sources_and_theme(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=textwrap.dedent('''
        from pathlib import Path
        from PySide6.QtCore import QObject,Property,Signal,QUrl,QPoint
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQuick import QQuickWindow
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtTest import QTest
        from shiboken6 import delete
        class Backend(QObject):
            changed=Signal()
            theme='light'
            @Property('QVariantMap',notify=changed)
            def state(self):return dict(theme=self.theme)
        app=QGuiApplication([]);backend=Backend();engine=QQmlApplicationEngine();warnings=[]
        engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
        engine.rootContext().setContextProperty('backend',backend)
        engine.rootContext().setContextProperty('systemDark',False)
        engine.loadData(b'import QtQuick\\nWindow {id:win;width:1280;height:800;visible:true;color: "#edf1f5";UsageGuide {objectName:"guide";anchors.right:parent.right;anchors.rightMargin:16;y:24;viewportWidth:win.width;viewportHeight:win.height}}',
            QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml/guide-test.qml').resolve())))
        assert engine.rootObjects(),warnings
        window=engine.rootObjects()[0];guide=window.findChild(QObject,'guide')
        preview=window.findChild(QObject,'usageGuidePreview');picture=window.findChild(QObject,'usageGuideImage')
        for source in ('tianji-usage-guide.png','umi-usage-guide.png','teleop-usage-guide.png',''):
            for width,theme in ((1280,'light'),(360,'dark')):
                window.setWidth(width);backend.theme=theme;backend.changed.emit()
                guide.setProperty('source',QUrl.fromLocalFile(str(Path('public',source).resolve())) if source else QUrl())
                QTest.mouseMove(window,QPoint(20,20));QTest.qWait(220)
                QTest.mouseMove(window,QPoint(width-35,36));QTest.qWait(250)
                assert preview.property('visible'), (source,width)
                assert preview.property('width')<=min(600,width-32)
                assert preview.property('height')<=(800-120)*2/3+1
                if source:
                    assert picture.property('sourceSize').width()>0
                    assert picture.property('paintedWidth')<=picture.property('width')+1
                    assert picture.property('paintedHeight')<=picture.property('height')+1
                shot=window.grabWindow();assert not shot.isNull()
                shot.save(str(Path(__import__('sys').argv[1])/f'{source or "blank"}-{width}.png'))
        window.close();delete(engine)
        assert not warnings,warnings
    ''')
    env={**os.environ,'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software'}
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],cwd=root,env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
