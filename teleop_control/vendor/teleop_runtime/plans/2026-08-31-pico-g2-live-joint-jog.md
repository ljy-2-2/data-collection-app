# PICO → G2 单关节连续控制会话

Status: implemented and live path reverified (2026-09-01)

## What

- Problem / goal: 消除 FIFO 旧指令堆积，让按住右 Grip 和摇杆可以稳定连续控制所选关节，
  并允许从手柄安全退出单关节模式。真机验证发现 G2 还有单关节限位之外的腕部耦合约束，
  需要在 G2Adapter 发送前主动拦截，避免 GDK 拒绝后结束整个会话。
- User-visible behavior: PICO 仍以 50 Hz 采集，但 PC 只保存最新控制状态；G2 以 10 Hz
  使用最新状态控制。手臂最高 0.2 rad/s，头部和身体最高 0.1 rad/s。松开 Grip 或摇杆
  回中立即停止刷新目标；二者均处于安全状态时长按 B 1 秒退出。
- Success criteria:
  - 新协议传递关节、方向、deadman、退出请求、序号和时间戳，旧状态被新状态覆盖而不排队执行。
  - 保持 Grip+摇杆时按 10 Hz 连续产生目标；松开、回中、输入超过 200 ms 或退出时不再发送。
  - 手臂每周期最多 0.02 rad、GDK 速度最多 0.2 rad/s；头部/身体分别最多 0.01 rad 和
    0.1 rad/s；最终 G2Adapter 边界再次强制这些上限，TTL 仍最多 100 ms。
  - 只有 X/A 松开、Grip 松开且摇杆回中时，连续按 B 满 1 秒才退出；短按、选择关节时或
    运动中按 B 不退出。
  - 只有 Grip 松开且摇杆回中时才允许 X/A 切换关节；读取 G2 状态期间若收到更新的 PICO
    状态，则旧状态在发送前作废。
  - 退出会结束 live worker 并释放 GDK；只读 preview、旧单步映射和现有测试不回退。
  - 已由 2026-08-31 真机日志确认的左腕约束
    `-q(idx26_arm_l_joint6) + q(idx27_arm_l_joint7) <= 1.92` 在 G2Adapter 内检查；
    软件使用 `0.001 rad` 保护余量，目标组合值超过 `1.919` 时不调用 GDK。
  - 耦合边界只拒绝当前方向，不结束 live 会话；终端只打印一次涉及关节、目标组合值、
    厂商上限、软件上限和 `command_not_sent`。Grip 松开或摇杆回中后解除提示锁存，允许向
    安全方向恢复。未分类的 GDK/通信/硬件错误仍结束会话。
- Out of scope: 不修改 Unity，不修改旧录制代码，不同时运行 recording 与 joint-jog，不实现 IK、
  视频、底盘或腿部控制；不猜测右腕或其他未由厂商资料/真机错误确认的约束，也不在本次
  离线实现阶段驱动真机。

## How

- Approach: 保留 XRFrame 和 JointJogMapper；新增 live 控制状态协议。输入线程持续读取并覆盖
  单槽 mailbox，控制线程每 100 ms 只取最新快照。方向为 0、deadman 释放、状态过期或退出
  时跳过发送。ControlRouter、DirectJointBackend、SafetyGateway 和 RobotAdapter 路径保持不变。
  G2Adapter 用完整实时关节状态加本次目标组成候选状态，检查可扩展的线性耦合约束；专用异常
  携带结构化终端字段，live worker 对该异常锁存并继续，对其他异常保持 fail-closed。
- Components / files touched: `src/teleop/retargeting/joint_jog.py`、`src/teleop/robots/g2.py`、
  `apps/pico_g2_live.py`、共享 launcher 的 emitter 配置、对应测试、README 和本文件。
- Phases: 先协议/映射，再 mailbox/控制循环，再 G2 分组上限和退出清理，最后离线端到端验证。
- Risks / unknowns: 旧 `test-teleop/send.py` 用 B 停止录制；因此两个 PC 模式必须互斥。
  GDK 完整耦合约束表不在本地公开配置中；当前只启用真机错误明确给出的左腕约束，后续获得
  厂商表后按同一数据结构扩展。真实 10 Hz 行为需在代码完成后另获当次真机确认再验证。
- Safety boundary: 本轮只修改 PC runtime 并使用 fake GDK；不修改 Unity，不连接或运动 G2。
- Verification plan: TDD 覆盖长按 B 条件、协议验证、mailbox 覆盖、10 Hz 重复、200 ms watchdog、
  分关节速度/步长最终边界和清理；新增 TDD 覆盖真实失败数值、保护余量边界、GDK 零调用、
  单次终端提醒、回中解锁和未知错误仍退出；运行完整单元测试、语法检查、launcher 假进程测试
  和独立审查。

## Verification

- 2026-08-31 修复前真机证据：连续控制成功发送 47 条指令；左腕第 6 关节负向目标
  `[-0.560798, 1.359372]` 触发 GDK 的 `-q26 + q27 <= 1.92` 拒绝并结束会话。
- 修复后完整离线测试 `128/128` 通过。
- Python 编译检查与 3 个 launcher 的 `bash -n` 检查通过。
- fake GDK 可见联合运行观察到：危险方向零发送、结构化终端提醒一次、回中解锁、反向连续
  发送 3 条、B 退出并释放 GDK；未知状态读取/厂商构造/字段赋值/发送错误均 fail-closed。
- 独立复审确认精确 `1.919` 边界、GDK 最终输入校验、致命错误分类和耦合锁存均无遗留发现。
- 后续操作者监督的真机会话再次确认连续关节发送、22 关节菜单选择和长按 B 安全退出；
  未再出现会话级致命错误。精确耦合边界的危险方向零发送与提示锁存仍由自动化 fake-GDK
  测试覆盖，不声称对未公开的其他耦合约束进行真机穷举。
