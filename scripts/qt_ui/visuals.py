"""Native Qt camera/plots and complete articulated Tianji CAD geometry."""
from __future__ import annotations
import json
import math
import re
from pathlib import Path
import numpy as np
from .grippers import endpoints, closure
from PySide6.QtCore import QObject,Property,Signal,Slot,QTimer,QUrl,QRectF,QPointF,Qt,QByteArray,QBuffer,QIODevice,QSize,QRunnable,QThreadPool
from PySide6.QtGui import QImage,QImageReader,QPainter,QPen,QColor,QFont,QPainterPath,QVector3D,QQuaternion,QMatrix3x3,QRadialGradient
from PySide6.QtNetwork import QNetworkAccessManager,QNetworkRequest,QNetworkReply
from PySide6.QtQuick import QQuickPaintedItem
from PySide6.QtQuick3D import QQuick3DGeometry


def rotation(axis,angle):
    x,y,z=np.asarray(axis)/ (np.linalg.norm(axis) or 1);c,s,t=math.cos(angle),math.sin(angle),1-math.cos(angle)
    return np.array([[t*x*x+c,t*x*y-s*z,t*x*z+s*y,0],[t*x*y+s*z,t*y*y+c,t*y*z-s*x,0],[t*x*z-s*y,t*y*z+s*x,t*z*z+c,0],[0,0,0,1]],dtype=float)


def transform(xyz,rpy=(0,0,0)):
    out=rotation((0,0,1),rpy[2])@rotation((0,1,0),rpy[1])@rotation((1,0,0),rpy[0]);out[:3,3]=xyz;return out


def quat(matrix):return QQuaternion.fromRotationMatrix(QMatrix3x3(matrix[:3,:3].reshape(-1).tolist()))


class MeshGeometry(QQuick3DGeometry):
    def __init__(self,mesh):
        super().__init__()
        vertices=np.asarray(mesh['vertices'],dtype=np.float32)[np.asarray(mesh['faces'],dtype=np.int32)]
        normals=np.cross(vertices[:,1]-vertices[:,0],vertices[:,2]-vertices[:,0]);normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-9)
        corner_normals=(np.asarray(mesh['normals'],dtype=np.float32)[np.asarray(mesh['normal_indices'],dtype=np.int32)]
                        if 'normals' in mesh and 'normal_indices' in mesh else np.repeat(normals[:,None,:],3,axis=1))
        data=np.concatenate((vertices,corner_normals),axis=2).astype(np.float32).reshape(-1,6)
        self.setVertexData(QByteArray(data.tobytes()));self.setStride(24)
        self.setPrimitiveType(QQuick3DGeometry.PrimitiveType.Triangles)
        self.addAttribute(QQuick3DGeometry.Attribute.Semantic.PositionSemantic,0,QQuick3DGeometry.Attribute.ComponentType.F32Type)
        self.addAttribute(QQuick3DGeometry.Attribute.Semantic.NormalSemantic,12,QQuick3DGeometry.Attribute.ComponentType.F32Type)
        self.setBounds(QVector3D(*vertices.min(axis=(0,1)).tolist()),QVector3D(*vertices.max(axis=(0,1)).tolist()))


class ModelPart(QObject):
    moved=Signal()
    meshChanged=Signal()
    def __init__(self,geometry,visual,parent):
        super().__init__(parent);self.geometry_value=geometry;self.visual=visual
        self.matrix=np.eye(4)
    @Property(QObject,notify=meshChanged)
    def mesh(self):return self.geometry_value
    @Property(QVector3D,constant=True)
    def size(self):return QVector3D(*self.visual['scale'])
    @Property(QVector3D,constant=True)
    def tint(self):return QVector3D(*self.visual['color'][:3])
    @Property(QVector3D,notify=moved)
    def position(self):return QVector3D(*self.matrix[:3,3].tolist())
    @Property(QQuaternion,notify=moved)
    def orientation(self):return quat(self.matrix)
    def update_matrix(self,matrix):
        if not np.allclose(matrix,self.matrix,atol=1e-7):self.matrix=matrix;self.moved.emit()


class RobotScene(QObject):
    cameraChanged=Signal()
    default_zoom=1.
    default_yaw=math.pi
    def __init__(self,root,parent=None,*,load_model=True,model_file="tianji-model.json"):
        super().__init__(parent)
        self.calibration=endpoints(root)
        self.model=json.loads((root/'public'/model_file).read_text()) if load_model else dict(meshes={},links=[],joints=[])
        self.geometries={key:MeshGeometry(mesh) for key,mesh in self.model['meshes'].items()}
        for geometry in self.geometries.values():geometry.setParent(self)
        self._view_prepared=False
        self.parts_value=[];self.by_link=[]
        for link in self.model['links']:
            for visual in link['visuals']:
                part=ModelPart(self.geometries[visual['mesh']],visual,self);self.parts_value.append(part);self.by_link.append((link['name'],part))
        self.yaw=self.default_yaw;self.pitch=0.;self.zoom=self.default_zoom;self.pan_x=0.;self.pan_y=0.
        self.camera_matrix=np.eye(4);self._pose_angles=None;self.update_camera();self.update_pose({})
    @Property('QVariantList',constant=True)
    def parts(self):return self.parts_value
    @Slot()
    def prepareView(self):
        # A destroyed View3D releases its render nodes. Reusing the same
        # QQuick3DGeometry in another view can retain stale GPU resources even
        # though the Python/QObject remains valid. Bind fresh geometries while
        # keeping the scene's measured transforms and camera unchanged.
        if self._view_prepared:
            previous=self.geometries
            self.geometries={key:MeshGeometry(mesh) for key,mesh in self.model['meshes'].items()}
            for geometry in self.geometries.values():geometry.setParent(self)
            for part in self.parts_value:
                part.geometry_value=self.geometries[part.visual['mesh']]
                part.meshChanged.emit()
            for geometry in previous.values():geometry.deleteLater()
        self._view_prepared=True
    @Property(QVector3D,notify=cameraChanged)
    def cameraPosition(self):return QVector3D(*self.camera_matrix[:3,3].tolist())
    @Property(QQuaternion,notify=cameraChanged)
    def cameraOrientation(self):return quat(self.camera_matrix)
    @Property(float,notify=cameraChanged)
    def zoomValue(self):return self.zoom
    def update_camera(self):
        # At zero orbit angles, view from base -X toward +X with +Z up.
        # Default yaw=pi views the front from +X toward -X.
        upright=np.array([[0,-1,0,0],[0,0,1,0],[-1,0,0,0],[0,0,0,1]])
        view=rotation((1,0,0),self.pitch)@upright@rotation((0,0,1),self.yaw)@transform((0,0,-.85))
        self.camera_matrix=np.linalg.inv(view)@transform((-self.pan_x,self.pan_y,4))
        self.cameraChanged.emit()
    @Slot(float,float)
    def drag(self,dx,dy):
        self.yaw+=dx*.008;self.pitch=max(-.9,min(.9,self.pitch+dy*.006));self.update_camera()
    @Slot(float,float,float,float)
    def pan(self,dx,dy,width,height):
        scale=max(1,min(height*.51,width*.35)*self.zoom)
        self.pan_x+=dx/scale;self.pan_y+=dy/scale;self.update_camera()
    @Slot(float)
    def changeZoom(self,factor):self.zoom=max(.5,min(4.4,self.zoom*factor));self.update_camera()
    @Slot()
    def resetView(self):self.yaw=self.default_yaw;self.pitch=0.;self.zoom=self.default_zoom;self.pan_x=0.;self.pan_y=0.;self.update_camera()
    @Slot('QVariantMap')
    def update_pose(self,status,calibration=None):
        if calibration is not None:self.calibration=calibration
        angles={f'Joint{i+1}_{letter}':math.radians(v) for side,letter in [('left','L'),('right','R')] for i,v in enumerate(status.get('joints',{}).get(side,[])) if math.isfinite(v)}
        for side,letter in [('left','L'),('right','R')]:
            amount=closure(status,side,self.calibration)
            if amount is None:amount=.5
            drive=next((j for j in self.model['joints'] if j['name']==f'gripper_drive_{letter}'),{})
            angles[f'gripper_drive_{letter}']=(1-amount)*float((drive.get('limit') or {}).get('upper',1))
        self.apply_angles(angles)

    def apply_angles(self,angles):
        # Status polling includes camera and upload changes. Avoid recalculating
        # every CAD transform when the actual pose has not changed on the board.
        if angles==self._pose_angles:return
        matrices={self.model.get('root_link','stand'):np.eye(4)};pending=self.model['joints'][:]
        while pending:
            remaining=[]
            for j in pending:
                if j['parent'] not in matrices:remaining.append(j);continue
                angle=angles.get(j['name'],0)
                if j['mimic']:
                    mimic=j['mimic'];angle=angles.get(mimic['joint'],0)*float(mimic['multiplier'])+float(mimic['offset'])
                matrix=matrices[j['parent']]@transform(j['xyz'],j['rpy'])
                if j['type'] in ('revolute','continuous'):matrix=matrix@rotation(j['axis'],angle)
                matrices[j['child']]=matrix
            if len(remaining)==len(pending):raise ValueError('模型关节结构不完整')
            pending=remaining
        for link,part in self.by_link:part.update_matrix(matrices.get(link,np.eye(4))@transform(part.visual['xyz'],part.visual['rpy']))
        self._pose_angles=angles


class KuavoScene(RobotScene):
    """Official display meshes driven only by KUAVO measured joint radians."""
    default_zoom=.95
    def __init__(self,root,parent=None):
        super().__init__(root,parent,model_file='kuavo-model.json')

    @Slot('QVariantMap')
    def update_pose(self,status,calibration=None):
        values=(status.get('telemetry') or {}).get('joints') or {}
        names={joint['name'] for joint in self.model['joints']
               if joint['type'] in ('revolute','continuous') and not joint['name'].startswith('kuavo_tool_')}
        angles=dict(self.model.get('display_joint_defaults', {}))
        angles.update({name:float(value) for name,value in values.items()
                if name in names and type(value) in (int,float) and math.isfinite(value)})
        grippers=(status.get('telemetry') or {}).get('grippers') or {}
        for side,letter in (('left','L'),('right','R')):
            amount=(grippers.get(side) or {}).get('closure_fraction')
            drive=next((j for j in self.model['joints'] if j['name']==f'kuavo_tool_gripper_drive_{letter}'),None)
            if type(amount) in (int,float) and math.isfinite(amount) and drive:
                angles[drive['name']]=(1-max(0.,min(1.,amount)))*float(drive['limit']['upper'])
        self.apply_angles(angles)

class _MjpegPreviewParser:
    """Keep the latest complete preview, honoring multipart JPEG boundaries.

    Camera JPEGs can contain thumbnails with their own SOI/EOI markers. A
    multipart Content-Length describes the whole image, including those bytes.
    Older preview producers omit the length or send bare JPEGs; retain marker
    parsing only for those streams.
    """
    MAX_FRAME_BYTES = 8 * 1024 * 1024

    def __init__(self):
        self.buffer = bytearray()
        self.body_size = None

    def feed(self, data):
        self.buffer.extend(data)
        latest = None
        while True:
            if self.body_size is not None:
                if len(self.buffer) < self.body_size:
                    break
                latest = bytes(self.buffer[:self.body_size])
                del self.buffer[:self.body_size]
                self.body_size = None
                continue

            start = self.buffer.find(b'\xff\xd8')
            header_end = self.buffer.find(b'\r\n\r\n')
            if header_end >= 0 and (start < 0 or header_end < start):
                header = bytes(self.buffer[:header_end])
                lengths = re.findall(rb'(?im)^content-length:[ \t]*([^\r\n]*)', header)
                if lengths:
                    if len(lengths) != 1 or not re.fullmatch(rb'[0-9]+[ \t]*', lengths[0]):
                        raise ValueError('Invalid MJPEG Content-Length')
                    length = int(lengths[0])
                    if not 0 < length <= self.MAX_FRAME_BYTES:
                        raise ValueError('Invalid MJPEG frame size')
                    self.body_size = length
                del self.buffer[:header_end + 4]
                continue

            end = self.buffer.find(b'\xff\xd9', start + 2) if start >= 0 else -1
            if end < 0:
                break
            if end + 2 - start > self.MAX_FRAME_BYTES:
                raise ValueError('Invalid MJPEG frame size')
            latest = bytes(self.buffer[start:end + 2])
            del self.buffer[:end + 2]

        if len(self.buffer) > self.MAX_FRAME_BYTES:
            raise ValueError('Invalid MJPEG buffer size')
        return latest


def decode_preview_image(data, target, *, crop=False):
    """Decode a display copy off the GUI thread; recorded bytes never enter here."""
    encoded=QBuffer();encoded.setData(QByteArray(data));encoded.open(QIODevice.ReadOnly)
    reader=QImageReader(encoded,b'JPEG')
    size=reader.size()
    if target.isValid() and size.isValid():
        fitted=size.scaled(target,Qt.KeepAspectRatioByExpanding if crop else Qt.KeepAspectRatio)
        if fitted.width()<size.width():reader.setScaledSize(fitted)
    return reader.read()


class _PreviewDecodeSignals(QObject):
    ready=Signal(int,QImage)


class _PreviewDecodeJob(QRunnable):
    def __init__(self,sequence,data,target,crop=False):
        super().__init__();self.sequence=sequence;self.data=data;self.target=target;self.crop=crop
        self.signals=_PreviewDecodeSignals()
    def run(self):
        try:image=decode_preview_image(self.data,self.target,crop=self.crop)
        except Exception:image=QImage()
        self.signals.ready.emit(self.sequence,image)


class CameraItem(QQuickPaintedItem):
    sourceChanged=Signal();onlineChanged=Signal();frameChanged=Signal();staleIntervalChanged=Signal();cropToFillChanged=Signal()
    def __init__(self,parent=None):
        super().__init__(parent);self._source='';self._online=False;self.frame=QImage();self.buffer=b'';self.reply=None;self.sequence=0
        self._decode_job=None;self._decode_pending=None;self._crop_to_fill=False
        self.network=QNetworkAccessManager(self);self.retry=QTimer(self);self.retry.setSingleShot(True);self.retry.timeout.connect(self.connect_stream)
        self.stale=QTimer(self);self.stale.setInterval(2500);self.stale.setSingleShot(True);self.stale.timeout.connect(self.stream_stalled)
        self.setAntialiasing(True)
    @Property(bool,notify=cropToFillChanged)
    def cropToFill(self):return self._crop_to_fill
    @cropToFill.setter
    def cropToFill(self,value):
        if self._crop_to_fill!=value:
            self._crop_to_fill=value;self.cropToFillChanged.emit();self.update()
    @Property(int,notify=staleIntervalChanged)
    def staleInterval(self):return self.stale.interval()
    @staleInterval.setter
    def staleInterval(self,value):
        value=max(500,min(int(value),60000))
        if value!=self.stale.interval():
            self.stale.setInterval(value);self.staleIntervalChanged.emit()
    @Property(str,notify=sourceChanged)
    def source(self):return self._source
    @source.setter
    def source(self,value):
        if value==self._source:return
        self.sequence+=1;self._source=value;self.retry.stop();self.stale.stop();self.buffer=b'';self._decode_pending=None
        if self.reply:self.reply.abort();self.reply=None
        self.frame=QImage();self.frameChanged.emit()
        self.offline();self.sourceChanged.emit()
        if value:self.connect_stream()
    @Property(bool,notify=onlineChanged)
    def online(self):return self._online
    @Property(bool,notify=frameChanged)
    def hasFrame(self):return not self.frame.isNull()
    @Property(float,notify=frameChanged)
    def aspectRatio(self):return self.frame.width()/self.frame.height() if not self.frame.isNull() else 0.0
    def offline(self):
        # Retain the decoded frame for recovery; QML decides how to show an
        # offline preview. Teleop hides it over a black background.
        self.update()
        if self._online:self._online=False;self.onlineChanged.emit()
    def stream_stalled(self):
        self.offline()
        if self.reply:self.reply.abort()
    def connect_stream(self):
        if not self._source or self.reply is not None:return
        sequence=self.sequence;request=QNetworkRequest(QUrl(self._source));request.setTransferTimeout(5000)
        reply=self.network.get(request);self.reply=reply;self.buffer=b''
        parser=_MjpegPreviewParser()
        frame_endpoint='/frame' in self._source
        def accept(data):
            ratio=self.window().devicePixelRatio() if self.window() else 1.0
            target=QSize(max(1,round(self.width()*ratio)),max(1,round(self.height()*ratio))) if self.width()>0 and self.height()>0 else QSize()
            # At most one running decode and one newest pending frame per tile.
            # Reading the network never waits for JPEG decode or old frames.
            self._decode_pending=(sequence,data,target,self._crop_to_fill)
            self._decode_next()
        def read():
            if sequence!=self.sequence:return
            latest=None
            # A delayed UI callback may receive many valid HD frames (>8 MB).
            # Drain bounded chunks and parse before limiting an incomplete frame;
            # the previous readAll limit repeatedly aborted healthy HD streams.
            while reply.bytesAvailable():
                data=bytes(reply.read(65536))
                try:
                    if frame_endpoint:self.buffer+=data
                    else:
                        parsed=parser.feed(data)
                        self.buffer=parser.buffer
                        if parsed is not None:latest=parsed
                    if len(self.buffer)>parser.MAX_FRAME_BYTES:
                        raise ValueError('Invalid preview frame size')
                except ValueError:
                    self.buffer=b'';reply.abort();return
            if latest:accept(latest)
        def finished():
            if sequence==self.sequence:
                read()
                if frame_endpoint and reply.error()==QNetworkReply.NoError:accept(self.buffer)
                # Reconnect without blanking a fresh frame; the frame-age timer
                # still declares a real outage after 2.5 s, even with incoming junk.
                self.reply=None
                self.retry.start(80 if frame_endpoint else 600)
            reply.deleteLater()
        reply.readyRead.connect(read);reply.finished.connect(finished)
    def _decode_next(self):
        if self._decode_job is not None or self._decode_pending is None:return
        sequence,data,target,crop=self._decode_pending;self._decode_pending=None
        job=_PreviewDecodeJob(sequence,data,target,crop)
        self._decode_job=job
        job.signals.ready.connect(self._decoded,Qt.QueuedConnection)
        QThreadPool.globalInstance().start(job)
    @Slot(int,QImage)
    def _decoded(self,sequence,image):
        self._decode_job=None
        if sequence==self.sequence and self._source and not image.isNull():
            size_changed=self.frame.size()!=image.size();self.frame=image;self.update();self.stale.start()
            if size_changed:self.frameChanged.emit()
            if not self._online:self._online=True;self.onlineChanged.emit()
        self._decode_next()
    def paint(self,painter):
        if self.frame.isNull() or self.width()<=0 or self.height()<=0:return
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if self._crop_to_fill:
            # Crop only the display source rectangle, preserving geometry.
            # The native camera JPEG and recorded field of view are unchanged.
            aspect=self.width()/self.height()
            width=min(self.frame.width(),self.frame.height()*aspect)
            height=min(self.frame.height(),self.frame.width()/aspect)
            source=QRectF((self.frame.width()-width)/2,(self.frame.height()-height)/2,width,height)
            painter.drawImage(self.boundingRect(),self.frame,source)
            return
        size=self.frame.size();size.scale(int(self.width()),int(self.height()),Qt.KeepAspectRatio)
        painter.drawImage(QRectF((self.width()-size.width())/2,(self.height()-size.height())/2,size.width(),size.height()),self.frame)


class CurveItem(QQuickPaintedItem):
    """UMI encoder plot: retain input radians and render a degree axis."""
    changed=Signal()
    def __init__(self,parent=None):super().__init__(parent);self._values=[];self._dark=False;self._zero_based=False;self.setAntialiasing(True)
    @Property('QVariantList',notify=changed)
    def values(self):return self._values
    @values.setter
    def values(self,value):self._values=value;self.update();self.changed.emit()
    @Property(bool,notify=changed)
    def dark(self):return self._dark
    @dark.setter
    def dark(self,v):self._dark=v;self.update();self.changed.emit()
    @Property(bool,notify=changed)
    def zeroBased(self):return self._zero_based
    @zeroBased.setter
    def zeroBased(self,v):self._zero_based=v;self.update();self.changed.emit()
    def paint(self,p):
        if not self.isVisible():return
        samples=[math.degrees(v) if isinstance(v,(int,float)) and math.isfinite(v) else None for v in self._values]
        w,h=self.width(),self.height();values=[v for v in samples if v is not None]
        lo,hi=(min(values),max(values)) if values else (0,1);pad=max((hi-lo)*.12,math.degrees(.0005));lo-=pad;hi+=pad
        if self._zero_based:lo=max(0,lo);hi=max(hi,lo+1e-9)
        p.setPen(QPen(QColor('#444850' if self._dark else '#cbd0db'),1))
        for frac in (0,.5,1):p.drawLine(QPointF(52,12+frac*(h-32)),QPointF(w-12,12+frac*(h-32)))
        p.setFont(QFont('monospace',7));p.setPen(QColor('#b2b2b2' if self._dark else '#686868'))
        for frac in (0,.5,1):
            rounded=f'{hi-frac*(hi-lo):.2f}'
            p.drawText(QRectF(1,4+frac*(h-32),47,16),Qt.AlignRight,('0.00' if rounded=='-0.00' else rounded)+'°')
        path=QPainterPath();drawing=False
        for i,value in enumerate(samples):
            if value is None:drawing=False;continue
            pos=QPointF(52+i/max(1,len(samples)-1)*(w-64),12+(hi-value)/(hi-lo)*(h-32))
            if drawing:path.lineTo(pos)
            else:path.moveTo(pos)
            drawing=True
        p.setPen(QPen(QColor('#8fb1ff' if self._dark else '#4f6df5'),2,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));p.drawPath(path)


class Backdrop(QQuickPaintedItem):
    loginChanged=Signal()
    _login=True
    @Property(bool,notify=loginChanged)
    def login(self):return self._login
    @login.setter
    def login(self,value):self._login=value;self.update();self.loginChanged.emit()
    darkChanged=Signal()
    def __init__(self,parent=None):super().__init__(parent);self._dark=False
    @Property(bool,notify=darkChanged)
    def dark(self):return self._dark
    @dark.setter
    def dark(self,value):self._dark=value;self.update();self.darkChanged.emit()
    def paint(self,p):
        p.fillRect(self.boundingRect(),QColor('#111111' if self._dark else '#f5f5f5'))
        gradient=QRadialGradient(QPointF(self.width()/2,0) if self._login else QPointF(self.width()*.12,-self.height()*.08),512 if self._login else 480)
        gradient.setColorAt(0,QColor('#b3362f13' if self._dark else '#b3fff7d1'));gradient.setColorAt(1,QColor('transparent'))
        p.fillRect(self.boundingRect(),gradient)


def g2_skeleton(values):
    points={'base_center':(0,0,.12),'base_fl':(.27,.22,.12),'base_fr':(.27,-.22,.12),'base_bl':(-.27,.22,.12),'base_br':(-.27,-.22,.12),'wheel_fl':(.27,.27,.05),'wheel_fr':(.27,-.27,.05),'wheel_bl':(-.27,.27,.05),'wheel_br':(-.27,-.27,.05),'body_root':(0,0,.12)}
    links=[(a,b,'base') for a,b in [('base_fl','base_fr'),('base_fr','base_br'),('base_br','base_bl'),('base_bl','base_fl'),('base_fl','wheel_fl'),('base_fr','wheel_fr'),('base_bl','wheel_bl'),('base_br','wheel_br'),('base_center','base_fl'),('base_center','base_fr'),('base_center','base_bl'),('base_center','base_br')]]
    def put(parent,matrix,name,xyz,rpy,axis,group):
        angle=values.get(name,0);angle=angle if isinstance(angle,(int,float)) and math.isfinite(angle) else 0
        matrix=matrix@transform(xyz,rpy)@rotation(axis,angle);points[name]=matrix[:3,3];links.append((parent,name,group));return matrix
    body=transform((0,0,.12));prior='body_root'
    for i,(xyz,axis) in enumerate([((.102,0,.144),(0,1,0)),((0,0,.36),(0,1,0)),((0,0,.38),(0,1,0)),((0,0,0),(1,0,0)),((0,0,.149),(0,0,1))]):
        name=f'idx0{i+1}_body_joint{i+1}';body=put(prior,body,name,xyz,(0,0,0),axis,'body');prior=name
    head=body
    for i,(xyz,axis) in enumerate([((-.013,0,.496),(0,0,1)),((.03335,0,.07),(1,0,0)),((-.03035,0,0),(0,1,0))]):
        name=f'idx1{i+1}_head_joint{i+1}';head=put(prior,head,name,xyz,(0,0,0),axis,'head');prior=name
    points['head_tip']=(head@transform((0,0,.15)))[:3,3];links.append((prior,'head_tip','head'))
    p=math.pi;arm=body@transform((0,0,.3085),(-p/2,0,0))
    origins=[((0,0,.069),(0,0,0)),((0,0,.1745),(p/2,0,0)),((0,0,0),(-p/2,0,0)),((.018,0,.287),(p/2,0,0)),((-.018,0,0),(-p/2,0,0)),((0,0,.314),(p/2,0,0)),((0,0,0),(p/2,0,p/2))]
    for side,prefix,letter in [('left','2','l'),('right','6','r')]:
        matrix=arm;prior='idx05_body_joint5'
        for i,(xyz,rpy) in enumerate(origins):
            if side=='right' and i==0:xyz,rpy=(0,0,-.069),(p,0,0)
            name=f'idx{prefix}{i+1}_arm_{letter}_joint{i+1}';matrix=put(prior,matrix,name,xyz,rpy,(0,0,1),side);prior=name
        end=side+'_end_effector';points[end]=(matrix@transform((.088,0,0)))[:3,3];links.append((prior,end,side))
    return [(points[a],points[b],group) for a,b,group in links]


class G2View(QQuickPaintedItem):
    changed=Signal()
    def __init__(self,parent=None):
        super().__init__(parent);self._values={};self._dark=False;self._live=False;self.yaw=-.65;self.pitch=.28;self.zoom=1.;self.pan_x=0.;self.pan_y=0.;self.setAntialiasing(True)
    @Property('QVariantMap',notify=changed)
    def values(self):return self._values
    @values.setter
    def values(self,value):self._values=value;self.update();self.changed.emit()
    @Property(bool,notify=changed)
    def dark(self):return self._dark
    @dark.setter
    def dark(self,v):self._dark=v;self.update();self.changed.emit()
    @Property(bool,notify=changed)
    def live(self):return self._live
    @live.setter
    def live(self,v):self._live=v;self.update();self.changed.emit()
    @Slot(float,float)
    def drag(self,dx,dy):self.yaw+=dx*.008;self.pitch=max(-.5,min(.8,self.pitch+dy*.006));self.update()
    @Slot(float)
    def changeZoom(self,factor):self.zoom=max(.65,min(1.65,self.zoom*factor));self.update()
    @Slot(float,float,float,float)
    def pan(self,dx,dy,width,height):self.pan_x+=dx;self.pan_y+=dy;self.update()
    @Slot()
    def resetView(self):self.yaw=-.65;self.pitch=.28;self.zoom=1.;self.pan_x=0.;self.pan_y=0.;self.update()
    def paint(self,p):
        if not self.isVisible():return
        colors=dict(zip(('base','body','head','left','right'),['#b8c5d3','#8fc7f2','#c1b6ea','#79d8c6','#f2b28c'] if self._dark else ['#8d9cad','#72a7d1','#9b91c8','#59bbaa','#df9874']))
        cy,sy,cp,sp=math.cos(self.yaw),math.sin(self.yaw),math.cos(self.pitch),math.sin(self.pitch)
        def project(point):
            x,y,z=point;x1=cy*x-sy*y;y1=sy*x+cy*y
            return QPointF(self.width()/2+self.pan_x+x1*155*self.zoom,self.height()*.86+self.pan_y-(sp*y1+cp*z)*155*self.zoom),cp*y1-sp*z
        p.setPen(QPen(QColor('#1fCDD8E6' if self._dark else '#245c6c80'),1))
        for i in range(-4,5):
            for a,b in [((i*.2,-.8,0),(i*.2,.8,0)),((-.8,i*.2,0),(.8,i*.2,0))]:p.drawLine(project(a)[0],project(b)[0])
        rendered=sorted([(project(a),project(b),group) for a,b,group in g2_skeleton(self._values)],key=lambda v:v[0][1]+v[1][1])
        for a,b,group in rendered:
            p.setPen(QPen(QColor('#7a0a0f18' if self._dark else '#dbffffff'),6 if group=='base' else 9,Qt.SolidLine,Qt.RoundCap));p.drawLine(a[0],b[0])
        for a,b,group in rendered:
            p.setPen(QPen(QColor(colors[group]),3.5 if group=='base' else 5.5,Qt.SolidLine,Qt.RoundCap));p.drawLine(a[0],b[0])
        for _,b,group in rendered:
            p.setPen(QPen(QColor('#8f0f1622' if self._dark else '#e6ffffff'),1.5));p.setBrush(QColor(colors[group]));r=3 if group=='base' else 4.5;p.drawEllipse(b[0],r,r)
        color=QColor(('#79d8ad' if self._dark else '#3c9f78') if self._live else ('#aebdce' if self._dark else '#76879a'))
        p.setPen(Qt.NoPen);p.setBrush(color);p.drawEllipse(QPointF(18,19),4,4);p.setPen(color);font=QFont('Ubuntu');font.setPixelSize(11);font.setWeight(QFont.DemiBold);p.setFont(font);p.drawText(QPointF(29,23),'LIVE' if self._live else 'TARGET')
