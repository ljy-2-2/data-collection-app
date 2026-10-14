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
        from qt_ui.visuals import CameraItem, _MjpegPreviewParser
        app = QGuiApplication([])
        from PySide6.QtTest import QTest
        import time
        def drain():
            deadline=time.monotonic()+3
            while camera._decode_job is not None or camera._decode_pending is not None:
                assert time.monotonic()<deadline, 'JPEG worker did not finish'
                QTest.qWait(2)

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
        first.readyRead.emit(); drain()
        assert not first.aborted and camera.online and camera.hasFrame
        assert camera.frame.width() == 32 and len(camera.buffer) < len(part)
        # A brief connection failure does not clear a recently received image.
        first.abort()
        assert camera.online and camera.hasFrame
        camera.retry.stop(); camera.connect_stream()
        second = camera.reply
        second.payload = part[:100]; second.readyRead.emit(); drain()
        second.payload = part[100:]; second.readyRead.emit(); drain()
        assert camera.online and camera.hasFrame and not second.aborted
        # A real stall is clearly offline and reconnects, retaining the last frame.
        camera.stream_stalled()
        assert not camera.online and camera.hasFrame and second.aborted
        camera.retry.stop(); camera.connect_stream()
        camera.reply.payload = part; camera.reply.readyRead.emit(); drain()
        assert camera.online
        # Changing camera/session must clear the previous camera's image.
        camera.source = 'http://localhost/umi/cameras/left'
        assert not camera.online and not camera.hasFrame
        # Oversized incomplete data is still rejected, bounding memory use.
        invalid = camera.reply
        invalid.payload = b'x' * (9 * 1024 * 1024)
        invalid.readyRead.emit(); drain()
        assert invalid.aborted and not camera.online and not camera.buffer
        camera.source = ''; camera.retry.stop(); camera.stale.stop()
        # Large previews decode to the displayed size instead of blocking UI
        # on full-resolution expansion; source JPEG itself is unchanged.
        hd = QImage(1920,1080,QImage.Format_RGB32);hd.fill(QColor('blue'))
        blob = QByteArray(); out = QBuffer(blob);out.open(QIODevice.WriteOnly)
        assert hd.save(out,'JPEG')
        camera.setWidth(640);camera.setHeight(480)
        camera.source='http://localhost/umi/cameras/right'
        camera.reply.payload=bytes(blob);camera.reply.readyRead.emit(); drain()
        assert camera.online and camera.frame.width()==640 and camera.frame.height()==360
        assert QImage.fromData(blob,'JPEG').width()==1920
        camera.source=''

        # A real JPEG thumbnail in an APP segment contains another end marker.
        # The multipart length must take precedence over that embedded marker.
        outer = QImage(96,64,QImage.Format_RGB32);outer.fill(QColor('green'))
        outer_blob=QByteArray();outer_out=QBuffer(outer_blob)
        outer_out.open(QIODevice.WriteOnly);assert outer.save(outer_out,'JPEG')
        nested=bytes(outer_blob[:2])+b'\xff\xe2'+(len(jpeg)+2).to_bytes(2,'big')+jpeg+bytes(outer_blob[2:])
        assert QImage.fromData(nested,'JPEG').size()==outer.size()
        def multipart(body):
            return b'--frame\r\nContent-Type: image/jpeg\r\ncOnTeNt-LeNgTh: '+str(len(body)).encode()+b'\r\n\r\n'+body+b'\r\n'
        camera.source='http://localhost/umi/device-cameras/video0'
        reply=camera.reply;framed=multipart(nested)
        # Fragment the header and stop at the embedded EOI: no partial image
        # may become the displayed frame or declare the camera online.
        thumb_end=framed.find(b'\xff\xd9')+2
        for fragment in (framed[:17],framed[17:thumb_end]):
            reply.payload=fragment;reply.readyRead.emit(); drain()
            assert not camera.online and not camera.hasFrame and not reply.aborted
        reply.payload=framed[thumb_end:];reply.readyRead.emit(); drain()
        assert camera.online and camera.frame.size()==outer.size() and not reply.aborted
        # A coalesced multipart burst still displays the newest whole image.
        reply.payload=multipart(jpeg)+framed;reply.readyRead.emit(); drain()
        assert camera.frame.size()==outer.size() and len(camera.buffer)<10
        camera.source=''

        for length in (b'0',b'-1',b'999999999',b'not-a-number',b'12\r\nContent-Length: 24'):
            camera.source='http://localhost/umi/cameras/center'
            reply=camera.reply
            reply.payload=b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+length+b'\r\n\r\n'+jpeg
            reply.readyRead.emit(); drain()
            assert reply.aborted and not camera.online and not camera.buffer
            camera.source=''
        # A declared body cannot accumulate unbounded memory while waiting for
        # its missing tail, and a frame exactly at the configured cap is legal.
        parser=_MjpegPreviewParser()
        size=parser.MAX_FRAME_BYTES
        body=b'\xff\xd8'+b'x'*(size-4)+b'\xff\xd9'
        framed=multipart(body)
        parsed=None
        for offset in range(0,len(framed),65536):
            frame=parser.feed(framed[offset:offset+65536])
            if frame is not None:parsed=frame
            assert len(parser.buffer)<=size
        assert parsed==body and len(parser.buffer)<10
        # Snapshot endpoints remain independent of multipart parsing.
        camera.source='http://localhost/camera/frame'
        reply=camera.reply;reply.payload=nested;reply.readyRead.emit(); drain()
        assert not camera.hasFrame
        reply.finished.emit(); drain()
        assert camera.online and camera.frame.size()==outer.size()
        camera.source='';camera.retry.stop();camera.stale.stop()
        # A busy decoder must not freeze the GUI, queue old frames, or show
        # an old camera's late result after the selected source changes.
        import threading
        import qt_ui.visuals as visuals
        original_decode=visuals.decode_preview_image
        entered=threading.Event();release=threading.Event();decoded=[]
        def delayed_decode(data,target):
            decoded.append(data)
            if len(decoded)==1:
                entered.set();assert release.wait(3)
            return original_decode(data,target)
        visuals.decode_preview_image=delayed_decode
        camera.source='http://localhost/umi/cameras/center'
        reply=camera.reply;reply.payload=multipart(jpeg);reply.readyRead.emit()
        deadline=time.monotonic()+2
        while not entered.is_set():
            assert time.monotonic()<deadline;QTest.qWait(2)
        from PySide6.QtCore import QTimer
        ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));QTest.qWait(5)
        assert ticks, 'JPEG decoding blocked GUI events'
        reply.payload=multipart(bytes(blob));reply.readyRead.emit()
        assert camera._decode_pending is not None
        camera.source='http://localhost/umi/cameras/left'
        assert not camera.hasFrame and camera._decode_pending is None
        reply=camera.reply;reply.payload=multipart(bytes(outer_blob));reply.readyRead.emit()
        assert camera._decode_pending[1]==bytes(outer_blob)
        shown=[]
        camera.frameChanged.connect(lambda:shown.append(camera.frame.size()) if camera.hasFrame else None)
        release.set();drain()
        assert decoded==[jpeg,bytes(outer_blob)], 'Obsolete pending frame was decoded'
        assert shown==[outer.size()] and camera.frame.size()==outer.size()
        camera.source='';visuals.decode_preview_image=original_decode
        from PySide6.QtCore import QThreadPool
        assert QThreadPool.globalInstance().waitForDone(3000)
        print('PASS: burst, fragmentation, reconnect, staleness, source reset, size limit, scaled decode, multipart thumbnail, invalid lengths, snapshot')
    ''')
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONPATH=str(root/'scripts'))
    result = subprocess.run([sys.executable, '-c', script], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
