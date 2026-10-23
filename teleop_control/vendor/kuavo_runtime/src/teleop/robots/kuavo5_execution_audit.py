"""Read-only ROS execution-interface discovery; never grants motion permission."""

import shlex


ARM_CONFIGURATION_INTERFACES = (
    ('/enable_lb_arm_quick_mode', 'kuavo_msgs/changeLbQuickModeSrv', {'quickMode': 'int8'}),
    ('/change_lb_mpc_obs_update_mode', 'kuavo_msgs/changeLbMpcObsUpdateModeSrv', {'obsUpdateMode': 'int8'}),
    ('/enable_arm_traj_interpolator', 'std_srvs/SetBool', {'data': 'bool'}),
)

REQUIRED_RUNTIME_NODES = ('/arms_ik_node', '/mobile_manipulator_sqp_mpc_node', '/nodelet_manager')


def _runtime_node_issues(nodes):
    """A registered name is not a live node, and a live node is not readiness."""
    if not isinstance(nodes, dict):
        return ['required runtime node responses unavailable or malformed']
    issues = []
    for name in REQUIRED_RUNTIME_NODES:
        item = nodes.get(name)
        if not isinstance(item, dict):
            issues.append(name + ' response unavailable or malformed')
        elif item.get('status') != 'alive':
            issues.append(name + ' ' + str(item.get('status', 'unknown')))
        elif type(item.get('pid')) is not int or item['pid'] <= 0:
            issues.append(name + ' invalid process id')
    return issues


def _configuration_interface_issues(services):
    """Check wire schemas, not current mode values or permission to change them."""
    if not isinstance(services, list) or any(not isinstance(s, dict) for s in services):
        return ['arm_configuration_services unavailable or malformed']
    issues = []
    for name, kind, request in ARM_CONFIGURATION_INTERFACES:
        matches = [s for s in services if s.get('name') == name]
        if len(matches) != 1:
            issues.append(name + ' missing or duplicated')
            continue
        service = matches[0]
        owners = service.get('owners')
        if not isinstance(owners, list) or not owners or any(
                not isinstance(n, str) or not n for n in owners):
            issues.append(name + ' owners missing or malformed')
        if 'query_error' in service:
            issues.append(name + ' query failed: ' + str(service['query_error']))
            continue
        if service.get('type') != kind or service.get('request_fields') != request \
                or service.get('response_fields') != {'success': 'bool', 'message': 'string'}:
            issues.append(name + ' type/fields mismatch')
    return issues


def build_readonly_ssh_command(program, *, control_path):
    """Reuse an existing authenticated master; never start a fallback connection.

    The robot's non-interactive shell startup was inspected before using this
    entry. No login shell or vendor setup script is sourced. This helper only
    builds argv; the caller must supply the audited query-only program.
    """
    if not isinstance(program, str) or not program.strip() or '\0' in program:
        raise ValueError('nonempty read-only program required')
    if not isinstance(control_path, str) or not control_path.startswith('/') \
            or '\0' in control_path:
        raise ValueError('absolute SSH master socket path required')
    remote = shlex.join([
        '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin',
        'PYTHONPATH=/home/lab/kuavo-ros-control/devel/lib/python3/dist-packages:'
        '/opt/ros/noetic/lib/python3/dist-packages',
        'ROS_MASTER_URI=http://127.0.0.1:11311',
        '/usr/bin/python3', '-B', '-u', '-c', program,
    ])
    return ['ssh', '-F', '/dev/null', '-T', '-S', control_path,
            '-o', 'BatchMode=yes', '-o', 'ProxyCommand=false',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=5',
            'lab@192.168.26.1', remote]


def assess_report(report, *, version, urdf_sha256):
    if not isinstance(report, dict) or report.get('event') != 'KUAVO5_EXECUTION_INTERFACE_AUDIT':
        raise ValueError('unexpected execution audit report')
    issues = []
    if report.get('audit_incomplete') is True:
        issues.append('read-only interface audit incomplete')
    for field, expected in (('robot_version', version), ('urdf_sha256', urdf_sha256),
                            ('topic_type', 'sensor_msgs/JointState')):
        if report.get(field) != expected:
            issues.append(field+' mismatch')
    for field in ('control_services_called', 'control_topics_published', 'physical_robot_command_sent'):
        if report.get(field) is not False:
            issues.append(field+' must be false')
    for field in ('publishers', 'subscribers'):
        values = report.get(field)
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            issues.append(field+' malformed')
    if not report.get('subscribers'):
        issues.append('no arm-trajectory subscriber')
    services = report.get('mode_services')
    if not isinstance(services, list) or any(not isinstance(s, dict) for s in services):
        issues.append('mode_services malformed')
        services = []
    matches = [s for s in services if s.get('name') == '/wheel_arm_change_arm_ctrl_mode']
    if len(matches) != 1 or matches[0].get('type') != 'kuavo_msgs/changeArmCtrlMode':
        issues.append('documented wheel arm mode service missing or different')
    else:
        service = matches[0]
        owners = service.get('owners')
        if not isinstance(owners, list) or not owners or any(not isinstance(n, str) or not n for n in owners):
            issues.append('wheel arm mode service owners missing or malformed')
        for field, key, expected in (('request_fields', 'control_mode', 'int32'),
                                     ('response_fields', 'result', 'bool')):
            fields = service.get(field)
            if not isinstance(fields, dict) or fields.get(key) != expected:
                issues.append('wheel arm mode service '+field+' mismatch')
    # A ROS graph is not proof of controller behaviour. In particular the ARM
    # mode service (0/2) must not be confused with the whole-body MPC mode.
    # These unresolved items cannot be cleared by booleans supplied in a report.
    blockers = ['controller_stop_contract_unverified', 'base_torso_hold_unverified',
                'robot_side_watchdog_not_verified', 'live_execution_not_verified',
                'ik_result_consumers_unverified']
    runtime_issues = _runtime_node_issues(report.get('node_liveness'))
    if runtime_issues:
        blockers.append('required_runtime_nodes_unavailable')
    hardware_ready = type(report.get('hardware_ready')) is int and report['hardware_ready'] == 1
    if not hardware_ready:
        blockers.append('hardware_ready_parameter_not_one')
    if issues:
        blockers.append('execution_interface_mismatch')
    for field, code in (('ik_command_publishers', 'concurrent_ik_command_publisher'),
                        ('cartesian_publishers', 'concurrent_cartesian_publisher')):
        nodes = report.get(field)
        if not isinstance(nodes, list) or any(not isinstance(n, str) or not n for n in nodes):
            blockers.append(field + '_unavailable')
        elif nodes:
            blockers.append(code)
    for part in ('base', 'torso'):
        sources = report.get(part + '_command_publishers')
        if not isinstance(sources, dict) or any(
                not isinstance(topic, str) or not topic or not isinstance(nodes, list)
                or any(not isinstance(n, str) or not n for n in nodes)
                for topic, nodes in sources.items()):
            blockers.append(part + '_command_publishers_unavailable')
        elif any(sources.values()):
            # Registered sources need auditing; registration alone is not motion.
            blockers.append(part + '_command_sources_require_review')
    configuration_issues = _configuration_interface_issues(report.get('arm_configuration_services'))
    return dict(report, issues=issues, interface_structure_matches=not issues,
                runtime_node_issues=runtime_issues, runtime_nodes_reachable=not runtime_issues,
                hardware_ready_parameter_matches=hardware_ready,
                arm_configuration_interface_issues=configuration_issues,
                arm_configuration_interfaces_match=not configuration_issues,
                running_arm_profile_verified=False,
                execution_blockers=blockers, base_torso_hold_verified=False,
                stop_semantics_verified=False, motion_ready=False)


def remote_program():
    """Only graph/parameter/type queries; no ROS control messages or service calls."""
    return r'''
import hashlib
import json
from pathlib import Path
import signal
import socket
import xmlrpc.client
import rosgraph
import rosservice

socket.setdefaulttimeout(3.0)
class AuditDeadline(TimeoutError):
    pass
def deadline(*_):
    raise AuditDeadline("read-only interface audit exceeded 15 seconds")
signal.signal(signal.SIGALRM, deadline)
signal.alarm(15)
report = dict(event="KUAVO5_EXECUTION_INTERFACE_AUDIT", node_liveness={
    name: dict(status="not_queried") for name in
    ("/arms_ik_node", "/mobile_manipulator_sqp_mpc_node", "/nodelet_manager")},
    mode_services=[], whole_body_mode_services=[], arm_configuration_services=[],
    control_services_called=False, control_topics_published=False, physical_robot_command_sent=False)
try:
    master = rosgraph.Master("/galaxis_execution_interface_audit")
    pubs, subs, services = master.getSystemState()
    publishers, subscribers, owners = dict(pubs), dict(subs), dict(services)
    types = dict(master.getTopicTypes())
    model = Path(master.getParam("/urdfFile"))
    startup_parameters = {name: master.getParam(name) if master.hasParam(name) else None
                          for name in ("/use_external_mpc", "/use_vr_control")}
    report.update(robot_version=str(master.getParam("/robot_version")),
                  urdf_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
                  topic_type=types.get("/kuavo_arm_traj"),
                  publishers=publishers.get("/kuavo_arm_traj", []),
                  subscribers=subscribers.get("/kuavo_arm_traj", []),
                  cartesian_publishers=publishers.get("/mm/two_arm_hand_pose_cmd", []),
                  ik_command_publishers=publishers.get("/ik/two_arm_hand_pose_cmd", []),
                  ik_result_subscribers=subscribers.get("/ik/result", []),
                  base_command_publishers={name: publishers[name] for name in
                      ("/cmd_vel", "/cmd_vel_world", "/cmd_pose", "/cmd_pose_world")
                      if name in publishers},
                  torso_command_publishers={name: publishers[name] for name in
                      ("/cmd_lb_torso_pose", "/cmd_torso_vel", "/cmd_torso_delta", "/lb_leg_traj")
                      if name in publishers},
                  startup_parameters=startup_parameters,
                  hardware_ready=master.getParam("/hardware/is_ready") if master.hasParam("/hardware/is_ready") else None)
    node_liveness = report["node_liveness"]
    for name in ("/arms_ik_node", "/mobile_manipulator_sqp_mpc_node", "/nodelet_manager"):
        item = dict(status="querying")
        node_liveness[name] = item
        try:
            uri = master.lookupNode(name)
        except AuditDeadline:
            raise
        except rosgraph.MasterError as exc:
            item.update(status="missing", error=str(exc))
        except Exception as exc:
            item.update(status="unreachable", error=str(exc))
        else:
            item["uri"] = uri
            try:
                reply = xmlrpc.client.ServerProxy(uri).getPid("/galaxis_execution_interface_audit")
                if not isinstance(reply, (list, tuple)) or len(reply) != 3 \
                        or type(reply[0]) is not int or reply[0] != 1 \
                        or type(reply[2]) is not int or reply[2] <= 0:
                    item.update(status="invalid_response", error="getPid requires success and a positive integer PID")
                else:
                    item.update(status="alive", pid=reply[2])
            except AuditDeadline:
                raise
            except Exception as exc:
                item.update(status="unreachable", error=str(exc))
    mode_services = report["mode_services"]
    whole_body_mode_services = report["whole_body_mode_services"]
    arm_configuration_services = report["arm_configuration_services"]
    for name in ("/wheel_arm_change_arm_ctrl_mode", "/change_arm_ctrl_mode",
                 "/mobile_manipulator_mpc_control", "/enable_lb_arm_quick_mode",
                 "/change_lb_mpc_obs_update_mode", "/enable_arm_traj_interpolator"):
        if name not in owners:
            continue
        item = dict(name=name, owners=owners[name])
        if name == "/mobile_manipulator_mpc_control":
            whole_body_mode_services.append(item)
        elif name in ("/wheel_arm_change_arm_ctrl_mode", "/change_arm_ctrl_mode"):
            mode_services.append(item)
        else:
            arm_configuration_services.append(item)
        item["query_error"] = "query did not complete"
        try:
            item["type"] = rosservice.get_service_type(name)
            cls = rosservice.get_service_class_by_name(name)
            if cls is not None:
                item["request_fields"] = dict(zip(cls._request_class.__slots__, cls._request_class._slot_types))
                item["response_fields"] = dict(zip(cls._response_class.__slots__, cls._response_class._slot_types))
            item.pop("query_error", None)
        except AuditDeadline:
            raise
        except Exception as exc:
            item["query_error"] = str(exc)
except AuditDeadline as exc:
    report.update(audit_incomplete=True, audit_error=str(exc))
    for item in report["node_liveness"].values():
        if item.get("status") == "querying":
            item.update(status="timed_out", error=str(exc))
except Exception as exc:
    report.update(audit_incomplete=True, audit_error=str(exc))
finally:
    signal.alarm(0)
print(json.dumps(report, allow_nan=False), flush=True)
'''
