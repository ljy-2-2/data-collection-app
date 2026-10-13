"""Exercise the native MJPEG consumer with fragmented/bursty network replies."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


def test_hd_bursts_reconnect_staleness_and_source_changes():
    script = textwrap.dedent(r'''
        from PySide6.QtCore import QObject, Signal, QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QGuiApplication, QImage, QColor
        from PySide6.QtNetwork import QNetworkReply
        from qt_ui.visuals import CameraItem
        app = QGuiApplication([])
        image = QImage(32, 24, QImage.Format_RGB32)
        image.fill(QColor('red'))
        encoded = QByteArray(); output = QBuffer(encoded)
        output.open(QIODevice.WriteOnly); assert image.save(output, 'JPEG')
        jpeg = bytes(encoded)
        part = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n'
        class Reply(QObject):
            readyRead = Signal(); finished = Signal()
            def __init__(self):
                super().__init__(); self.payload = b''; self.aborted = False
            def bytesAvailable(self): return len(self.payload)
            def read(self, count):
                data, self.payload = self.payload[:count], self.payload[count:]
                return data
            def error(self): return QNetworkReply.OperationCanceledError if self.aborted else QNetworkReply.NoError
            def abort(self):
                self.aborted = True; self.payload = b''; self.finished.emit()
        class Network:
            def get(self, request):
                self.latest = Reply(); return self.latest
        camera = CameraItem(); camera.network = Network()
        camera.source = 'http://localhost/umi/cameras/center'
        first = camera.reply
        first.payload = part * (9 * 1024 * 1024 // len(part) + 1)
        first.readyRead.emit()
        assert not first.aborted and camera.online and camera.hasFrame
        assert camera.frame.width() == 32 and len(camera.buffer) < len(part)
        # A brief connection failure does not clear a recently received image.
        first.abort()
        assert camera.online and camera.hasFrame
        camera.retry.stop(); camera.connect_stream()
        second = camera.reply
        second.payload = part[:100]; second.readyRead.emit()
        second.payload = part[100:]; second.readyRead.emit()
        assert camera.online and camera.hasFrame and not second.aborted
        # A real stall is clearly offline and reconnects, retaining the last frame.
        camera.stream_stalled()
        assert not camera.online and camera.hasFrame and second.aborted
        camera.retry.stop(); camera.connect_stream()
        camera.reply.payload = part; camera.reply.readyRead.emit()
        assert camera.online
        # Changing camera/session must clear the previous camera's image.
        camera.source = 'http://localhost/umi/cameras/left'
        assert not camera.online and not camera.hasFrame
        # Oversized incomplete data is still rejected, bounding memory use.
        invalid = camera.reply
        invalid.payload = b'x' * (9 * 1024 * 1024)
        invalid.readyRead.emit()
        assert invalid.aborted and not camera.online and not camera.buffer
        camera.source = ''; camera.retry.stop(); camera.stale.stop()
        # Large previews decode to the displayed size instead of blocking UI
        # on full-resolution expansion; source JPEG itself is unchanged.
        hd = QImage(1920,1080,QImage.Format_RGB32);hd.fill(QColor('blue'))
        blob = QByteArray(); out = QBuffer(blob);out.open(QIODevice.WriteOnly)
        assert hd.save(out,'JPEG')
        camera.setWidth(640);camera.setHeight(480)
        camera.source='http://localhost/umi/cameras/right'
        camera.reply.payload=bytes(blob);camera.reply.readyRead.emit()
        assert camera.online and camera.frame.width()==640 and camera.frame.height()==360
        assert QImage.fromData(blob,'JPEG').width()==1920
        camera.source=''
        print('PASS: HD burst, fragmented JPEG, short reconnect, true stall, recovery, source reset, memory bound')
    ''')
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONPATH=str(root/'scripts'))
    result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
