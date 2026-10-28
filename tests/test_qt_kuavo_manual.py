"""Render manual controls and verify press/release wiring with fake requests only."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_manual_layout_and_release(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=textwrap.dedent('''
        import json, math, tempfile
        from pathlib import Path
        from PySide6.QtCore import QUrl, QObject, QPointF, Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQuick import QQuickWindow, QQuickItem
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtTest import QTest
        from qt_ui.bridge import Bridge
        app=QGuiApplication([])
        rows=json.loads(Path('teleop_control/assets/kuavo/manual-joints.json').read_text())
        positions={j['name']:math.radians((j['min_deg']+j['max_deg'])/2) for j in rows}
        with tempfile.TemporaryDirectory() as settings:
            bridge=Bridge({},demo=True,settings_path=settings);bridge.poll_timer.stop()
            manual=dict(state='ready',active=True,available=True,session_id='fake',fresh=True,busy=False,
                joints=rows,telemetry=dict(joints=positions,grippers={'left':{'closure_fraction':.3},'right':{'closure_fraction':.8}}))
            bridge.update(authenticated=True,kuavo={'state':'idle','active':True,'manual':manual})
            bridge.demo=False
            requests=[]
            bridge.request=lambda path,data=None,**kwargs:requests.append((path,data))
            engine=QQmlApplicationEngine();warnings=[]
            engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
            engine.rootContext().setContextProperty('backend',bridge)
            engine.rootContext().setContextProperty('systemDark',False)
            qml='''+repr('''import QtQuick
import QtQuick.Controls
import "file://'''+str(root/'scripts/qt_ui/qml')+'''"
ApplicationWindow {
 id:app;width:1120;height:900;visible:true
 KuavoManual {id:panel;width:parent.width-24;x:12;y:12}
}
''')+'''
            engine.loadData(qml.encode())
            assert engine.rootObjects(),warnings
            window=engine.rootObjects()[0];QTest.qWait(160)
            def walk(item):
                yield item
                for child in item.childItems():yield from walk(child)
            def item(name):return next(o for o in walk(window.contentItem()) if o.objectName()==name)
            panel=item('kuavoManualCard');disc=item('kuavoBaseDisc')
            assert disc.width()==disc.height()==220 and disc.property('radius')==110
            assert not any(o.property('text') in ('前进','后退','左移','右移','左旋','右旋','停止') for o in walk(disc))
            assert item('kuavoManualExit').isVisible() and not item('kuavoManualEnter').isVisible()
            assert len([o for o in walk(panel) if hasattr(o,'clicked') and o.property('text')=='▲'])==20
            direction=item('kuavoBase_forward')
            assert direction.isEnabled()
            direction.pressed.emit();QTest.qWait(110)
            assert any(data['command']=={'action':'base','direction':'forward'} for _,data in requests)
            direction.released.emit();QTest.qWait(110)
            motions=[data['command'] for _,data in requests if data['command']['action']=='base']
            assert motions[-1]=={'action':'base','direction':'stop'}
            requests.clear();direction.pressed.emit();direction.canceled.emit();QTest.qWait(110)
            assert [d['command'] for _,d in requests if d['command']['action']=='base'][-1]['direction']=='stop'
            requests.clear();direction.pressed.emit()
            bridge.update(kuavo={'state':'idle','active':True,'manual':{**manual,'busy':True}})
            QTest.qWait(110)
            assert [d['command'] for _,d in requests if d['command']['action']=='base'][-1]['direction']=='stop'
            bridge.update(kuavo={'state':'idle','active':True,'manual':manual});QTest.qWait(50)
            # Real pointer hits match the six curved regions, not their bounding boxes.
            for name,x,y in [('forward',110,28),('backward',110,192),('left',28,110),('right',192,110),('turn_left',84,110),('turn_right',136,110)]:
                requests.clear()
                point=disc.mapToScene(QPointF(x,y)).toPoint()
                QTest.mousePress(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(30)
                QTest.mouseRelease(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(30)
                motions=[data['command']['direction'] for _,data in requests if data['command']['action']=='base']
                assert motions==[name,'stop'],(name,motions)
            requests.clear()
            for x,y in [(0,0),(110,110)]:
                point=disc.mapToScene(QPointF(x,y)).toPoint()
                QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point)
            assert not [data for _,data in requests if data['command']['action']=='base']
            # Stale telemetry disables sending; retained values are display only.
            bridge.update(kuavo={'state':'idle','active':True,'manual':{**manual,'fresh':False}})
            QTest.qWait(50);assert not direction.isEnabled()
            bridge.update(kuavo={'state':'idle','active':True,'manual':manual});QTest.qWait(50)
            output=Path('reports/kuavo-manual-style-20261028.png');output.parent.mkdir(exist_ok=True)
            image=panel.grabToImage();QTest.qWait(100)
            assert image.saveToFile(str(output.resolve()))
            assert not warnings,warnings
            bridge.demo=True;bridge.shutdown();window.close()
    ''')
    result=subprocess.run([sys.executable,'-c',script],cwd=root,
        env={**os.environ,'QT_QPA_PLATFORM':'offscreen','PYTHONDONTWRITEBYTECODE':'1'},text=True,capture_output=True,timeout=25)
    assert result.returncode==0,result.stdout+result.stderr
