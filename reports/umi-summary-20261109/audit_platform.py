import io,json,struct,hashlib,re,sys
import numpy as np
from mcap.reader import make_reader
from app.db import SessionLocal
from app.models import Episode
from sqlalchemy import select
from app.services.storage import get_object_bytes,get_minio_client
class CDR:
    def __init__(self, data):
        if len(data) < 4 or data[:2] not in (b'\x00\x01', b'\x00\x00'):
            raise ValueError('Only ROS2 plain CDR is supported')
        self.data, self.pos = data, 4
        self.endian = '<' if data[1] else '>'

    def read(self, code, align):
        self.pos += (-(self.pos - 4)) % align
        value = struct.unpack_from(self.endian + code, self.data, self.pos)[0]
        self.pos += struct.calcsize(code)
        return value

    def string(self):
        n = self.read('I', 4)
        if n < 1 or self.pos + n > len(self.data):
            raise ValueError('Invalid CDR string')
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b.rstrip(b'\0').decode('utf-8')

    def header(self):
        sec, nano = self.read('i', 4), self.read('I', 4)
        return sec * 1_000_000_000 + nano, self.string()


def stats(a):
 return dict(zip(('min','p50','p95','p99','max'),map(float,np.percentile(a,[0,50,95,99,100])))) if len(a) else {}
def timing(t):
 a=np.array(t,dtype=np.int64);d=np.diff(a)
 return dict(count=len(a),span_s=int(a[-1]-a[0])/1e9,hz=(len(a)-1)*1e9/int(a[-1]-a[0]),interval_ms=stats(d/1e6),nonincreasing=int(sum(d<=0)),gaps_over100ms=int(sum(d>100000000)))
results=[]
with SessionLocal() as session:
 rows=list(session.scalars(select(Episode).where(Episode.deleted_at.is_(None))))
 items=sorted([e for e in rows if re.match(r'UMI_数据采集_\d+_',str((e.extra or {}).get('import_folder','')))],key=lambda e:e.extra['import_folder'])
 for e in items:
  k=e.storage_keys;m=json.loads(get_object_bytes(k['video_manifest']))
  out=dict(id=str(e.id),folder=e.extra['import_folder'],state=m['state'],cameras={},signals={},crf=m.get('crf'))
  bucket,prefix=k['raw'].split('/',1)
  objects=list(get_minio_client().list_objects(bucket,prefix=prefix,recursive=True))
  out['storage_files']={o.object_name.removeprefix(prefix):o.size for o in objects};out['storage_bytes']=sum(o.size for o in objects)
  for c in m['cameras']:
   side=c['file'].split('.')[0];frames=c['frames'];ts=[CDR(bytes.fromhex(f['cdr_prefix'])).header()[0] for f in frames]
   out['cameras'][side]=dict(timing(ts),size=c['size'],manifest_fps=c['fps'],declared=c['frame_count'],log_minus_header_ms=stats([(f['log_time']-t)/1e6 for f,t in zip(frames,ts)]))
  raw=get_object_bytes(k['mcap']);assert hashlib.sha256(raw).hexdigest()==m['signals']['sha256']
  streams={};device={};seq={};valid={};joints=[];names=set()
  topics=['/pico/controller/left/state','/pico/controller/right/state','/pico/hmd/state','/joint_states']
  for schema,ch,msg in make_reader(io.BytesIO(raw),validate_crcs=True).iter_messages(topics=topics,log_time_order=False):
   r=CDR(msg.data);stamp,_=r.header();streams.setdefault(ch.topic,[]).append((stamp,msg.log_time))
   if ch.topic.startswith('/pico/'):
    dev,mono,sq=r.read('Q',8),r.read('Q',8),r.read('I',4);r.string();v=[r.read('d',8) for _ in range(7)]
    device.setdefault(ch.topic,[]).append((dev,mono));seq.setdefault(ch.topic,[]).append(sq)
    if '/controller/' in ch.topic:valid.setdefault(ch.topic,[]).append(bool(r.read('B',1)) and bool(r.read('B',1)))
    assert np.isfinite(v).all()
   else:
    n=[r.string() for _ in range(r.read('I',4))];p=[r.read('d',8) for _ in range(r.read('I',4))];assert len(n)==len(p);names.update(n);joints.append(p)
  for topic,rows in streams.items():
   t=[v[0] for v in rows];q=dict(timing(t),log_minus_header_ms=stats([(v[1]-v[0])/1e6 for v in rows]))
   if topic in device:
    dev=[v[0] for v in device[topic]];q['device_timing']=timing(dev);diff=np.diff(seq[topic]);q.update(sequence_gaps=int(sum(np.maximum(diff-1,0))),sequence_nonincreasing=int(sum(diff<=0)),receive_device_offset_spread_ms=float(np.ptp([v[1]-v[0] for v in device[topic]])/1e6))
    if topic in valid:q['invalid_tracking']=sum(not v for v in valid[topic])
   out['signals'][topic]=q
  ja=np.array(joints);out['gripper']=dict(names=sorted(names),invalid_values=int(sum(~np.isfinite(ja).ravel())),min_rad=ja.min(axis=0).tolist(),max_rad=ja.max(axis=0).tolist(),both_positions_repeat_transitions=int(np.sum(np.all(np.diff(ja,axis=0)==0,axis=1))),independent_encoder_sample_timestamps_present=False)
  results.append(out)
  if len(results)%20==0:print('audited',len(results),file=sys.stderr,flush=True)
print(json.dumps(dict(scope='All 98 existing UMI numbered captures; read-only raw metadata and signals; no new capture',episodes=results),ensure_ascii=False))
