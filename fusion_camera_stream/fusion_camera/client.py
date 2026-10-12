"""Laptop client: no SSH, no robot SDK calls. Python 3.8+."""
import hashlib
import io
import json
from pathlib import Path
import socket
import ssl
import struct
import time
from dataclasses import dataclass

MAX_FRAME = 2 * 1024 * 1024

class CameraError(RuntimeError):
    def __init__(self, response):
        self.response = response
        super().__init__(response.get('error', 'camera_error') + ': ' + response.get('detail', ''))

def _exact(sock, length):
    pieces = []
    while length:
        part = sock.recv(min(length, 65536))
        if not part:
            raise ConnectionError('Camera service closed an incomplete response')
        pieces.append(part)
        length -= len(part)
    return b''.join(pieces)

@dataclass(frozen=True)
class Frame:
    metadata: dict
    data: bytes

    @property
    def camera_id(self): return self.metadata['camera_id']

    @property
    def format(self): return self.metadata['format']

    def to_pil(self):
        """Decode on laptop only. Pillow; YUYV additionally needs NumPy."""
        from PIL import Image
        if self.format == 'mjpeg':
            image = Image.open(io.BytesIO(self.data))
            if image.size != (self.metadata['width'], self.metadata['height']):
                raise ValueError('JPEG dimensions do not match metadata')
            image.load()
            return image.convert('RGB')
        if self.format != 'yuyv': raise ValueError('Unsupported pixel format')
        matrix = self.metadata.get('yuv_matrix', 'bt601')
        if matrix not in ('bt601', 'bt709'):
            raise ValueError('Unsupported YUYV color matrix: '+str(matrix))
        if self.metadata.get('yuv_range', 'limited') != 'limited':
            raise ValueError('Unsupported YUYV quantization range')
        import numpy as np
        width, height = self.metadata['width'], self.metadata['height']
        if len(self.data) != width*height*2 or width%2:
            raise ValueError('Invalid YUYV frame size')
        pairs = np.frombuffer(self.data,dtype=np.uint8).reshape(height,width//2,4).astype(np.float32)
        y = np.empty((height,width),dtype=np.float32)
        y[:,0::2] = pairs[:,:,0]; y[:,1::2] = pairs[:,:,2]
        u = np.repeat(pairs[:,:,1]-128,2,axis=1)
        v = np.repeat(pairs[:,:,3]-128,2,axis=1)
        c = 1.16438356*(y-16)
        if matrix == 'bt709':
            kr, kb = .2126, .0722
            kg = 1-kr-kb
            chroma = 255/224
            rgb = np.stack((c+2*(1-kr)*chroma*v,
                            c-2*kb*(1-kb)/kg*chroma*u-2*kr*(1-kr)/kg*chroma*v,
                            c+2*(1-kb)*chroma*u),axis=-1)
        else:
            rgb = np.stack((c+1.59602678*v,c-0.39176229*u-0.81296765*v,c+2.01723214*u),axis=-1)
        return Image.fromarray(np.clip(np.rint(rgb),0,255).astype(np.uint8),'RGB')

    def to_numpy(self):
        """RGB array, not OpenCV's BGR ordering."""
        import numpy as np
        return np.asarray(self.to_pil())

    def save(self, path):
        path = Path(path)
        if self.format == 'mjpeg' and path.suffix.lower() in ('.jpg','.jpeg'):
            path.write_bytes(self.data)
        elif path.suffix.lower() == '.yuyv' and self.format == 'yuyv':
            path.write_bytes(self.data)
        else:
            self.to_pil().save(path)

class FusionCameraClient:
    def __init__(self, host='192.168.1.190', *, credentials=None, timeout=12):
        credentials = Path(credentials) if credentials else Path(__file__).resolve().parent.parent/'client.json'
        settings = json.loads(credentials.read_text())
        self.host = host
        self.port = settings.get('port',57491)
        self.token = settings['token']
        self.timeout = timeout
        certificate = credentials.parent/settings['certificate']
        self.context = ssl.create_default_context(cafile=str(certificate))
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.set_ciphers('ECDHE-RSA-AES128-GCM-SHA256:AES128-GCM-SHA256')
        self.closed = False

    def _request(self, method, **fields):
        if self.closed: raise RuntimeError('Client is closed')
        request = json.dumps(dict(fields,method=method,token=self.token),separators=(',',':')).encode()
        if len(request)>1024: raise ValueError('Request too large')
        with socket.create_connection((self.host,self.port),timeout=self.timeout) as raw:
            with self.context.wrap_socket(raw,server_hostname=self.host) as sock:
                sock.sendall(struct.pack('!I',len(request))+request)
                header_length = struct.unpack('!I',_exact(sock,4))[0]
                if not 1 <= header_length <= 8192: raise ValueError('Invalid response header')
                metadata = json.loads(_exact(sock,header_length))
                length = metadata.get('bytes')
                if type(length) is not int or not 0 <= length <= MAX_FRAME:
                    raise ValueError('Invalid response size')
                data = _exact(sock,length)
        if not metadata.get('ok'): raise CameraError(metadata)
        if data and hashlib.sha256(data).hexdigest() != metadata.get('sha256'):
            raise ValueError('Image checksum mismatch')
        return metadata,data

    def connect(self):
        """Check the independent camera service; never moves the robot or starts capture."""
        self.closed = False
        return self.status()

    def status(self): return self._request('status')[0]

    def list_cameras(self): return self._request('list')[0]['cameras']

    def read(self, camera_id, *, wait=True):
        """Capture one fresh frame. Rate limits may delay the request, never replay a stale frame."""
        deadline = time.monotonic()+self.timeout
        while True:
            try:
                metadata,data = self._request('snapshot',camera_id=camera_id)
                if metadata.get('camera_id') != camera_id or not data:
                    raise ValueError('Invalid camera response')
                return Frame(metadata,data)
            except CameraError as error:
                if not wait or error.response.get('error')!='rate_limited': raise
                delay = max(0.05,min(float(error.response['retry_after'])+0.05,3.0))
                if time.monotonic()+delay>=deadline: raise
                time.sleep(delay)

    def read_all(self):
        """Serial snapshots, NOT simultaneous/synchronized frames or a continuous stream."""
        cameras = self.list_cameras()
        missing = [c['id'] for c in cameras if not c['available']]
        if missing:
            raise CameraError(dict(error='camera_unavailable',detail=', '.join(missing)))
        return {c['id']:self.read(c['id']) for c in cameras}

    def close(self):
        self.closed = True

    def __enter__(self):
        self.connect(); return self

    def __exit__(self, *args): self.close()
