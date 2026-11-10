"""Bounded recovery of the dedicated camera USB controller. No robot commands."""
from __future__ import print_function
import os,sys,json,glob,time,subprocess,signal,fcntl
import server
from joints import JointReader
EXPECTED={'1-1':('2b03','f681'),'1-2':('32e4','9230'),'1-4':('32e4','9230'),'2-1':('2b03','f682')}
PCI='/sys/bus/pci/devices/0000:00:14.0';DRIVER='/sys/bus/pci/drivers/xhci_hcd'
def text(path):return open(path).read().strip()
def guard():
 r=JointReader(server.monotonic)
 try:s=r.sample()
 finally:r.close()
 assert s['arm_state']==[0,-1,0,0,-1,0], 'Arms must be disabled'
 assert os.path.realpath(PCI+'/driver')==DRIVER
 rows=[]
 for path in glob.glob('/sys/bus/usb/devices/*'):
  name=os.path.basename(path)
  if ':' in name or not os.path.exists(path+'/idVendor'):continue
  ids=(text(path+'/idVendor'),text(path+'/idProduct'))
  if name.startswith('usb'):
   assert name in ('usb1','usb2') and ids in (('1d6b','0002'),('1d6b','0003'))
   assert os.path.dirname(os.path.realpath(path))==os.path.realpath(PCI)
  else:assert name in EXPECTED and ids==EXPECTED[name], 'Non-camera USB device present: '+name
  rows.append({'path':name,'ids':ids,'address':text(path+'/devnum')})
 for path in glob.glob('/sys/block/*'):
  assert '/usb' not in os.path.realpath(path),'USB storage present'
 for path in glob.glob('/proc/[0-9]*/exe'):
  try:exe=os.readlink(path)
  except OSError:continue
  assert os.path.basename(exe) not in ('capture','capture-three','capture-three-teleop'),'Capture active'
 return {'arm_state':s['arm_state'],'usb':rows,'driver':os.path.realpath(PCI+'/driver')}

def main():
 lock=open('/run/fusion-camera/usb-recovery.lock','a')
 fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
 before=guard()
 config=json.load(open('/mnt/fusion-camera/server.json'))
 for cam in config['cameras']:
  info=server.device_info(cam)
  if info['available']:
   assert server.interface_idle(cam),'Camera interface active'
   with open(os.devnull,'wb') as null:
    assert subprocess.call(['/bin/fuser','/dev/bus/usb/%03d/%03d'%(info['bus'],info['address'])],stdout=null,stderr=null)==1,'Camera USB device busy'
 def cancelled(signum,frame):raise RuntimeError('Camera USB recovery interrupted')
 signal.signal(signal.SIGTERM,cancelled)
 try:
  guard()
  with open(DRIVER+'/unbind','w') as f:f.write('0000:00:14.0')
  time.sleep(.5)
 finally:
  if not os.path.exists(PCI+'/driver'):
   with open(DRIVER+'/bind','w') as f:f.write('0000:00:14.0')
 for line in open('/proc/interrupts'):
  if 'xhci_hcd' in line:
   irq=line.split(':')[0].strip()
   if irq.isdigit():
    with open('/proc/irq/'+irq+'/smp_affinity_list','w') as f:f.write('3')
 for _ in range(40):
  if all(os.path.exists('/sys/bus/usb/devices/'+p+'/idVendor') for p in EXPECTED):break
  time.sleep(.2)
 after=guard()
 print(json.dumps({'ok':True,'before':before,'after':after}))
if __name__=='__main__':
 try:main()
 except Exception as exc:
  print(json.dumps({'ok':False,'error':str(exc)}));sys.exit(1)
