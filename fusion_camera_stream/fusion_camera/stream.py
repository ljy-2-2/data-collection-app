"""TLS image + passive dual-arm state client. Does not use SSH or robot SDK."""
import json
import socket
import struct
import time
from dataclasses import dataclass
from .client import FusionCameraClient, Frame, CameraError, _exact, MAX_FRAME

@dataclass(frozen=True)
class StreamEvent:
    metadata: dict
    data: bytes = b''

    def frame(self):
        if self.metadata['type'] != 'image':
            raise ValueError('This event is not an image')
        return Frame(self.metadata, self.data)

class FusionStreamClient(FusionCameraClient):
    def recover_camera(self):
        """Request checked recovery of a cleaned-up camera gap; no robot commands."""
        return self._request('recover')[0]

    def clock_sync(self, samples=5):
        """Estimate clock offset without changing either clock. RTT is a bound,
        not a hardware synchronization guarantee. Only available while idle.
        """
        rows=[]
        for _ in range(samples):
            request=json.dumps({'method':'clock','token':self.token},separators=(',',':')).encode()
            with socket.create_connection((self.host,self.port),timeout=self.timeout) as raw:
                with self.context.wrap_socket(raw,server_hostname=self.host) as sock:
                    # Exclude asymmetric TLS handshake time from the RTT sample.
                    start=time.monotonic_ns()
                    sock.sendall(struct.pack('!I',len(request))+request)
                    n=struct.unpack('!I',_exact(sock,4))[0]
                    if not 1<=n<=8192:raise ValueError('Invalid clock reply')
                    meta=json.loads(_exact(sock,n))
                    end=time.monotonic_ns()
                    if not meta.get('ok') or meta.get('bytes')!=0:raise CameraError(meta)
            rows.append({'offset_ns':meta['controller_monotonic_ns']-(start+end)//2,
                         'rtt_ns':end-start,'uncertainty_ns':(end-start)//2,'boot_id':meta['boot_id'],
                         'pc_midpoint_ns':(start+end)//2})
        best=min(rows,key=lambda r:r['rtt_ns'])
        return dict(best,measurements=rows,method='minimum_RTT_midpoint_estimate')

    def stream(self, seconds=60, hz=30, joint_hz=100, mode='stationary', camera_ids=None):
        """Yield start/image/joints/health/end events. Closing iterator stops capture.

        Joint order: left 1..7, right 1..7. Joint sampling defaults to 100 Hz; image
        timestamps and shared-state read times use FUSION CLOCK_MONOTONIC.
        Safe validation mode rejects observed joint speeds above 1 degree/s.
        """
        if type(seconds) is not int or not 1 <= seconds <= 600 or hz != 30:
            raise ValueError('seconds must be 1..600 and hz must be 30')
        if type(joint_hz) is not int or joint_hz not in (30,100):
            raise ValueError('joint_hz must be 30 or 100')
        if mode not in ('stationary', 'teleop_observation'):
            raise ValueError('Unknown observation mode')
        if self.closed: raise RuntimeError('Client is closed')
        if getattr(self, '_stream_socket', None) is not None:
            raise RuntimeError('Only one active stream per client')
        request_data = {'method':'stream','token':self.token,'seconds':seconds,'hz':hz,
                        'joint_hz':joint_hz,'observation_mode':mode}
        if camera_ids is not None:
            if (not isinstance(camera_ids, (list, tuple)) or not camera_ids
                    or any(c not in ('usb-1','usb-2','usb-4') for c in camera_ids)
                    or len(set(camera_ids)) != len(camera_ids)):
                raise ValueError('Invalid camera selection')
            request_data['camera_ids'] = list(camera_ids)
        request = json.dumps(request_data,separators=(',',':')).encode()
        self._stop_requested = False
        finished = False
        with socket.create_connection((self.host,self.port),timeout=self.timeout) as raw:
            with self.context.wrap_socket(raw,server_hostname=self.host) as sock:
                sock.settimeout(15)
                sock.sendall(struct.pack('!I',len(request))+request)
                self._stream_socket = sock
                try:
                    while True:
                        n = struct.unpack('!I',_exact(sock,4))[0]
                        if not 1 <= n <= 262144: raise ValueError('Invalid stream header length')
                        meta = json.loads(_exact(sock,n))
                        size = meta.get('bytes')
                        if type(size) is not int or not 0 <= size <= MAX_FRAME:
                            raise ValueError('Invalid stream payload length')
                        body = _exact(sock,size)
                        meta['pc_recv_ns'] = time.time_ns()
                        meta['pc_recv_monotonic_ns'] = time.monotonic_ns()
                        if meta.get('type') == 'end':
                            finished = True
                            yield StreamEvent(meta,body)
                            if not meta.get('ok'): raise CameraError(meta)
                            break
                        if not meta.get('ok'): raise CameraError(meta)
                        if meta.get('type') not in ('start','image','joints','health'):
                            raise ValueError('Unknown stream event')
                        yield StreamEvent(meta,body)
                finally:
                    if not finished:
                        try: sock.sendall(b'S')
                        except OSError: pass
                    self._stream_socket = None

    def request_stop(self):
        """Ask for an audited normal end; keep reading until the end message."""
        sock = getattr(self, '_stream_socket', None)
        if sock is not None and not getattr(self, '_stop_requested', False):
            sock.sendall(b'S')
            self._stop_requested = True

    def close(self):
        sock = getattr(self, '_stream_socket', None)
        if sock is not None:
            try: sock.sendall(b'S')
            except OSError: pass
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            sock.close()
            self._stream_socket = None
        super().close()
