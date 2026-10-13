#!/usr/bin/env python3
"""PySide6/Qt Quick desktop application; no browser or web view is involved."""
from __future__ import annotations
import argparse
from collections import deque
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from generate_runtime_config import platform_urls, read_env


def desktop_text_scale() -> float:
    try:
        value = subprocess.check_output(
            ['gsettings', 'get', 'org.gnome.desktop.interface', 'text-scaling-factor'],
            timeout=2, stderr=subprocess.DEVNULL, text=True)
        return max(.75, min(3., float(value.strip())))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 1.


def configure_rendering(values: dict[str, str], *, software=False) -> bool:
    # Use OpenGL for both Qt Quick and the complete native Qt Quick 3D model.
    # Qt's 2D-only software scene graph cannot display Qt Quick 3D.
    os.environ.setdefault('QSG_RHI_BACKEND', 'opengl')
    # Python-painted video/plots share the GUI thread; avoid cross-thread GIL waits.
    os.environ.setdefault('QSG_RENDER_LOOP', 'basic')
    scale = os.environ.get('APP_UI_SCALE', values.get('APP_UI_SCALE', ''))
    try:
        scale = float(scale) if scale else desktop_text_scale()
        if not .75 <= scale <= 3: raise ValueError
    except ValueError:
        scale = desktop_text_scale()
    os.environ.setdefault('QT_SCALE_FACTOR', str(scale))
    software = software or os.environ.get('APP_SOFTWARE_RENDERING', values.get('APP_SOFTWARE_RENDERING', '0')).lower() in ('1', 'true', 'yes')
    if software:
        os.environ['LIBGL_ALWAYS_SOFTWARE'] = '1'
    return software


def main() -> int:
    parser = argparse.ArgumentParser(description='小凯实验室数采平台 · PySide6')
    parser.add_argument('--mock', action='store_true', help='模拟机器人后端')
    parser.add_argument('--demo', action='store_true', help='界面演示，不启动采集服务')
    parser.add_argument('--software-rendering', action='store_true', help='使用 Mesa CPU 渲染完整三维模型')
    args = parser.parse_args()
    values = read_env(ROOT / '.env')
    values.update(platform_urls(values))
    service_env = os.environ.copy()
    configure_rendering(values, software=args.software_rendering)
    from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, QUrl, Qt, Slot
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
    from qt_ui.bridge import Bridge
    from qt_ui.visuals import CameraItem, CurveItem, Backdrop, G2View, RobotScene
    from qt_ui.record_keys import UmiRecordKeys

    app = QGuiApplication(sys.argv[:1])
    app.setOrganizationName('XiaokaiLab')
    app.setApplicationName('xiaokai-lab-data-collection')
    app.setApplicationDisplayName('小凯实验室数采平台')
    app.setDesktopFileName('xiaokai-lab-data-collection')
    app.setWindowIcon(QIcon(str(ROOT / 'public/xiaokai-data-collection.svg')))
    app.setQuitOnLastWindowClosed(False)
    for name, cls in [('CameraItem',CameraItem),('CurveItem',CurveItem),('Backdrop',Backdrop),('G2View',G2View)]:
        qmlRegisterType(cls, 'Collection.Native', 1, 0, name)
    if args.demo: values['LOCAL_CONTROL_URL']='http://127.0.0.1:1'
    bridge = Bridge(values, demo=args.demo)
    scene = RobotScene(ROOT)
    bridge.changed.connect(lambda: scene.update_pose(bridge.state['tianji']))
    engine = QQmlApplicationEngine()
    context = engine.rootContext()
    context.setContextProperty('backend', bridge)
    context.setContextProperty('robotScene', scene)
    def system_theme():
        context.setContextProperty('systemDark', app.styleHints().colorScheme() == Qt.ColorScheme.Dark)
    system_theme()
    app.styleHints().colorSchemeChanged.connect(system_theme)
    engine.load(QUrl.fromLocalFile(str(ROOT / 'scripts/qt_ui/qml/Main.qml')))
    if not engine.rootObjects(): return 2
    window = engine.rootObjects()[0]
    record_keys = UmiRecordKeys(app, window, bridge)
    window.showMaximized()

    class Runtime(QObject):
        def __init__(self):
            super().__init__()
            self.closing = False
            self.buffer = ''
            self.tail = deque(maxlen=35)
            self.process = QProcess(self)
            self.process.setProcessChannelMode(QProcess.MergedChannels)
            env = QProcessEnvironment()
            for key, value in service_env.items(): env.insert(key, value)
            env.insert('PYTHONUNBUFFERED', '1')
            self.process.setProcessEnvironment(env)
            self.process.setWorkingDirectory(str(ROOT))
            self.process.readyReadStandardOutput.connect(self.output)
            self.process.finished.connect(self.finished)
            self.process.errorOccurred.connect(self.error)
            self.timeout = QTimer(self);self.timeout.setSingleShot(True)
            self.timeout.timeout.connect(lambda: bridge.serviceFailed('本机服务启动超时，请查看启动日志后重试'))
            self.log = None
            try:
                logdir = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state'))) / 'xiaokai-lab-data-collection'
                logdir.mkdir(parents=True, exist_ok=True)
                self.log = (logdir/'desktop.log').open('w')
            except OSError: pass
        def start(self):
            if args.demo: bridge.serviceReady();return
            self.process.start(sys.executable, [str(ROOT/'scripts/run_app.py'), '--backend-only', '--no-browser', *(['--mock'] if args.mock else [])])
            self.timeout.start(60000)
        def output(self):
            output = bytes(self.process.readAllStandardOutput()).decode('utf-8',errors='replace')
            if self.log: self.log.write(output);self.log.flush()
            print(output, end='', flush=True)
            self.buffer += output
            lines = self.buffer.split('\n');self.buffer = lines.pop()
            for line in lines:
                self.tail.append(line)
                if line.strip() == 'NATIVE_SERVICE_READY' and not self.closing:
                    self.timeout.stop();bridge.serviceReady()
        def error(self, error):
            if error == QProcess.FailedToStart:
                self.timeout.stop();bridge.serviceFailed('本机采集服务无法启动：'+self.process.errorString())
        def finished(self, code, _status):
            self.output();self.timeout.stop()
            if self.closing: app.quit()
            else: bridge.serviceFailed('本机服务已退出（'+str(code)+'）：\n'+'\n'.join(self.tail)[-2500:])
        @Slot()
        def close(self):
            if self.closing: return
            self.closing = True;self.timeout.stop();bridge.shutdown()
            if self.process.state() == QProcess.NotRunning: app.quit();return
            # Supervisor first saves UMI, then shuts down all child processes.
            os.kill(self.process.processId(), signal.SIGINT)
            QTimer.singleShot(260000, self.force_stop)
        def force_stop(self):
            if self.process.state() != QProcess.NotRunning:
                self.process.terminate()
                QTimer.singleShot(6000, self.process.kill)
    runtime = Runtime()
    bridge.closeRequested.connect(runtime.close)
    signal.signal(signal.SIGINT, lambda *_: runtime.close())
    signal.signal(signal.SIGTERM, lambda *_: runtime.close())
    heartbeat = QTimer();heartbeat.start(200);heartbeat.timeout.connect(lambda: None)
    QTimer.singleShot(0, runtime.start)
    result = app.exec()
    if runtime.log: runtime.log.close()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
