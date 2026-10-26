#!/usr/bin/env python3
"""Read-only export from the local platform; never imports robot code.

Docker is used only to read the platform database and existing object bytes.
The manifest freezes UI row numbers into immutable episode IDs.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess

QUERY = '''
import json,os,sys
from sqlalchemy import create_engine,text
limits=json.load(sys.stdin)
with create_engine(os.environ['DATABASE_URL']).connect() as c:
 c.execute(text('SET TRANSACTION READ ONLY'))
 rows=c.execute(text("SELECT id,task,source,created_at,duration_s,storage_keys,extra FROM episodes WHERE deleted_at IS NULL ORDER BY created_at DESC,id DESC OFFSET :offset LIMIT :limit"),dict(offset=limits['first']-1,limit=limits['last']-limits['first']+1)).mappings().all()
 result=[]
 for i,r in enumerate(rows,limits['first']):
  result.append(dict(row=i,id=str(r['id']),task=r['task'],source=r['source'],created_at=str(r['created_at']),duration_s=r['duration_s'],folder=r['extra'].get('import_folder'),storage={k:r['storage_keys'][k] for k in ('mcap','meta','metadata') if k in r['storage_keys']},platform_check=r['extra'].get('data_check',{}).get('report',{}).get('data_status')))
 print(json.dumps(result,ensure_ascii=False))
'''
READ = '''
import json,sys
from app.services.storage import open_object
key=json.load(sys.stdin)['key']
with open_object(key) as response:
 while chunk:=response.read(1024*1024):sys.stdout.buffer.write(chunk)
'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--first',type=int,default=4);p.add_argument('--last',type=int,default=94)
    p.add_argument('--container',default='data-platform-cloud-api-1')
    args=p.parse_args()
    if not 1<=args.first<=args.last:p.error('Invalid inclusive row range')
    args.output.mkdir(parents=True,exist_ok=True)
    manifest_path=args.output/'manifest.json'
    command=['docker','exec','-i',args.container,'python','-c']
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text())
        if manifest['row_range']!=[args.first,args.last]:p.error('Existing manifest has another row range')
    else:
        rows=json.loads(subprocess.check_output(command+[QUERY],input=json.dumps(dict(first=args.first,last=args.last)).encode(),timeout=30))
        if len(rows)!=args.last-args.first+1 or any('mcap' not in r['storage'] or 'UMI' not in (r['task'] or '').upper() for r in rows):
            raise ValueError('Selected rows are not the requested complete UMI MCAP range; no substitution')
        manifest=dict(version=1,source='local data-platform-cloud',selection='all active episodes, created_at DESC, id DESC',row_range=[args.first,args.last],episodes=rows)
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')

    def download(row):
        folder=args.output/row['id'];folder.mkdir(exist_ok=True)
        hashes={}
        for kind,key in row['storage'].items():
            name={'mcap':'signals.mcap','meta':'meta.json','metadata':'metadata.yaml'}[kind]
            dest=folder/name
            if not dest.is_file():
                temporary=dest.with_suffix(dest.suffix+'.part')
                with temporary.open('wb') as out:
                    subprocess.run(command+[READ],input=json.dumps(dict(key=key)).encode(),stdout=out,check=True,timeout=120)
                temporary.replace(dest)
            hashes[name]=hashlib.sha256(dest.read_bytes()).hexdigest()
            expected=row.get('files',{}).get(name)
            if expected and hashes[name]!=expected:
                raise ValueError(f"Frozen source checksum changed: row {row['row']} / {name}")
        return dict(**row,files=hashes)
    with ThreadPoolExecutor(max_workers=3) as pool:
        completed=[]
        for row in pool.map(download,manifest['episodes']):
            completed.append(row)
            print(f"{len(completed)}/{len(manifest['episodes'])} row={row['row']} {row['folder']}",flush=True)
    manifest['episodes']=completed
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
