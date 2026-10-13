import ctypes,json,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from fusion_camera.grippers import ReadOnlyGrippers
from fusion_camera import FusionStreamClient
base=Path(__file__).resolve().parent
run=base/time.strftime('trace-%Y%m%d-%H%M%S');run.mkdir()
ssh=['ssh','-i','/home/yeap/.ssh/id_ed25519_192_168_1_190','-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15','root@192.168.1.190']
audit_cmd="python -B -c 'import sys,json;sys.path.insert(0,\"/mnt/fusion-camera\");from streaming import audit;print(json.dumps(audit()))'"
def audit(name):
    data=subprocess.check_output(ssh+[audit_cmd],timeout=25)
    (run/name).write_bytes(data)
    return json.loads(data)
before=audit('audit-before.json')
err=None; reader=None;count=0
wire=(run/'wire.pcap').open('wb');log=(run/'tcpdump.log').open('wb')
cap=subprocess.Popen(ssh+["timeout -s INT 37 taskset -c 3 nice -n 19 /usr/sbin/tcpdump -p -i any -nn -s 350 -U -w - 'udp and (port 4729 or port 4730) and udp[8:2] = 0x4348'"],stdout=wire,stderr=log)
try:
    deadline=time.monotonic()+25
    while b'listening on' not in (run/'tcpdump.log').read_bytes():
        if cap.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Passive capture did not start')
        time.sleep(.1)
    with ReadOnlyGrippers() as reader, (run/'api-replies.jsonl').open('w',buffering=1) as reply_log, (run/'samples.jsonl').open('w',buffering=1) as sample_log:
        for side,original in list(reader.receivers.items()):
            def receive(buf,ch,side=side,original=original):
                size=original(buf,ch)
                if size:
                    reply_log.write(json.dumps({'pc_ns':time.monotonic_ns(),'side':side,'size':size,'hex':bytes(buf[:max(0,min(size,256))]).hex()})+'\n')
                return size
            reader.receivers[side]=receive
        start=time.monotonic();tick=0;last_temp=-1
        while time.monotonic()-start<30:
            now=time.monotonic();time.sleep(max(0,start+tick/30-now))
            now=time.monotonic();tick=max(tick,int((now-start)*30))
            sample=reader.sample(temperatures=int(now-start)!=last_temp)
            last_temp=int(now-start)
            sample_log.write(json.dumps(sample)+'\n');count+=1;tick+=1
except Exception as e:
    err=str(e)
finally:
    if reader:
        (run/'api-audit.json').write_text(json.dumps({'requests':reader.trace,'unexpected_reply':reader.unexpected_reply,'duplicate_replies':reader.duplicate_replies},indent=2))
    try: cap.wait(timeout=45)
    except subprocess.TimeoutExpired:cap.terminate();cap.wait(timeout=5)
    wire.close();log.close()
    after=audit('audit-after.json')
    checks={k:before[k]==after[k] for k in ['robot_pids','modules','mounts','config_hashes','dmesg_sha256']}
    summary={'run':str(run),'samples':count,'error':err,'capture_exit':cap.returncode,'audit_unchanged':checks}
    (run/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary,ensure_ascii=False))
