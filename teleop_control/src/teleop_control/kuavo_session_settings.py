"""Versioned KUAVO parameter operations, loaded only by the isolated worker."""
import copy
import re


LOCK_MESSAGES={
    'session_active_or_cleanup_pending':'遥操作会话正在运行，或上一会话正在退出清理；完成后可修改参数',
    'previous_operation_cleanup_unconfirmed':'上一操作尚未确认清理完成，请重新检查设备',
    'controller_busy':'主控正在处理操作，请稍后重试',
    'preparation_busy':'设备准备正在进行，请稍后重试',
    'maintenance':'机器人服务正在维护',
    'shutting_down':'机器人服务正在关闭',
    'session_settings_not_installed':'机器人尚未安装参数设置功能',
    'session_settings_unreadable':'机器人参数暂时无法读取，请重新检查设备',
}

def lock_message(code):
    return LOCK_MESSAGES.get(code,'机器人暂不允许修改参数：'+str(code)) if code else ''


def settings_form(result):
    from teleop.robots.kuavo5_service_contract import validate_settings_snapshot, validate_settings_schema
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise ValueError('机器人参数请求失败：' + str(result.get('error') if isinstance(result, dict) else result))
    snapshot = validate_settings_snapshot(result.get('snapshot'))
    schema = validate_settings_schema(result.get('schema'), result.get('defaults'))
    if snapshot['installation_sha256'] != result.get('installation_sha256'):
        raise ValueError('参数与机器人安装版本不一致，请重新读取')
    if not re.fullmatch('[0-9a-f]{32}', result.get('service_instance_id') or ''):
        raise ValueError('机器人参数缺少有效服务身份')
    return dict(values=copy.deepcopy(snapshot['values']), schema=schema,
                revision=snapshot['revision'], settings_sha256=snapshot['settings_sha256'],
                service_instance_id=result.get('service_instance_id'),
                installation_sha256=result.get('installation_sha256'),
                editable=result.get('editable') is True, locked_reason=result.get('locked_reason'))


def read_settings(client, status):
    form = settings_form(client.get_session_settings())
    service = status.get('service') or {}
    if any(form[key] != service.get(key) for key in ('service_instance_id', 'installation_sha256')):
        raise ValueError('读取参数时机器人服务已变化，请重新读取')
    return form


def settings_operation(client, request):
    """Never retry writes. Retain the original observed revision for preview/apply."""
    action = request['action']
    if action == 'read':
        status = client.get_status()
        if status.get('management_ready') is not True:
            raise ValueError('请先核验机器人服务连接')
        return {'service': status, 'session_settings': read_settings(client, status)}
    identity = {k: request[k] for k in ('expected_revision', 'expected_service_instance_id',
                                       'expected_installation_sha256')}
    changes = request.get('changes') or {}
    if action in ('preview', 'apply'):
        from teleop.robots.kuavo5_service_contract import validate_settings_values
        changes = validate_settings_values(changes, partial=True)
        preview = client.preview_session_settings(changes, **identity)
        if preview.get('ok') is not True:
            raise ValueError('参数预览未通过：' + str(preview.get('error') or preview))
        if action == 'preview':
            return {'settings_preview': preview}
    if request.get('confirm') is not True:
        raise ValueError('需要明确确认保存或恢复参数')
    if action == 'apply':
        saved = client.apply_session_settings(changes, confirm=True, **identity)
    elif action == 'reset':
        saved = client.reset_session_settings(confirm=True, **identity)
    else:
        raise ValueError('无效参数操作')
    if saved.get('ok') is not True:
        raise ValueError('参数未确认保存，请重新读取：' + str(saved.get('error') or saved))
    current = client.get_session_settings()
    form = settings_form(current)
    if (form['service_instance_id'] != identity['expected_service_instance_id']
            or form['installation_sha256'] != identity['expected_installation_sha256']
            or current['snapshot'] != saved.get('snapshot')):
        raise ValueError('参数保存后回读发生变化，请重新读取；未自动重试')
    from teleop.robots.kuavo5_service_contract import expand_settings_changes
    expected_values = expand_settings_changes(changes, set(form['values'])) if action == 'apply' else current['defaults']
    if any(form['values'].get(key) != value for key, value in expected_values.items()):
        raise ValueError('机器人回读值与请求的参数不一致，请重新读取；未自动重试')
    return {'session_settings': form, 'settings_preview': None}


def has_base(contract):
    return ('base_vendor_vr_x_v1' in contract.get('capabilities', [])
            or contract.get('profile', {}).get('base_mode') == 'vendor_vr_x')


def require_selected_contract(contract, options):
    """Check the selected scope and pin parameters before opening XR/channel."""
    mode = options.get('arm_mode', 'dual')
    if mode not in ('dual', 'left', 'right', 'dual_base'):
        raise ValueError('无效遥操作范围')
    if mode == 'dual_base' and not has_base(contract):
        raise ValueError('机器人服务不支持双臂＋底盘，请选择双臂或使用兼容的联合服务')
    expected = options.get('expected_settings')
    if expected is None and ('session_settings' in contract or contract.get('protocol') in (
            'kuavo_robot_session_v2', 'kuavo_robot_session_v3')):
        raise ValueError('机器人已提供新版参数，请先重新读取并核对后开始')
    if expected is not None:
        snapshot = contract.get('session_settings') or {}
        if any(contract.get(k) != expected[k] for k in ('service_instance_id', 'installation_sha256')) or any(
                snapshot.get(k) != expected[k] for k in ('revision', 'settings_sha256')):
            raise ValueError('机器人参数或服务已改变，请重新读取后再开始')
