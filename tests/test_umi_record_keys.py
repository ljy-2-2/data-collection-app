"""Real Qt focus/repeat events for a Space-key foot pedal, without hardware."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_pedal_with_buttons_editors_popups_and_held_keys():
    root = Path(__file__).resolve().parents[1]
    code = textwrap.dedent('''
        from PySide6.QtCore import QObject, QUrl, Qt, QEvent
        from PySide6.QtGui import QGuiApplication, QKeyEvent
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtTest import QTest
        from qt_ui.record_keys import UmiRecordKeys
        app = QGuiApplication([])
        engine = QQmlApplicationEngine()
        engine.loadData(b"""
            import QtQuick
            import QtQuick.Controls
            ApplicationWindow {
                visible: true; width: 500; height: 400
                property bool recordKeyBlocked: false
                property int accidentalClicks: 0
                Button {objectName: "button"; text: "Other action"; onClicked: accidentalClicks++}
                TextField {objectName: "editor"; y: 60}
                TextArea {objectName: "textarea"; y: 110}
                TextEdit {objectName: "label";y: 160;text: "Status detail";readOnly: true;selectByMouse: true}
                Dialog {objectName: "dialog"; modal: true; standardButtons: Dialog.Ok}
            }
        """, QUrl('file:///pedal-test.qml'))
        assert engine.rootObjects()
        window = engine.rootObjects()[0]
        class Backend:
            state = dict(authenticated=True, closing=False, page='umi', umiStep=3, busy='',
                         umi=dict(state='ready', monitor_connected=True, recording=False))
            calls = []
            def update(self, **values): self.state.update(values)
            def recordUmi(self, action):
                self.calls.append(action)
                self.state['umi']['recording'] = action == 'start'
                self.state['umi']['state'] = 'recording' if action == 'start' else 'ready'
        backend = Backend()
        button = window.findChild(QObject, 'button')
        window.requestActivate(); button.forceActiveFocus(); QTest.qWait(100)
        assert app.focusWindow() == window
        keys = UmiRecordKeys(app, window, backend)
        def space(): QTest.keyClick(window, Qt.Key_Space); QTest.qWait(15)
        QTest.keyPress(window, Qt.Key_Space)
        QTest.qWait(20)
        assert backend.calls == ['start'], (backend.calls, keys.monitoring(), keys.editing_or_popup(), keys.space_down, window.activeFocusItem().metaObject().className())
        # Hold through a fast server response and several native auto-repeat
        # press/release pairs. One physical press must still make one request.
        for _ in range(4):
            for kind in (QEvent.KeyRelease, QEvent.KeyPress):
                event = QKeyEvent(kind, Qt.Key_Space, Qt.NoModifier, ' ', True, 1)
                app.sendEvent(window, event)
        assert backend.calls == ['start']
        QTest.keyRelease(window, Qt.Key_Space)
        QTest.qWait(20)
        space()
        assert backend.calls == ['start', 'stop']
        assert window.property('accidentalClicks') == 0
        assert '已收到空格' in backend.state['umiPedalMessage']
        for state in ('busy', 'starting', 'offline', 'closing'):
            backend.state['busy'] = 'record-stop' if state == 'busy' else ''
            backend.state['closing'] = state == 'closing'
            backend.state['umi'].update(state='starting' if state == 'starting' else 'ready',
                                        monitor_connected=state != 'offline')
            space()
            assert backend.calls == ['start', 'stop'], state
        backend.state.update(busy='', closing=False)
        backend.state['umi'].update(state='ready', monitor_connected=True)
        for name in ('editor', 'textarea'):
            editor = window.findChild(QObject, name)
            editor.forceActiveFocus(); space()
            assert editor.property('text') == ' ', (name, editor.property('text'))
            assert backend.calls == ['start', 'stop']
        label = window.findChild(QObject, 'label')
        label.forceActiveFocus()
        assert not keys.editing_or_popup(), 'A read-only status does not block the pedal'
        from PySide6.QtCore import QMetaObject
        QMetaObject.invokeMethod(label, 'selectAll')
        assert keys.editing_or_popup(), 'Copy selection blocks accidental recording'
        button.forceActiveFocus()
        window.setProperty('recordKeyBlocked', True); space()
        assert backend.calls == ['start', 'stop']
        window.setProperty('recordKeyBlocked', False)
        dialog = window.findChild(QObject, 'dialog')
        dialog.setProperty('visible', True); QTest.qWait(40); space()
        assert backend.calls == ['start', 'stop'], 'A popup must not start a recording'
        dialog.setProperty('visible', False); button.forceActiveFocus()
        # Contexts outside the monitoring page never issue recording commands.
        for page, step, authenticated in [('umi', 2, True), ('teleop', 3, True), ('umi', 3, False)]:
            backend.state.update(page=page, umiStep=step, authenticated=authenticated)
            space(); assert backend.calls == ['start', 'stop']
        backend.state.update(page='umi', umiStep=3, authenticated=True)
        space(); assert backend.calls[-1] == 'start'
        space(); assert backend.calls[-1] == 'stop'
        app.removeEventFilter(keys)
        window.close()
    ''')
    result = subprocess.run([sys.executable, '-c', code], cwd=root,
                            env=dict(os.environ, QT_QPA_PLATFORM='offscreen',
                                     QT_QUICK_BACKEND='software', PYTHONPATH=str(root / 'scripts')),
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
