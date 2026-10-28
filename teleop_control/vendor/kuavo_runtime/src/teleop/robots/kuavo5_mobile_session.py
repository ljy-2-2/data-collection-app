"""Owned chassis channel inside the arm process; no I/O until explicit start.

The arm input lease is reused. No second input receiver, mode service, ROS node,
or direct wheel topic is created. Tests inject all device interfaces.
"""
import time
import threading

from teleop.control.kuavo5_base_session import BaseSession
from teleop.robots.kuavo5_base_lock import acquire_base_lock
from teleop.robots.kuavo5_base_ros import BaseRosBinding


BASE_MODE='vendor_vr_x'
BASE_POLICY='owned_body_relative_v1'


class JointBaseRuntime:
    def __init__(self, *, clock=time.monotonic_ns, sleep=time.sleep,
                 binding_factory=BaseRosBinding, lock_path='/tmp/teleop-kuavo5-base.lock'):
        self.clock,self.sleep=clock,sleep
        self.binding_factory,self.lock_path=binding_factory,lock_path
        self.binding=None
        self.lockfile=None
        self.session=BaseSession(publish=self._publish, toggle_mode='x',
            recover_with_neutral=False, clock=clock)
        self._enabled=False
        self._last_frame=None
        self._closed=False
        self._close_started=False
        self._close_thread=None
        self._close_done=threading.Event()
        self._finish_close=threading.Event()
        self._close_report=None
        self._stop_error=None
        self._publish_attempts=0
        self._published=0
        self._last_publish_ns=None
        self._last_velocity=(0.,0.,0.)

    def _publish(self, velocity):
        if not self._enabled:
            if any(velocity):
                raise ValueError('base publication unavailable before unified readiness')
            return  # Preflight observes quiet without creating a publisher.
        now=self.clock()
        # Share the arm loop without doubling the old 50 Hz base sender rate.
        # A transition to zero always passes immediately, even between ticks.
        if (self._last_publish_ns is not None and now-self._last_publish_ns<20_000_000
                and not (not any(velocity) and any(self._last_velocity))):
            return
        self._publish_attempts+=1
        self.binding.create_publisher()
        self.binding.publish(velocity)
        self._published+=1
        self._last_publish_ns=now
        self._last_velocity=tuple(velocity)

    def start(self, ros, master):
        if self.binding is not None or self.lockfile is not None or self._closed:
            raise ValueError('base runtime already started or closed')
        self.lockfile=acquire_base_lock(self.lock_path)
        self.binding=self.binding_factory(motion_enabled=True,ros=ros,master=master,
            clock=self.clock,speed_profile='vendor_vr',shared_arms=True)
        self.binding.start()

    def preflight(self, stop, *, timeout_s=5.):
        deadline=self.clock()+int(timeout_s*1e9)
        while self.clock()<deadline:
            if stop.is_set():
                raise ValueError('operator disconnected during base preflight')
            if self.binding.guard.error:
                raise ValueError(self.binding.guard.error)
            health,feedback=self.binding.guard.snapshot()
            now=self.clock()
            # Missing first samples may arrive; explicit bad state never changes modes.
            if (not health.error and 0<=now-health.observed_ns<500_000_000
                    and feedback is not None and 0<=now-feedback.observed_ns<200_000_000
                    and all(v is True for v in (health.control_enabled,health.velocity_enabled,health.mpc_enabled))):
                if not health.downstream_quiet:
                    raise ValueError('manufacturer base output nonzero before takeover')
                report=self.session.tick(None,now_ns=now,health=health,feedback=feedback)
                if report['error']:
                    raise ValueError(report['error'])
                if self.session.started:
                    return
            else:
                # A gap must not join two separate quiet periods.
                self.session._quiet_since=None
            self.sleep(.01)
        raise ValueError('base preflight requires fresh modes, odometry and 0.5 s quiet output')

    def activate(self):
        self.check()
        if not self.session.started:
            raise ValueError('base quiet preflight incomplete')
        self.session.publish_attempts=self.session.publish_enqueued=0
        self._enabled=True

    def check(self):
        if self._closed or self.binding is None:
            raise ValueError('base binding unavailable')
        health,feedback=self.binding.guard.snapshot()
        now=self.clock()
        if (health.error or not 0<=now-health.observed_ns<500_000_000
                or any(v is not True for v in (health.control_enabled,health.velocity_enabled,health.mpc_enabled))
                or feedback is None or not 0<=now-feedback.observed_ns<200_000_000
                or self.session.error):
            raise ValueError('unified base authority/feedback lost: '+str(health.error or self.session.error or 'not ready'))

    def observe(self, frame):
        # Only called with a frame accepted by the existing robot-issued lease.
        now=self.clock()
        if frame.stale or not 0<=now-frame.received_monotonic_ns<100_000_000:
            return
        self._last_frame=frame
        self.binding.input_deadline_ns=frame.received_monotonic_ns+100_000_000

    def tick(self, *, inhibit=False):
        health,feedback=self.binding.guard.snapshot()
        frame,self._last_frame=self._last_frame,None
        report=self.session.tick(frame,now_ns=self.clock(),health=health,feedback=feedback,
                                 inhibit=inhibit or self._close_started)
        return report

    def begin_close(self, reason):
        if self._close_started or self.binding is None:
            return
        self._close_started=True
        self._last_frame=None
        self.binding.input_deadline_ns=None
        self.session.request_close(reason)
        if self._enabled:
            try:
                self.tick(inhibit=True)  # Revoke before waiting on other cleanup.
            except Exception as exc:
                self._stop_error=str(exc)
            # Only started once the shared input loop has ended. Continue
            # zero/feedback observation while arm/gripper shutdown may block;
            # retain ROS resources and the exclusive lock until close joins us.
            self._close_thread=threading.Thread(target=self._observe_close,
                name='kuavo-joint-base-close',daemon=True)
            self._close_thread.start()

    def _observe_close(self):
        try:
            deadline=self.clock()+self.session.stop_timeout_ns+100_000_000
            while not self.session.closed and self.clock()<deadline:
                self.tick(inhibit=True)
                if not self.session.closed:
                    self.sleep(.01)
            if not self.session.report.get('stop_observed'):
                self._stop_error=self._stop_error or 'base stop feedback not confirmed'
            if self.session.error:
                self._stop_error=self.session.error
            self._close_done.set()
            # Initial quiet feedback is not the final cleanup result. Keep
            # reading while another owner closes; no further movement commands.
            while not self._finish_close.wait(.01):
                self._verify_stopped()
        except Exception as exc:
            self._stop_error=str(exc)
        finally:
            self._close_done.set()

    def _verify_stopped(self):
        health,feedback=self.binding.guard.snapshot()
        now=self.clock()
        if (health.error or not 0<=now-health.observed_ns<500_000_000
                or any(v is not True for v in (health.control_enabled,health.velocity_enabled,health.mpc_enabled))
                or not health.downstream_quiet
                or type(health.downstream_observed_ns) is not int
                or not 0<=now-health.downstream_observed_ns<200_000_000
                or feedback is None or not 0<=now-feedback.observed_ns<200_000_000
                or max(abs(feedback.vx),abs(feedback.vy))>.01 or abs(feedback.wz)>.02):
            raise ValueError('base stop observation lost during cleanup: '+str(health.error or 'feedback not fresh and quiet'))

    def close(self):
        if self._close_report is not None:
            return dict(self._close_report)
        errors=[]
        try:
            self.begin_close('unified session ended')
            if self._close_thread is not None:
                # Never release ownership while a publisher can still run.
                self._finish_close.set()
                self._close_thread.join()
            if self.binding is not None and self._enabled:
                self._verify_stopped()
        except Exception as exc:
            self._stop_error=str(exc)
        finally:
            if self.binding is not None:
                try: self.binding.close()
                except Exception as exc: errors.append(str(exc))
            if self.lockfile is not None:
                self.lockfile.close(); self.lockfile=None
            self._enabled=False
            self._closed=True
        self._close_report=dict(closed=not errors,errors=errors,stop_error=self._stop_error,
                    stop_observed=not self._stop_error and bool(self.session.report.get('stop_observed')),
                    physical_stop_verified=False,publish_attempts=self._publish_attempts,
                    publish_enqueued=self._published,final_status=self.session.report)
        return dict(self._close_report)
