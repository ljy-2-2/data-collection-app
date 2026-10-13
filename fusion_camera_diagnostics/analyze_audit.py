"""Read saved controller telemetry; no device access or mutations on FUSION."""
import json
from pathlib import Path
import re
import statistics
import sys

run = Path(sys.argv[1])
audit = run/'audit'
clock_ticks = int((audit/'clock-ticks').read_text()) if (audit/'clock-ticks').exists() else 100
samples = []
for block in re.split(r'^SAMPLE \d+\n', (audit/'monitor.log').read_text(), flags=re.M)[1:]:
    lines = block.splitlines()
    s = {'uptime': float(lines[0].split()[0]), 'cpu': {}, 'xeno': {}, 'processes': {}}
    for line in lines:
        if re.match(r'^cpu\d* ', line):
            t=line.split(); s['cpu'][t[0]]=list(map(int,t[1:9]))
        if line.startswith('MemAvailable:'): s['available_kib']=int(line.split()[1])
        if line.strip().startswith('eth1:'): s['eth1']=list(map(int,line.split(':')[1].split()))
        if 'FX_RtTask' in line or 'RTPSI' in line:
            t=line.split(); s['xeno'][t[-1]]={'msw':int(t[2]),'pf':int(t[4]),'cpu':float(t[6])}
        match = re.match(r'^PROCESS_STAT (\d+) \(([^)]+)\) (.*)$', line)
        if match:
            # Following comm, rest[0] is field 3 (state); utime/stime are fields 14/15.
            t=match[3].split()
            s['processes'][int(match[1])]={'name':match[2], 'ticks':int(t[11])+int(t[12]),'starttime':int(t[19])}
    samples.append(s)
metrics={'samples':len(samples),'cpu_busy_percent':{},'process_cpu_percent_one_core':{}}
for cpu in samples[0]['cpu']:
    values=[]
    for a,b in zip(samples,samples[1:]):
        d=[y-x for x,y in zip(a['cpu'][cpu],b['cpu'][cpu])]
        if sum(d)>0: values.append(100*(sum(d)-d[3]-d[4])/sum(d))
    metrics['cpu_busy_percent'][cpu]={'mean':statistics.mean(values) if values else None,'max':max(values) if values else None}
process_values={}
for a,b in zip(samples,samples[1:]):
    dt=b['uptime']-a['uptime']
    for pid,after in b['processes'].items():
        before=a['processes'].get(pid)
        if before and before['starttime']==after['starttime'] and dt>0:
            key='%s:%d'%(after['name'],pid)
            process_values.setdefault(key,[]).append(100*(after['ticks']-before['ticks'])/clock_ticks/dt)
for key,values in process_values.items():
    metrics['process_cpu_percent_one_core'][key]={'mean':statistics.mean(values),'max':max(values),'intervals':len(values)}
metrics['min_available_MiB']=min(s['available_kib'] for s in samples)/1024
metrics['eth1_mean_tx_MBps']=(samples[-1]['eth1'][8]-samples[0]['eth1'][8])/(samples[-1]['uptime']-samples[0]['uptime'])/1e6 if len(samples)>1 and samples[-1]['uptime']>samples[0]['uptime'] else None
metrics['rt_MSW_PF_zero_all_samples']=all(v['msw']==v['pf']==0 for s in samples for v in s['xeno'].values())
metrics['rt_cpu_percent']={name:{'mean':statistics.mean(s['xeno'][name]['cpu'] for s in samples),'max':max(s['xeno'][name]['cpu'] for s in samples)} for name in ('RTPSI','FX_RtTask')}
metrics['unchanged']={name:(audit/('before.'+name)).read_bytes()==(audit/('after.'+name)).read_bytes() for name in ('dmesg','modules','mounts','config-sha256')}
for phase in ('before','after'):
    metrics[phase+'_robot_pids']={line.split()[1]+':'+line.split()[0]:int(line.split()[0]) for line in (audit/(phase+'.processes')).read_text().splitlines() if any(name in line for name in ('FXProducer','FXPSI','FXTcpFileServer','EtherCAT-OP'))}
metrics['robot_pids_unchanged']=metrics['before_robot_pids']==metrics['after_robot_pids']
(run/'audit-summary.json').write_text(json.dumps(metrics,indent=2)+'\n')
print(json.dumps(metrics,indent=2))
