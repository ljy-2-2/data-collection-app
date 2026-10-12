"""Explicit, bounded 60-second hardware test. Requires the prepared remote tree."""
import json
from pathlib import Path
import subprocess
import sys
import time

base = Path(__file__).resolve().parent
run = base / 'results' / time.strftime('%Y%m%d-%H%M%S')
run.mkdir(parents=True, exist_ok=False)
print(run, flush=True)
ssh = ['ssh','-i','/home/yeap/.ssh/id_ed25519_192_168_1_190',
       '-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
       '-o','ConnectTimeout=20','-o','ConnectionAttempts=1',
       '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2','root@192.168.1.190']
remote_path='/tmp/linux-3.14.18-ipipe-build/fusion-three-mjpeg30b-20260920/run_remote.sh'
with open(base/'run_remote.sh','rb') as source, open(run/'remote.log','wb') as log, open(run/'receive.log','wb') as received:
    remote=subprocess.Popen(ssh+['cat > '+remote_path+' && nice -n 19 taskset -c 3 sh '+remote_path], stdin=source, stdout=subprocess.PIPE, stderr=log)
    receiver=subprocess.Popen([sys.executable,str(base/'receive.py'),'--seconds','60','--format','mjpeg','--width','1024','--height','768','--decode','--output',str(run/'data')],stdin=remote.stdout,stdout=received,stderr=subprocess.STDOUT)
    remote.stdout.close()
    timed_out=False
    try:
        rc_receiver=receiver.wait(timeout=105)
        rc_remote=remote.wait(timeout=20)
    except subprocess.TimeoutExpired:
        timed_out=True
        receiver.terminate()
        remote.terminate()
        for proc in [receiver,remote]:
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        rc_receiver,rc_remote=receiver.returncode,remote.returncode
status={'remote_exit':rc_remote,'receiver_exit':rc_receiver,'coordinator_timeout':timed_out}
pipeline = None
for line in (run/'remote.log').read_text(errors='replace').splitlines():
    try: event = json.loads(line)
    except ValueError: continue
    if event.get('queue_event') == 'final': pipeline = event
if pipeline is not None:
    (run/'pipeline-stats.json').write_text(json.dumps(pipeline,indent=2)+'\n')
    status['pipeline_counters_balanced'] = all(
        pipeline['captured'][i] == sum(pipeline[k][i] for k in
          ('sent','overflow_dropped','shutdown_dropped','failed_frames'))
        and pipeline['pending'][i] == pipeline['in_flight'][i] == 0 for i in range(3))
    if not status['pipeline_counters_balanced']: status['accounting_error'] = True
else:
    status['pipeline_final_stats_missing'] = True
(run/'status.json').write_text(json.dumps(status,indent=2)+'\n')
print(json.dumps(status),flush=True)
sys.exit(0 if rc_remote==0 and rc_receiver==0 and not timed_out and status.get('pipeline_counters_balanced',False) else 1)
