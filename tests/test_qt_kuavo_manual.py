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
                poses=[{'id':'saved','name':'工作位置'}],pose_supported=True,initial_pose_available=True,poses_available=True,
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
            assert disc.width()==disc.height()==180 and disc.property('radius')==90
            assert not any(o.property('text') in ('前进','后退','左移','右移','左旋','右旋','停止') for o in walk(disc))
            assert item('kuavoManualExit').isVisible()
            assert not any(o.objectName()=='kuavoManualEnter' for o in walk(panel))
            assert item('kuavoManualStep').property('currentIndex')==0
            assert len([o for o in walk(panel) if hasattr(o,'clicked') and o.property('text')=='▲'])==20
            # Changing the selector changes the real outgoing joint delta.
            up=next(o for o in walk(panel) if hasattr(o,'clicked') and o.property('text')=='▲')
            for index,amount in enumerate((1,5,10)):
                item('kuavoManualStep').setProperty('currentIndex',index);QTest.qWait(10)
                up.clicked.emit()
                assert next(d['command'] for _,d in reversed(requests) if d['command']['action']=='joint')['delta_deg']==amount
            item('kuavoManualStep').setProperty('currentIndex',0)
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
                point=disc.mapToScene(QPointF(x*180/220,y*180/220)).toPoint()
                QTest.mousePress(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(30)
                QTest.mouseRelease(window,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(30)
                motions=[data['command']['direction'] for _,data in requests if data['command']['action']=='base']
                assert motions==[name,'stop'],(name,motions)
            requests.clear()
            for x,y in [(0,0),(110,110)]:
                point=disc.mapToScene(QPointF(x*180/220,y*180/220)).toPoint()
                QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point)
            assert not [data for _,data in requests if data['command']['action']=='base']
            # Stale telemetry disables sending; retained values are display only.
            bridge.update(kuavo={'state':'idle','active':True,'manual':{**manual,'fresh':False}})
            QTest.qWait(50);assert not direction.isEnabled()
            bridge.update(kuavo={'state':'idle','active':True,'manual':manual});QTest.qWait(50)
            # Same toolbar location and complete save/restore/delete wiring.
            assert item('kuavo-save-pose').isEnabled()
            assert item('kuavo-restore-pose').isEnabled()
            assert not item('kuavo-delete-pose').isEnabled()
            bridge.setValue('kuavoSelectedPose','saved');QTest.qWait(30)
            assert item('kuavo-delete-pose').isEnabled()
            item('kuavo-restore-pose').clicked.emit();QTest.qWait(30)
            item('kuavo-pose-confirm-accept').clicked.emit();QTest.qWait(30)
            assert any(data.get('command')=={'action':'restore','pose_id':'saved'} for _,data in requests)
            actions=[]
            bridge.action=lambda *args,**kwargs:actions.append((args,kwargs))
            item('kuavo-save-pose').clicked.emit();QTest.qWait(30)
            item('kuavo-pose-name').setProperty('text','桌面位置')
            item('kuavo-pose-save-confirm').clicked.emit();QTest.qWait(30)
            assert actions[-1][0][1:] == ('/kuavo/manual/poses',{'action':'save','name':'桌面位置','session_id':'fake'})
            item('kuavo-delete-pose').clicked.emit();QTest.qWait(30)
            item('kuavo-pose-confirm-accept').clicked.emit();QTest.qWait(30)
            assert actions[-1][0][2]=={'action':'delete','id':'saved'}
            bridge.update(kuavo={'state':'idle','active':True,'manual':{**manual,'busy':True,'restoring':True}});QTest.qWait(30)
            assert item('kuavo-restore-pose').isEnabled() and item('kuavo-restore-pose').property('text')=='停止'
            item('kuavo-restore-pose').clicked.emit()
            assert any(data.get('command')=={'action':'stop'} for _,data in requests)
            bridge.update(kuavo={'state':'idle','active':True,'manual':manual});QTest.qWait(50)
            bridge.setValue('kuavoSelectedPose','initial');QTest.qWait(30)
            output=Path('reports/kuavo-manual-poses-20261028.png');output.parent.mkdir(exist_ok=True)
            image=panel.grabToImage();QTest.qWait(100)
            assert image.saveToFile(str(output.resolve()))
            assert not warnings,warnings
            bridge.demo=True;bridge.shutdown();window.close()
    ''')
    result=subprocess.run([sys.executable,'-c',script],cwd=root,
        env={**os.environ,'QT_QPA_PLATFORM':'offscreen','PYTHONDONTWRITEBYTECODE':'1'},text=True,capture_output=True,timeout=25)
    assert result.returncode==0,result.stdout+result.stderr
