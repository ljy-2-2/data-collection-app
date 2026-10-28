"""Explicit device preparation using the app's bundled client."""
from pathlib import Path


def prepare_controller(client, options, emit, *, clock=None, sleep=None, wait_s=120., cancelled=lambda:False):
    # Only an explicit check-button request authorizes the selected preparation stages.
    if not all(options.get(key) is True for key in ('prepare_controller', 'confirm_motion', 'motor_ids_confirmed')):
        raise ValueError('检查并准备主控需要本次明确的启动、初始化及当前电机配置许可')
    grippers = options.get('prepare_grippers') is True
    if grippers and options.get('accept_unverified_gripper_bus') is not True:
        raise ValueError('夹爪准备需要接受当前共享总线配置')
    from teleop.robots.kuavo5_current_wizard import PreparationWizard
    from teleop.robots.kuavo5_wizard_receipts import ReceiptStore
    from .kuavo_readiness import require_hardware_ready

    class ControllerCheck(PreparationWizard):
        def check_cancelled(self):
            if cancelled():raise RuntimeError('检查已结束，未请求下一步；已发出的准备请求需查询结果')

        def service(self, **kwargs):
            self.check_cancelled()
            status = super().service(**kwargs)
            expected = options.get('expected_gripper_sha256')
            from teleop.robots.kuavo5_service_contract import CONFIGURABLE_SESSION_PROTOCOLS, validate_service_contract
            online_parameters = (status.get('session_contract') or {}).get('protocol') in CONFIGURABLE_SESSION_PROTOCOLS
            if grippers and online_parameters:
                # Runtime settings derive a new gripper digest. Validate their
                # complete signed-channel descriptor and retain wizard pins;
                # an old PC calibration copy is not the runtime configuration.
                validate_service_contract(status)
            elif grippers and expected and status.get('profile', {}).get('gripper_config_sha256') != expected:
                raise ValueError('本机夹爪参数尚未应用到机器人服务；请先核对配置')
            return status

        def confirm(self, message):
            # The UI click authorizes only the stages selected in options.
            self.check_cancelled()

        def request(self, action):
            self.check_cancelled()
            allowed = ('status', 'start', 'initialize', 'grippers_inspect', 'grippers_prepare') if grippers else ('status', 'start', 'initialize')
            if action not in allowed:
                raise ValueError('本次检查未授权此准备操作')
            if action == 'grippers_prepare' and action in self.sent:
                raise ValueError('本轮夹爪准备已执行或已恢复原回执，仍未就绪；不会重复设置')
            if action != 'status':
                self.say({'start':'正在启动厂家主控', 'initialize':'正在执行厂家初始化',
                          'grippers_inspect':'正在检查左右夹爪电机模式与通信保护',
                          'grippers_prepare':'正在准备夹爪模式与通信保护，并核对读回值'}[action])
            result = super().request(action)
            if action.startswith('grippers_'):
                emit({'gripper_preparation':result})
            return result

        def snapshot(self):
            state = super().snapshot()
            emit({'preparation':state})
            return state

    def forbidden(*args, **kwargs):
        raise RuntimeError('检查设备不会进入遥操作或视频流程')

    timers = {}
    if clock is not None:timers['clock'] = clock
    if sleep is not None:timers['sleep'] = sleep
    # This path moves with the app/config and never references Teleoperation.
    directory = Path(options['config']).parent/'preparation-receipts'
    with ReceiptStore(directory) as receipts:
        wizard = ControllerCheck(client, receipts, prepare_network=forbidden,
            check_pico=forbidden, execute=forbidden, ask=forbidden,
            say=lambda message:emit({'preparation_progress':str(message)}), wait_s=wait_s,
            bus_accepted=grippers, **timers)
        wizard.service(idle=False)
        pending = receipts.pending()
        allowed_pending = ('start', 'initialize', 'grippers_inspect', 'grippers_prepare') if grippers else ('start', 'initialize')
        if pending and pending['action'] not in allowed_pending:
            raise ValueError('存在其它未确认准备请求，请先核对原请求；未发出新动作')
        wizard.recover()  # Lost replies: query the persisted ID, never resend.
        wizard.controller()  # Skip ready stages; each action at most once.
        state = wizard.snapshot()
        require_hardware_ready(state)
        if grippers:
            if not wizard.grippers(wizard.request('grippers_inspect')):
                wizard.snapshot()
                if not wizard.grippers(wizard.request('grippers_prepare')):
                    raise ValueError('设置后夹爪仍未就绪，不重复设置')
            state = wizard.snapshot()
            require_hardware_ready(state)
            emit({'gripper_preparation_ready':True,
                  'preparation_progress':'厂家主控、初始化及左右夹爪准备检查已通过'})
        else:
            emit({'preparation_progress':'厂家主控与初始化已就绪'})
        return state
