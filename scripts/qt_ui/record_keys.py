"""Handle a keyboard-style Space pedal before focused Qt controls consume it."""
import time
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickWindow


class UmiRecordKeys(QObject):
    def __init__(self, application, window: QQuickWindow, backend):
        super().__init__(application)
        self.window = window
        self.backend = backend
        self.space_down = False
        application.installEventFilter(self)

    def monitoring(self):
        state = self.backend.state
        return (QGuiApplication.focusWindow() == self.window
                and state.get('authenticated') and not state.get('closing')
                and state.get('page') == 'umi' and state.get('umiStep') == 3)

    def editing_or_popup(self):
        if self.window.property('recordKeyBlocked'):
            return True
        focus = self.window.activeFocusItem()
        while focus is not None:
            if focus.inherits('QQuickPopupItem'):
                return True
            if any(focus.inherits(name) for name in ('QQuickTextInput', 'QQuickTextEdit')):
                # A click on a read-only status label must not disable the
                # pedal. Active text selection/editing still blocks recording.
                if not focus.property('readOnly') or focus.property('selectedText'):
                    return True
            focus = focus.parentItem()
        return False

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.ApplicationDeactivate, QEvent.Type.WindowDeactivate):
            if watched == self.window or kind == QEvent.Type.ApplicationDeactivate:
                self.space_down = False
            return False
        if kind not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return False
        if event.key() != Qt.Key.Key_Space:
            return False
        # A release must never click the previously focused button after our
        # press started/stopped a recording, even if UI state changed meanwhile.
        if kind == QEvent.Type.KeyRelease and self.space_down:
            if not event.isAutoRepeat():
                self.space_down = False
            event.accept(); return True
        if event.modifiers() != Qt.KeyboardModifier.NoModifier:
            return False
        if not self.monitoring() or self.editing_or_popup():
            return False
        if kind == QEvent.Type.ShortcutOverride:
            event.accept(); return True
        if kind == QEvent.Type.KeyPress:
            repeat = self.space_down or event.isAutoRepeat()
            self.space_down = True
            if not repeat:
                state = self.backend.state
                umi = state.get('umi', {})
                message = '已收到空格 ' + time.strftime('%H:%M:%S')
                if state.get('busy'):
                    message += ' · 正在处理上一操作'
                elif not umi.get('recording') and not (umi.get('state') == 'ready' and umi.get('monitor_connected')):
                    message += ' · 采集服务尚未就绪'
                self.backend.update(umiPedalMessage=message)
                if not state.get('busy'):
                    if umi.get('recording'):
                        self.backend.recordUmi('stop')
                    elif umi.get('state') == 'ready' and umi.get('monitor_connected'):
                        self.backend.recordUmi('start')
            event.accept(); return True
        return False
