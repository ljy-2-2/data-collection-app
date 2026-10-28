"""Interpret read-only preparation evidence separately from session admission."""


def hardware_readiness(report):
    value = report
    for _ in range(4):
        if not isinstance(value, dict):break
        if value.get('error') or value.get('ok') is False:
            return False, '主控就绪状态读取失败，请重新检查设备：'+str(value.get('error') or '准备状态查询失败')
        if 'controller' in value:break
        value = value.get('result')
    if not isinstance(value, dict) or not isinstance(value.get('controller'), dict):
        return False, '尚未核验厂家主控就绪状态，请检查设备；服务在线不代表可遥操'
    controller = value['controller']
    phase = value.get('phase')
    parameters = controller.get('parameters') or {}
    if phase == 'not_started':
        return False, '厂家主控未启动：点击检查设备可按需启动并初始化'
    if phase == 'awaiting_confirmation':
        return False, '厂家主控已启动，等待初始化；点击检查设备可继续准备，不会重复启动主控'
    if phase in ('starting', 'preparing'):
        return False, '厂家主控正在准备，尚未就绪；检查设备会等待状态更新，不会重复启动'
    if (phase == 'ready' and controller.get('phase') == 'ready'
            and str(parameters.get('/robot_version')) == '63'
            and type(parameters.get('/hardware/is_ready')) is int
            and parameters['/hardware/is_ready'] == 1
            and parameters.get('/use_external_mpc') is True):
        return True, '厂家主控就绪状态已核验；开始时再次检查，运行保护仍由机器人服务执行'
    return False, '厂家主控尚未就绪或 ROS 环境不匹配，请核对主控状态后重新检查设备；不要重复启动已有主控'


def require_hardware_ready(report):
    ready, detail = hardware_readiness(report)
    if not ready:raise ValueError(detail)


def service_block_detail(service):
    if not isinstance(service,dict):return ''
    blocked=service.get('blocked_reason')
    if blocked=='worker_exit_without_confirmed_cleanup':
        failed=(service.get('cleanup_diagnostics') or {}).get('failed_checks',[])
        if 'arm_hold_unconfirmed' in failed:
            return '机器人网络已连接，但上一会话未确认手臂保持状态，服务锁定了新会话。需要现场核对停止状态并按厂家流程恢复；重复检查设备不会解除此锁。'
        return '机器人网络已连接，但上一会话退出清理未通过，服务暂不允许开始新会话。'
    if blocked:return '机器人网络已连接，服务暂不可遥操：'+str(blocked)
    if service.get('owner_present') is True:return '机器人网络已连接，已有控制会话或准备操作占用服务。'
    return ''
