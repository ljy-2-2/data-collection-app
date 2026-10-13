"""Decode every saved JPEG on the laptop and preserve original frame indices."""
import csv
import io
import json
from pathlib import Path
import sys
import time
from PIL import Image

p=Path(sys.argv[1])
rows=list(csv.DictReader(open(p/'frames.csv')))
counts=[sum(int(r['camera'])==i for r in rows) for i in range(3)]
decoded=[0,0,0]; seen=[0,0,0]; errors=[]
handles=[open(p/('usb-%d.mjpeg'%port),'rb') for port in (1,2,4)]
start=time.monotonic()
try:
    for row in rows:
        cam=int(row['camera']); f=handles[cam]
        f.seek(int(row['file_offset'])); data=f.read(int(row['bytes']))
        try:
            if len(data)!=int(row['bytes']): raise ValueError('short frame')
            with Image.open(io.BytesIO(data)) as im:
                if im.format!='JPEG' or im.size!=(int(row.get('width',640)),int(row.get('height',480))): raise ValueError('wrong format or size')
                im.load()  # Forces full decode; truncated images are not enabled.
                if seen[cam] in (counts[cam]//2,counts[cam]-1):
                    label='middle' if seen[cam]==counts[cam]//2 else 'last'
                    im.convert('RGB').save(p/('usb-%d-%s.png'%((1,2,4)[cam],label)))
            decoded[cam]+=1
        except Exception as exc:
            errors.append({'camera':cam,'sequence':row['sequence'],'error':str(exc)})
        seen[cam]+=1
finally:
    for f in handles: f.close()
result={'frames':counts,'decoded':decoded,'errors':errors,'decode_seconds':time.monotonic()-start,
        'location':'laptop; offline full decoding, not controller compression'}
(p/'decode-summary.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
sys.exit(1 if errors or any(n==0 for n in counts) else 0)
