"""Real controller-side recording boundary while the camera transport stays open."""
import os,time,threading

class AuditTask(threading.Thread):
    def __init__(self, function, args):
        threading.Thread.__init__(self)
        self.daemon=True;self.function=function;self.args=args
        self.done=threading.Event();self.value=None;self.error=None
        self.start()
    def run(self):
        try:self.value=self.function(*self.args)
        except Exception as exc:self.error=exc
        finally:self.done.set()
    def result(self):
        if self.error:raise self.error
        return self.value

def native_snapshot(handle):
    # Reopen through proc: do not seek the shared file description used by stderr.
    size=os.fstat(handle.fileno()).st_size
    with open('/proc/self/fd/%d'%handle.fileno(),'rb') as f:
        start=max(0,size-1024*1024);f.seek(start);raw=f.read(size-start)
    if start:raw=raw.split(b'\n',1)[-1]
    return raw.decode('utf-8','replace').splitlines()

def active_baseline(before, selected):
    """Account only for the known open camera/DCSS handles, then audit live state."""
    from streaming import audit,unchanged_for_recovery
    after=audit()
    def modules(value):
        return dict((row.split()[0],row.split()) for row in value.splitlines() if row.strip())
    old,new=modules(before['modules']),modules(after['modules'])
    if set(old)!=set(new):raise RuntimeError('Kernel modules changed during camera startup')
    for name,a in old.items():
        b=new[name]
        if len(a)<6 or len(b)!=len(a) or a[:2]+a[3:]!=b[:2]+b[3:]:
            raise RuntimeError('Kernel module identity changed during camera startup')
        expected=2 if name=='FXKMemory' else len(selected) if name=='usbcore' else 0
        if int(b[2])-int(a[2])!=expected:
            raise RuntimeError('Unexpected module ownership during camera startup: '+name)
    adjusted=dict(before,modules=after['modules'])
    if not unchanged_for_recovery(adjusted,after,'teleop_observation'):
        raise RuntimeError('Controller state changed during camera startup')
    return after

def checkpoint(api, before, process, errors, counts, joint_sent, sampler, selected, last_stamps, ident):
    from streaming import audit,unchanged_for_recovery,native_queue_overflow
    from camera_audit import timing_notice_warnings
    after=audit()
    diagnostics=native_snapshot(errors)
    for line in diagnostics:
        line=line.strip()
        if (not line or line.startswith(('{','[')) or line.startswith('unsupported descriptor subtype ')
                or line == 'attempt to claim already-claimed interface 1'):continue
        raise RuntimeError('Camera error before recording checkpoint: '+line[:512])
    if not unchanged_for_recovery(before,after,'teleop_observation'):
        raise RuntimeError('Controller audit changed before recording checkpoint')
    now=int(api.monotonic()*1e9)
    if sampler is None or sampler.error:raise RuntimeError('Joint sampler unavailable at recording checkpoint')
    # The streaming thread records CURRENT frame age at the ordered boundary;
    # request-time copies cannot describe delivery latency.
    result=dict(ok=True,type='checkpoint',checkpoint_id=ident,transport_continues=True,
        boundary_kind='controller_checkpoint',audit_scope='continuous_stream_interval',
        timestamp_ns=now,controller_monotonic_ns=now,controller_unix_ns=int(time.time()*1e9),
        boot_id=api.read('/proc/sys/kernel/random/boot_id'),
        frames=list(counts),joint_samples=joint_sent,cameras=list(selected),
        joint_feedback_error=sampler.feedback_error,joint_gap_count=sampler.gap_count,
        joint_skipped_ticks=sampler.skipped,joint_max_lateness_ns=sampler.max_lateness_ns,
        native_exit=None,native_cleanup_performed=False,stop_reason='episode_boundary',
        camera_queue_overflow=native_queue_overflow(diagnostics),
        native_diagnostics_scope='live_latest_1MiB',audit_before=before,audit_after=after,
        audit_warnings=timing_notice_warnings(before,after,'teleop_observation'),error=None)
    return result,after
