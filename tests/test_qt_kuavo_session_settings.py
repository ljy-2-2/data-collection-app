"""Render and edit the parameter dialog against a fake callback only."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_versioned_parameters_dialog(tmp_path):
    root=Path(__file__).resolve().parents[1]
    code=textwrap.dedent('''
        import json, tempfile
        from pathlib import Path
        from PySide6.QtCore import QUrl, QObject, QMetaObject, Q_ARG
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQuick import QQuickWindow
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtTest import QTest
        from qt_ui.bridge import Bridge
        app=QGuiApplication([])
        schema=json.loads(Path('tests/fixtures/kuavo_settings/schema.json').read_text())
        snapshot=json.loads(Path('tests/fixtures/kuavo_settings/snapshot.json').read_text())
        form=dict(values=snapshot['values'],schema=schema,revision=2,settings_sha256='c'*64,
                  service_instance_id='a'*32,installation_sha256='b'*64,editable=True)
        with tempfile.TemporaryDirectory() as config:
            bridge=Bridge({},demo=True,settings_path=config)
            bridge.poll_timer.stop();bridge.demo=False
            bridge.update(authenticated=True,robot='kuavo-5w',kuavo={'state':'idle','parameter_mode':'service'})
            calls=[]
            def fake_action(name,path,payload,**kw):
                assert path=='/kuavo/session-settings'
                calls.append(payload)
                if payload['action']=='apply':
                    form['values']={**form['values'],**payload['changes']};form['revision']+=1
                kw['done']({'state':'idle','parameter_mode':'service','session_settings':form,
                            'settings_preview':{'ok':True} if payload['action']=='preview' else None})
            bridge.action=fake_action
            engine=QQmlApplicationEngine();warnings=[]
            engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
            engine.rootContext().setContextProperty('backend',bridge)
            engine.rootContext().setContextProperty('systemDark',False)
            path=QUrl.fromLocalFile(str(Path('scripts/qt_ui/qml').resolve())).toString()
            qml=('import QtQuick; import QtQuick.Controls; import "'+path+'" as UI; '
                 'ApplicationWindow {width:1000;height:900;visible:true; '
                 'UI.KuavoSessionSettingsDialog {id:dialog;Component.onCompleted:open()} }')
            engine.loadData(qml.encode())
            assert engine.rootObjects(),warnings
            window=engine.rootObjects()[0];QTest.qWait(150)
            dialog=window.findChild(QObject,'kuavoSessionSettingsDialog')
            assert dialog and calls==[{'action':'read'}]
            def walk(item):
                yield item
                for child in item.childItems():yield from walk(child)
            def find(name):return next(o for o in walk(window.contentItem()) if o.objectName()==name)
            fields=[o for o in walk(window.contentItem()) if o.objectName().startswith('kuavo-service-')]
            assert len(fields)==8
            assert dialog.property('width')==560
            assert all(not o.property('showRange') and not o.property('arrows') for o in fields)
            assert find('kuavoServiceApply').property('text')=='确认'
            assert find('kuavoServiceCancel').property('text')=='取消'
            assert not any(o.objectName()=='kuavoServicePreview' for o in walk(window.contentItem()))
            field=find('kuavo-service-left_gripper_speed_rad_s')
            field.edited.emit(.5)
            find('kuavoServiceCancel').clicked.emit();QTest.qWait(30)
            assert calls==[{'action':'read'}] and not dialog.property('opened')
            dialog.open();QTest.qWait(30)
            field=find('kuavo-service-left_gripper_speed_rad_s')
            assert field.property('value')==15
            # Confirm must include text still being edited, even before blur.
            editor=next(o for o in walk(field) if o.property('validator') is not None)
            editor.setProperty('text','0.6')
            # A polling response cannot silently replace the original revision.
            bridge.update(kuavo={'state':'idle','session_settings':{**form,'revision':99}})
            window.grabWindow().save(__import__('os').environ['KUAVO_SETTINGS_SCREENSHOT'])
            find('kuavoServiceApply').clicked.emit();QTest.qWait(50)
            assert calls[-1]['action']=='apply' and calls[-1]['confirm']
            assert calls[-1]['changes']=={'left_gripper_speed_rad_s':.6} and calls[-1]['expected_revision']==2
            assert not dialog.property('opened')
            dialog.open();QTest.qWait(30)
            field=find('kuavo-service-left_gripper_speed_rad_s')
            assert field.property('value')==.6
            count=len(calls)
            find('kuavoServiceApply').clicked.emit();QTest.qWait(30)
            assert len(calls)==count and not dialog.property('opened'), 'unchanged confirm must not write'
            dialog.open();QTest.qWait(30)
            window.setWidth(360);QTest.qWait(30)
            assert dialog.property('width')==328
            shot=Path(__import__('os').environ['KUAVO_SETTINGS_SCREENSHOT'])
            window.grabWindow().save(str(shot.with_stem('kuavo-settings-narrow')))
            assert not warnings,warnings
            bridge.shutdown()
    ''')
    env={**os.environ,'QT_QPA_PLATFORM':'offscreen','QT_QUICK_BACKEND':'software',
         'PYTHONPATH':os.pathsep.join((str(root/'scripts'),str(root/'teleop_control/src'),os.environ.get('PYTHONPATH',''))),
         'KUAVO_SETTINGS_SCREENSHOT':str(tmp_path/'kuavo-settings.png')}
    result=subprocess.run([sys.executable,'-c',code],cwd=root,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
