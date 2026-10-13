#!/usr/bin/env python3
"""Bounded, passive camera/teleoperation timeline. Never starts capture or motion."""
import argparse
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Python 2.7 on the controller. Only proc/sysfs reads and dmesg; no camera API,
# USB control transfers, driver writes, SDK calls or persistent remote files.
REMOTE = r'''
import glob, hashlib, json, os, subprocess, sys, time

def read(path):
    try:
        with open(path, 'rb') as f: return f.read(524288).decode('utf-8', 'replace').strip()
    except IOError: return None

end = time.time() + int(sys.argv[1])
previous_log = None
while time.time() < end:
    started = time.time()
    row = dict(controller_wall_time=started, uptime=read('/proc/uptime'),
               boot_id=read('/proc/sys/kernel/random/boot_id'))
    row['usb'] = {}
    for path in glob.glob('/sys/bus/usb/devices/*'):
        if not os.path.exists(path+'/idVendor'): continue
        fields = ('idVendor','idProduct','devnum','busnum','speed','authorized',
                  'power/control','power/runtime_status')
        row['usb'][os.path.basename(path)] = dict((k,read(path+'/'+k)) for k in fields)
    row['xhci_interrupts'] = [line for line in (read('/proc/interrupts') or '').splitlines() if 'xhci' in line.lower()]
    row['xenomai'] = read('/proc/xenomai/stat')
    row['cpu'] = [line for line in (read('/proc/stat') or '').splitlines() if line.startswith('cpu')]
    row['loadavg'] = read('/proc/loadavg')
    row['processes'] = {}
    for path in glob.glob('/proc/[0-9]*'):
        comm = read(path+'/comm') or ''
        if comm.startswith('capture') or comm in ('FXProducer','FXPSI','EtherCAT-OP'):
            row['processes'][os.path.basename(path)] = dict(comm=comm, stat=read(path+'/stat'))
    p = subprocess.Popen(['dmesg'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    log, error = p.communicate()
    digest = hashlib.sha256(log).hexdigest()
    if p.returncode:
        row['dmesg_error'] = error.decode('utf-8','replace')
    elif digest != previous_log:
        row['dmesg'] = log.decode('utf-8','replace')
        previous_log = digest
    row['dmesg_sha256'] = digest
    print(json.dumps(row, separators=(',', ':')))
    sys.stdout.flush()
    time.sleep(max(0, 1-(time.time()-started)))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--api', default='http://127.0.0.1:8011')
    parser.add_argument('--key', type=Path, default=Path.home()/'.ssh/id_ed25519_192_168_1_190')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800:
        parser.error('--seconds must be 1..1800')
    output = ROOT/'teleop_control/runtime/tianji'/time.strftime('camera-timeline-%Y%m%d-%H%M%S.jsonl')
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic()+args.seconds
    stop = threading.Event()
    lock = threading.Lock()
    output_file = output.open('x', encoding='utf-8')
    os.chmod(output, 0o600)

    def save(source, data):
        with lock:
            output_file.write(json.dumps(dict(source=source, pc_wall_time=time.time(),
                pc_monotonic=time.monotonic(), data=data), ensure_ascii=False)+'\n')
            output_file.flush()

    def app_watch():
        previous = None
        last_event = -1.0
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                # Never fall back to /status: that endpoint renews the operator lease.
                with urllib.request.urlopen(args.api.rstrip('/')+'/tianji/diagnostics', timeout=2) as response:
                    data = json.load(response)
                # Save each event once, even though the API retains a rolling history.
                # A restarted backend keeps the same host monotonic time base.
                events = data.get('events', [])
                data['events'] = [event for event in events if event['monotonic'] > last_event]
                if events:
                    last_event = max(last_event, max(event['monotonic'] for event in events))
                state = data['status']
                summary = dict(state=state.get('state'), phase=data.get('camera_phase'),
                    cameras=[c['id'] for c in state.get('cameras', []) if c.get('available')],
                    error=state.get('camera_error'))
            except Exception as exc:
                data = summary = dict(error=str(exc), endpoint='passive_diagnostics')
            save('app', data)
            if summary != previous:
                print('App '+json.dumps(summary, ensure_ascii=False), flush=True)
                previous = summary
            stop.wait(0.5)

    def controller_watch():
        previous = None
        while not stop.is_set() and time.monotonic() < deadline:
            seconds = max(1, int(deadline-time.monotonic()))
            command = ['ssh', '-i', str(args.key), '-o', 'IdentitiesOnly=yes',
                '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=12', '-o', 'ServerAliveInterval=10',
                '-o', 'ServerAliveCountMax=3', 'root@192.168.1.190',
                'nice -n 10 python -u - '+str(seconds)]
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as process:
                try:
                    process.stdin.write(REMOTE)
                    process.stdin.close()
                    for line in process.stdout:
                        if stop.is_set(): break
                        try: data = json.loads(line)
                        except ValueError:
                            save('controller_error', dict(error='Invalid diagnostic JSON'))
                            continue
                        save('controller', data)
                        summary = dict(boot_id=data.get('boot_id'), usb=sorted(data.get('usb', {})))
                        if summary != previous:
                            print('USB '+json.dumps(summary), flush=True)
                            previous = summary
                finally:
                    if process.poll() is None: process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if process.returncode:
                    error = process.stderr.read(4096)
                    save('controller_error', dict(code=process.returncode, error=error))
                    print('SSH '+error.strip(), flush=True)
            stop.wait(3)

    print('只读监测开始，时限 %s 秒；日志：%s' % (args.seconds, output), flush=True)
    threads = [threading.Thread(target=fn, daemon=True) for fn in (app_watch, controller_watch)]
    try:
        for thread in threads: thread.start()
        while any(thread.is_alive() for thread in threads):
            for thread in threads: thread.join(timeout=0.25)
    except KeyboardInterrupt:
        stop.set()
        for thread in threads: thread.join(timeout=15)
    finally:
        # Controller reads are bounded to one second; SSH has keepalive bounds.
        stop.set()
        for thread in threads: thread.join(timeout=15)
        if not any(thread.is_alive() for thread in threads): output_file.close()
    print('监测结束：'+str(output), flush=True)


if __name__ == '__main__':
    main()
