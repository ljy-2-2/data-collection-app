"""Explicit PC workflow. All I/O is injected; importing this module is inert.

Preparation requests use the installed robot implementation. This module owns
no motion parameters, CAN implementation, controller launcher or retry policy.
"""
import copy
import re
import time


IDENTITY_KEYS = ('service_instance_id', 'installation_sha256', 'bundle_sha256')
PREVIEW = '''PC 向导（流程预览，未连接机器人）
1. 检查 PC 网络，连接机器人正式服务。
2. 显示主控状态；需要启动、初始化时分别等待你输入 YES。
3. 检查左右夹爪；需要设置准备参数时，再等待 YES。
4. 检查 PICO 新鲜输入，显示准备结果；输入 YES 开始遥操。
5. 默认不限时；Ctrl+C 结束本轮遥操，厂家主控继续运行。
准备途中退出或丢失回复：保留请求编号，下次只查询原请求，不重做动作。
这只是入口整合，不修改双臂速度、肘部偏好或夹爪开合参数。'''


class WizardCancelled(Exception):
    pass


class PreparationWizard:
    def __init__(self, client, receipts, *, prepare_network, check_pico, execute,
                 ask=input, say=print, clock=time.monotonic, sleep=time.sleep,
                 wait_s=120., bus_accepted=False):
        self.client, self.receipts = client, receipts
        self.prepare_network, self.check_pico, self.execute = prepare_network, check_pico, execute
        self.ask, self.say, self.clock, self.sleep = ask, say, clock, sleep
        self.wait_s, self.bus_accepted = wait_s, bus_accepted
        self.identity = None
        self.contract_sha256 = None
        self.contract_protocol = None
        self.controller_identity = None
        self.ready_context = None
        self.sent = set()
        self.last_progress = float('-inf')

    def confirm(self, message):
        if self.ask(message + '\n输入 YES 继续，其他输入退出：').strip() != 'YES':
            raise WizardCancelled('已退出向导，未请求下一步。')

    def service(self, *, idle=True):
        report = self.client.get_status()
        if report.get('management_ready') is not True:
            raise ValueError('机器人服务不兼容或未准备好接收请求：' + str(report.get('compatibility_error')))
        status = report['service']
        identity = {k: status.get(k) for k in IDENTITY_KEYS}
        for key, value in identity.items():
            size = 32 if key == 'service_instance_id' else 64
            if not isinstance(value, str) or re.fullmatch('[0-9a-f]{%d}' % size, value) is None:
                raise ValueError('机器人服务版本标识不完整')
        if self.identity is None:
            self.identity = identity
        elif self.identity != identity:
            raise ValueError('另一窗口更新了正式版本或服务已重启；本次向导停止，请重新核对版本。')
        contract = status.get('session_contract') or {}
        digest = contract.get('contract_sha256')
        if digest is not None:
            if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
                raise ValueError('机器人参数契约标识不完整')
            if self.contract_sha256 is None:
                self.contract_sha256 = digest
                self.contract_protocol = contract.get('protocol')
            elif digest != self.contract_sha256:
                raise ValueError('准备期间参数已被另一入口修改；请重新查看并确认，未继续启动。')
        elif self.contract_sha256 is not None:
            raise ValueError('机器人参数契约丢失；停止本轮准备。')
        from .kuavo5_controller_admin import PREPARATION_ACTIONS, PREPARATION_PROTOCOL
        if (status.get('backend') != 'robot'
                or status.get('robot_preparation_protocol') != PREPARATION_PROTOCOL
                or not set(PREPARATION_ACTIONS) <= set(status.get('robot_preparation_actions', []))):
            raise ValueError('当前正式服务尚未接齐启动、初始化和夹爪准备接口')
        if idle and (status.get('phase') != 'idle' or status.get('owner_present') is not False
                     or status.get('blocked_reason') or status.get('robot_preparation_busy')):
            raise ValueError('机器人服务被占用或仍在准备；不会接管另一轮操作。')
        return status

    def pins(self, *, preparation=False):
        pins = dict(expected_service_instance_id=self.identity['service_instance_id'],
                    expected_installation_sha256=self.identity['installation_sha256'])
        from .kuavo5_service_contract import CONFIGURABLE_SESSION_PROTOCOLS
        if preparation and self.contract_protocol in CONFIGURABLE_SESSION_PROTOCOLS:
            pins['expected_contract_sha256'] = self.contract_sha256
        return pins

    def progress(self, event):
        now = self.clock()
        if event.get('phase') == 'pending' and now - self.last_progress >= 2.:
            self.say('机器人仍在处理 %s，已等待 %.0f 秒；不会重复发送。' %
                     (event.get('action', '准备请求'), event.get('elapsed_s', 0.)))
            self.last_progress = now

    def body(self, reply, action):
        # The helper result is nested inside the authenticated service receipt.
        if reply.get('event') != 'preparation_result' or reply.get('action') != action:
            raise ValueError('准备回执格式不正确；不能认定准备完成')
        helper = reply.get('result') or {}
        body = helper.get('result') or {}
        error = reply.get('error') or {}
        if not helper and error and error.get('outcome_unknown') is False:
            raise ValueError('机器人准备失败：' + str(error.get('message') or error))
        if (not isinstance(body, dict) or type(helper.get('ok')) is not bool
                or type(helper.get('outcome_unknown')) is not bool):
            raise ValueError('准备结果不是完整记录')
        if (error.get('outcome_unknown') is True or helper.get('outcome_unknown') is True
                or body.get('outcome_unknown') is True):
            raise ValueError('准备结果尚未确认；保留原请求编号，不自动重做。')
        inspection_not_ready = (action == 'grippers_inspect' and helper.get('ok') is False
                                and not helper.get('error') and error.get('type') == 'PreparationFailed'
                                and error.get('outcome_unknown') is False)
        if (error and not inspection_not_ready) or helper.get('error'):
            raise ValueError('机器人准备失败：' + str(reply.get('error') or helper.get('error')))
        if helper.get('ok') is not True and action != 'grippers_inspect':
            raise ValueError('机器人未确认这一步完成：' + action)
        if helper.get('helper_exit_code', 0) != 0:
            raise ValueError('准备程序异常退出，不能把返回内容当作准备完成')
        return body

    def settle(self, record, reply):
        """Clear only complete, known outcomes; retain ambiguous receipts."""
        helper = reply.get('result') or {}
        body = helper.get('result') or {}
        error = reply.get('error') or {}
        known_refusal = not helper and error and error.get('outcome_unknown') is False
        if (reply.get('event') != 'preparation_result'
                or any(reply.get(k) != record[k] for k in
                       ('request_id', 'action', 'service_instance_id', 'installation_sha256'))
                or not isinstance(body, dict)
                or (not known_refusal and (type(helper.get('ok')) is not bool
                                          or type(helper.get('outcome_unknown')) is not bool))
                or (reply.get('error') or {}).get('outcome_unknown') is True
                or helper.get('outcome_unknown') is True or body.get('outcome_unknown') is True):
            raise ValueError('准备结果未确认；保留请求 ' + record['request_id'] + '，不会重复执行。')
        self.receipts.resolve(record)
        return self.body(reply, record['action'])

    def request(self, action):
        self.service()
        if action == 'status':
            return self.body(self.client.preparation('status', **self.pins(preparation=True),
                             on_progress=self.progress), 'status')
        record = self.receipts.begin(action, self.identity)
        self.say('正在请求 %s；请求编号 %s' % (action, record['request_id']))
        from .kuavo5_direct_channel import PreparationOutcomeUnknown
        try:
            reply = self.client.preparation(action, request_id=record['request_id'],
                confirm_motion=action in ('start', 'initialize', 'grippers_prepare'),
                motor_ids_confirmed=action == 'start',
                accept_unverified_gripper_bus=action.startswith('grippers_') and self.bus_accepted,
                on_progress=self.progress, **self.pins(preparation=True))
        except PreparationOutcomeUnknown:
            self.say('回复中断，原请求已记录。再次运行向导时只查询它，不重新发送。')
            raise
        except (OSError, ValueError, RuntimeError, EOFError, KeyboardInterrupt):
            # The direct channel converts every post-dispatch exception to
            # PreparationOutcomeUnknown. Other failures prove no dispatch.
            self.receipts.resolve(record)
            raise
        self.sent.add(action)
        return self.settle(record, reply)

    def recover(self):
        record = self.receipts.pending()
        if record is None:
            return
        if any(record[k] != self.identity[k] for k in IDENTITY_KEYS):
            raise ValueError('上一请求属于旧服务或旧版本，结果仍需核对；本向导不会重新发起动作。')
        self.say('发现上一轮未确认的请求 %s；只查询原结果。' % record['request_id'])
        deadline = self.clock() + self.wait_s
        while True:
            reply = self.client.preparation_job(record['request_id'], **self.pins(), on_progress=self.progress)
            if reply.get('event') == 'preparation_result':
                self.settle(record, reply)
                self.sent.add(record['action'])
                return
            if reply.get('event') != 'preparation_pending' or self.clock() >= deadline:
                raise ValueError('原请求仍未确认；请求编号已保留，不重新执行。')
            self.sleep(.5)

    def snapshot(self):
        result = self.request('status')
        # UI compatibility with a pre-fix deployed preparation observer.
        manager = result.get('manager', {}).get('phase')
        controller = result.get('controller', {})
        if result.get('phase') == 'not_started' and manager in ('process_running', 'unmanaged_controller'):
            result['phase'] = 'starting' if manager == 'process_running' else 'unavailable'
        identity = controller.get('controller_identity')
        if identity:
            if self.controller_identity is None:
                self.controller_identity = copy.deepcopy(identity)
            elif identity != self.controller_identity:
                raise ValueError('厂家主控已换成另一进程；停止本轮准备，不自动初始化新进程。')
        if self.ready_context is not None and (
                result.get('phase') != 'ready' or result.get('context') != self.ready_context):
            raise ValueError('主控或夹爪通信状态在准备后发生变化；请重新核对。')
        return result

    def controller(self):
        deadline = self.clock() + self.wait_s
        previous, last_print = None, float('-inf')
        labels = {'not_started': '主控未启动', 'starting': '主控启动中，等待节点出现',
                  'awaiting_confirmation': '主控等待初始化确认', 'preparing': '初始化进行中',
                  'ready': '主控准备检查通过'}
        while True:
            state = self.snapshot()
            phase = state.get('phase')
            now = self.clock()
            if phase != previous or now - last_print >= 5.:
                self.say(labels.get(phase, '主控状态：' + str(phase)))
                previous, last_print = phase, now
            if phase == 'ready':
                context = state.get('context')
                if not self.controller_identity or not isinstance(context, dict) or context.get('controller') != self.controller_identity:
                    raise ValueError('主控就绪报告缺少进程身份，不能继续')
                self.ready_context = copy.deepcopy(context)
                return
            if phase == 'not_started':
                if self.sent & {'start', 'initialize'}:
                    raise ValueError('请求后主控仍未启动或已退出；不重复启动。')
                self.confirm('即将按正式配置启动厂家主控，可能发生动作。请确认现场可启动，'
                             '且已核对当前自研夹爪电机标识（左 8、右 1）。')
                latest = self.snapshot()
                if latest.get('phase') != 'not_started' or latest.get('manager', {}).get('phase') != 'not_started':
                    raise ValueError('确认期间主控状态已改变；没有发送启动请求，请重新检查。')
                result = self.request('start')
                if result.get('accepted') is not True:
                    raise ValueError('启动请求未被接受')
                deadline = self.clock() + self.wait_s
            elif phase == 'awaiting_confirmation' and 'initialize' not in self.sent:
                self.confirm('即将调用原来 o 对应的初始化功能，可能发生动作。请确认现场允许初始化。')
                latest = self.snapshot()
                if latest.get('phase') != 'awaiting_confirmation':
                    raise ValueError('确认期间初始化状态已改变；没有发送初始化请求，请重新检查。')
                if self.request('initialize').get('accepted') is not True:
                    raise ValueError('初始化请求未被接受')
                deadline = self.clock() + self.wait_s
            elif phase not in ('starting', 'preparing', 'awaiting_confirmation'):
                raise ValueError('主控尚不能准备：%s。不会重启或接管已有主控。' % phase)
            if self.clock() >= deadline:
                raise TimeoutError('等待主控状态更新超时；不会重复启动或初始化。请先查询状态。')
            self.sleep(.5)

    def grippers(self, body):
        answer = body.get('result') or {}
        if (not isinstance(answer, dict) or answer.get('error') or answer.get('outcome_unknown') is True
                or answer.get('clients_closed') is not True or answer.get('cleanup_errors')):
            raise ValueError('夹爪检查未完整结束或通信清理未确认，不能开始遥操。')
        ready = True
        for side, label in (('left', '左夹爪'), ('right', '右夹爪')):
            detail = answer.get('sides', {}).get(side, {})
            state = detail.get('status')
            if (detail.get('error') or detail.get('cleanup_error')
                    or state not in ('ready', 'needs_preparation')
                    or (state == 'ready' and detail.get('ready') is not True)):
                raise ValueError('%s检查失败：阶段 %s；%s' %
                                 (label, detail.get('stage', '未知'), detail.get('error') or detail.get('cleanup_error') or state))
            is_ready = detail.get('ready') is True and state == 'ready'
            ready = ready and is_ready
            self.say(label + ('：准备检查通过' if is_ready else '：需要设置准备参数'))
        if ready and answer.get('ready') is not True:
            raise ValueError('夹爪总结果与左右结果不一致')
        return ready

    def run(self):
        self.say('连接与准备向导：输入 YES 才执行需确认的步骤；Ctrl+C 退出本轮。')
        self.prepare_network()
        status = self.service(idle=False)
        self.say('当前正式版本：' + self.identity['installation_sha256'][:12])
        settings = (status.get('session_contract') or {}).get('session_settings')
        if settings:
            values = settings['values']
            self.say('本轮参数版本 %d。' % settings['revision'])
            if 'left_arm_speed_rad_s' in values:
                for side, label in (('left', '左臂'), ('right', '右臂')):
                    self.say('%s：位置比例 %.3g，关节速度 %.3g rad/s，规划加速度 %.3g rad/s²。' %
                        (label, values[side+'_arm_scale'], values[side+'_arm_speed_rad_s'],
                         values[side+'_arm_acceleration_rad_s2']))
            else:
                self.say('双臂速度 %.3g rad/s。' % values['arm_speed_rad_s'])
            self.say('左右夹爪开合速度 %.3g / %.3g rad/s。' %
                     (values['left_gripper_speed_rad_s'], values['right_gripper_speed_rad_s']))
            for side, label in (('left', '左夹爪'), ('right', '右夹爪')):
                self.say('%s基准目标角：张开 %.6g rad，闭合 %.6g rad。' %
                         (label, values[side+'_gripper_open_target_rad'],
                          values[side+'_gripper_closed_target_rad']))
            self.say('基准角沿用原配置坐标，上电偏移由程序自动换算；'
                     '不是夹口几何角，也不要把本次上电的原始电机读数直接填作目标。')
        self.recover()
        self.controller()
        if not self.bus_accepted:
            self.confirm('下一步将读取左右夹爪电机。当前共用 CAN 的兼容性尚未完成独立验收；'
                         '确认使用这套已接线的正式夹爪配置继续检查？')
            self.bus_accepted = True
        self.snapshot()
        if not self.grippers(self.request('grippers_inspect')):
            self.confirm('夹爪需要设置上电后电机通信参数，并重新读取确认。' +
                         ('保留本轮已确认的工作参数。' if settings else '开合范围继续使用机器人正式配置。'))
            self.snapshot()
            if not self.grippers(self.request('grippers_prepare')):
                raise ValueError('设置后夹爪仍未就绪，不重复设置')
        self.say('检查 PICO 连接和新鲜输入……')
        self.check_pico()
        self.snapshot()
        self.confirm('主控、左右夹爪准备检查与 PICO 检查已通过。开始双臂＋夹爪遥操？'
                     '默认不限时；运行后按 Ctrl+C 结束本轮，主控继续运行。')
        # A long confirmation/PICO check may invalidate previous observations.
        self.snapshot()
        if not self.grippers(self.request('grippers_inspect')):
            raise ValueError('开始前复查发现夹爪未就绪；不会自动重新设置或开始。')
        self.snapshot()
        self.service()
        selected = copy.deepcopy(self.identity)
        if self.contract_sha256 is not None:
            selected['contract_sha256'] = self.contract_sha256
        return self.execute(selected)
