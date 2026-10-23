"""Explicit vendor-fast arm binding, imported only by the authorized entry."""

import math
import threading
import time
from queue import Queue, Empty
from types import SimpleNamespace

from teleop.robots.kuavo5_arm_ros_transport import KuavoArmRosTransport, TransportRefused
from teleop.robots.kuavo5_live_guard import RuntimeMotionPaused


VENDOR_BASELINE = (
    ('/wheel_arm_change_arm_ctrl_mode', {'control_mode': 0}),
    ('/enable_lb_arm_quick_mode', {'quickMode': 0}),
    ('/enable_arm_traj_interpolator', {'data': False}),
    ('/change_arm_ctrl_mode', {'control_mode': 0}),
)
VENDOR_ENTER = (
    ('/wheel_arm_change_arm_ctrl_mode', {'control_mode': 2}),
    ('/enable_lb_arm_quick_mode', {'quickMode': 2}),
    ('/enable_arm_traj_interpolator', {'data': True}),
)


def _call_bounded(call, name, request, *, timeout_s=.25):
    done = Queue(maxsize=1)
    def invoke(service=name, fields=request, results=done):
        try:
            results.put((call(service, fields), None))
        except Exception as exc:
            results.put((None, exc))
    threading.Thread(target=invoke, name='kuavo-profile-request', daemon=True).start()
    try:
        response, error = done.get(timeout=timeout_s)
    except Empty as exc:
        raise TransportRefused('vendor profile request timed out (late effects unverified): '+name) from exc
    if isinstance(error, RuntimeMotionPaused):
        raise error
    ok = (getattr(response, 'result', None) is True
          and type(getattr(response, 'mode', None)) is int
          and response.mode == request['control_mode']
          if 'control_mode' in request else getattr(response, 'success', None) is True)
    if error is not None or not ok:
        raise TransportRefused('vendor profile request failed: '+name+' '+repr(error))


def run_vendor_transition(call, *, entering):
    """Run one audited transition; exit attempts every cleanup setter once."""
    steps = VENDOR_ENTER if entering else VENDOR_BASELINE
    errors = []
    for name, request in steps:
        try:
            _call_bounded(call, name, request)
        except RuntimeMotionPaused:
            if entering:
                raise
            errors.append('unexpected motion pause during baseline restoration')
        except Exception as exc:
            errors.append(str(exc))
            if entering:
                break
    if errors:
        raise TransportRefused('; '.join(errors))


def prepare_vendor_baseline(call):
    """Restore the known idle baseline before any target or quick2 request."""
    run_vendor_transition(call, entering=False)


class RosArmBinding(KuavoArmRosTransport):
    def __init__(self, schema, *, max_velocity_rad_s=.6):
        import rospy
        from sensor_msgs.msg import JointState
        from kuavo_msgs.srv import changeArmCtrlMode, changeLbQuickModeSrv
        from std_srvs.srv import SetBool
        classes = {'/change_arm_ctrl_mode': changeArmCtrlMode,
                   '/wheel_arm_change_arm_ctrl_mode': changeArmCtrlMode,
                   '/enable_lb_arm_quick_mode': changeLbQuickModeSrv,
                   '/enable_arm_traj_interpolator': SetBool}
        self._proxies = {}
        self._profile_ready = False
        self._profile_attempted = False
        self._profile_active = False
        self._ros_message_attempts = 0
        self._ros_messages_enqueued = 0
        self._subscriber_connections = None
        self._runtime_check = lambda: None
        self._exit_check = lambda: None
        for name, cls in classes.items():
            rospy.wait_for_service(name, timeout=2.)
            self._proxies[name] = rospy.ServiceProxy(name, cls, persistent=False)
        self._publisher = rospy.Publisher('/kuavo_arm_traj', JointState,
                                          queue_size=1, latch=False, tcp_nodelay=True)
        def publish_message(names, values, velocities, *, final_check=None):
            if len(names) != 14 or len(values) != 14 or len(velocities) != 14 \
                    or not all(math.isfinite(value) for value in (*values, *velocities)):
                raise TransportRefused('exact finite 14-joint position/velocity message required')
            connections = self._publisher.get_num_connections()
            if self._subscriber_connections is None or connections != self._subscriber_connections:
                raise TransportRefused('arm subscriber topology changed')
            msg = JointState()
            msg.header.stamp = rospy.Time.now()
            msg.name, msg.position = list(names), list(values)
            msg.velocity, msg.effort = list(velocities), [0.]*14
            with self._condition:
                if final_check is not None:
                    final_check()
                reference_ns = self._now()
            self._ros_message_attempts += 1
            self._publisher.publish(msg)
            self._ros_messages_enqueued += 1
            return reference_ns
        self._publish_message = publish_message
        def publish(names, values, velocities):
            self._runtime_check()
            if not self._profile_ready or not self._profile_active:
                raise TransportRefused('vendor fast profile has not been established')
            if self._pending_publication_check is None:
                raise TransportRefused('publication requires an active checked target')
            def final_check():
                self._runtime_check()
                self._pending_publication_check()
            publish_message(names, values, velocities, final_check=final_check)
        def transition_check(state, check, *, enforce_envelope):
            # A fresh guard observation does not refresh the fixed snapshot
            # encoded into this transition message.
            check()
            self._validate_feedback_state(state, self._now(),
                                          enforce_envelope=enforce_envelope)
        def current_hold_message(check):
            state = check()
            try:
                values = tuple(math.degrees(state.positions[name]) for name in schema.names)
                positions = {name: state.positions[name] for name in schema.names}
            except Exception as exc:
                raise TransportRefused('fresh complete arm feedback required for transition') from exc
            return values, positions, state
        def call(name, request):
            self._runtime_check()
            return self._proxies[name](**request)
        def cleanup_call(name, request):
            return self._proxies[name](**request)
        def mode_request(mode):
            if mode not in (0, 2):
                raise TransportRefused('vendor profile supports only external2 and hold0')
            if mode == 2:
                if not self._profile_ready:
                    raise TransportRefused('vendor baseline has not been established')
                seed, seed_positions, seed_state = current_hold_message(self._runtime_check)
                # This newly fetched observation may differ from the feedback
                # checked by arm(). Reject it before any seed publication;
                # baseline synchronization after the loop is too late.
                if tuple(seed_state.positions) != schema.names:
                    raise TransportRefused('fresh exact arm feedback required for entry')
                with self._condition:
                    seed_state = self._validate_feedback_state(
                        seed_state, self._now(), enforce_envelope=True)
                seed_positions = {name: seed_state.positions[name] for name in schema.names}
                seed = tuple(math.degrees(seed_positions[name]) for name in schema.names)
                for _ in range(5):
                    self._runtime_check()
                    # _now also updates the shared last-clock sample. Serialize
                    # it with the watchdog; no external calls under this lock.
                    with self._condition:
                        self._validate_feedback_state(
                            seed_state, self._now(), enforce_envelope=True)
                    reference_ns = self._publish_message(schema.names, seed, (0.,)*14,
                        final_check=lambda: transition_check(seed_state, self._runtime_check,
                                                             enforce_envelope=True))
                    rospy.sleep(.01)
                self._synchronize_output_baseline(seed_positions, reference_time_ns=reference_ns)
                try:
                    run_vendor_transition(call, entering=True)
                    self._profile_active = True
                except Exception as entry_error:
                    self._profile_active = False
                    try:
                        run_vendor_transition(cleanup_call, entering=False)
                    except Exception as cleanup_error:
                        # Unknown partial mode changes cannot be downgraded to
                        # a resumable pause merely because its trigger was one.
                        raise TransportRefused('vendor entry failed: '+str(entry_error)
                            +'; baseline restoration failed: '+str(cleanup_error)) from cleanup_error
                    raise
                return SimpleNamespace(result=True, mode=2)
            quiesce_error = None
            try:
                hold, _, hold_state = current_hold_message(self._exit_check)
                # Validate before the first cleanup publication.  The cleanup
                # path may leave the fixed session envelope, but it must never
                # publish stale, incomplete, non-finite, or hard-limit-invalid
                # feedback as a hold target.
                if tuple(hold_state.positions) != schema.names:
                    raise TransportRefused('fresh exact arm feedback required for cleanup')
                with self._condition:
                    hold_state = self._validate_feedback_state(
                        hold_state, self._now(), enforce_envelope=False)
                hold = tuple(math.degrees(hold_state.positions[name]) for name in schema.names)
                for _ in range(5):
                    self._exit_check()
                    with self._condition:
                        self._validate_feedback_state(
                            hold_state, self._now(), enforce_envelope=False)
                    reference_ns = self._publish_message(schema.names, hold, (0.,)*14,
                        final_check=lambda: transition_check(hold_state, self._exit_check,
                                                             enforce_envelope=False))
                    rospy.sleep(.01)
                self._synchronize_cleanup_baseline(hold_state, reference_time_ns=reference_ns)
            except Exception as exc:
                quiesce_error = exc
            try:
                run_vendor_transition(cleanup_call, entering=False)
            except Exception as baseline_error:
                if quiesce_error is not None:
                    raise TransportRefused('zero-velocity quiesce failed: '
                        + str(quiesce_error) + '; baseline restoration failed: '
                        + str(baseline_error)) from baseline_error
                raise
            finally:
                self._profile_active = False
            if quiesce_error is not None:
                raise TransportRefused('zero-velocity quiesce failed: '+str(quiesce_error))
            return SimpleNamespace(result=True, mode=0)
        super().__init__(schema, publish=publish, change_mode=mode_request,
                         motion_enabled=True, mode_timeout_s=1.5, control_period_s=.01,
                         max_offset_rad=None,
                         max_velocity_rad_s=max_velocity_rad_s)

    def prepare_profile(self, *, check, exit_check, expected_connections):
        if self._profile_attempted:
            raise TransportRefused('profile setup cannot be retried in this session')
        if type(expected_connections) is not int or expected_connections < 3:
            raise TransportRefused('audited arm subscriber count is required')
        self._profile_attempted = True
        self._runtime_check = check
        self._exit_check = exit_check or check
        deadline, previous = time.monotonic()+2., None
        while True:
            check()
            current = self._publisher.get_num_connections()
            if current == expected_connections and current == previous:
                self._subscriber_connections = current
                break
            if time.monotonic() >= deadline:
                raise TransportRefused('required arm subscribers are not all connected')
            previous = current
            time.sleep(.01)
        def call(name, request):
            check()
            return self._proxies[name](**request)
        prepare_vendor_baseline(call)
        self._profile_ready = True

    def arm(self):
        if not self._profile_ready:
            raise TransportRefused('vendor fast profile has not been established')
        return super().arm()

    def close(self):
        report = super().close()
        self._publisher.unregister()
        return dict(report, profile_configured=self._profile_ready,
                    vendor_fast_profile_active=self._profile_active,
                    ros_message_attempts=self._ros_message_attempts,
                    ros_messages_enqueued=self._ros_messages_enqueued)
