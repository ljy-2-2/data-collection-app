# teleop-runtime

本目录是 `projects/Teleoperation/` 总项目中的 PC 运行时。如果不知道 PICO、G2、视频和控制应该按什么顺序启动，请先严格按照上一级 [`README.md`](../README.md) 的“新手完整启动”执行；本文件保留各模块的技术细节和独立调试命令。

This directory is the new robot-independent PC runtime. Phase 1 implements the
robot-independent offline direct-joint path:

```text
JointIntent -> ControlRouter -> DirectJointBackend -> JointCommand
            -> SafetyGateway -> FakeRobot
```

Phase 2 adds a dry-run-only G2 adapter while keeping the same core:

```text
JointIntent -> ControlRouter -> DirectJointBackend -> JointCommand
            -> SafetyGateway -> RobotAdapter -> G2Adapter request preview
```

The existing `../test-teleop/` baseline is intentionally untouched and is retained only
as a legacy reference for retargeting, URDF IK, dual-arm configuration, and recording.
The current runtime never imports or launches it. Later sections document the added
PICO input, direct-joint, G2 adapter, robot-video, and explicit live-control stages.

## Run the tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## 双臂末端与夹爪路线（已完成并冻结）

旧 `test-teleop/` 中已经使用过的双臂末端映射和 Pinocchio URDF IK，现已按新架构迁入本目录：

```text
XRFrame
  -> DualArmRetargeter
  -> EndEffectorPoseIntent（左手 / 右手 / 双手）
  -> ControlRouter
  -> G2Adapter.solve_ik()
  -> G2URDFIKSolver
  -> JointCommand（每只手臂 7 个关节）
  -> SafetyGateway

XRFrame 左/右 Trigger
  -> GripperMapper
  -> GripperIntent（统一 0..1）
  -> ControlRouter
  -> GripperCommand
  -> GripperSafetyGateway
  -> RobotAdapter.send_gripper_command()
```

`RobotAdapter.solve_ik()` 只是统一入口，不是 IK 算法本身。当前代码由启动入口创建一个 `IKSolver`，再通过构造参数注入 Adapter；`RobotAdapter` 不会自动发现或选择求解器。G2 入口默认注入 `G2URDFIKSolver（Pinocchio + G2 URDF + G2 基座/末端坐标系与双臂关节配置）`，并未使用 G2 厂商 IK。未来跨厂商目标策略是：由 Adapter Factory 在会话启动时选择已经接入、验证并显式启用的官方 `IKSolver(kind="native")`；否则注入该机器人专用的 URDF IKSolver。当前项目没有一个“只替换任意 URDF 即可工作”的通用 `URDFIKSolver`，也没有具体厂商 IK 实现。求解器一旦注入，运行期间不静默切换。夹爪命令始终绕过 IK，单独经过 `GripperSafetyGateway`。

头部路线只跟随左右转头、抬头低头，不跟随歪头或平移。输入、方向、连续控制和统一会话已经接通；软件按 yaw 绝对 `[-8°, +45°]` 的安全范围受限冻结。操作者也在 G2 本机操作界面复现了右转受阻，因此完整右转范围属于 G2 本体/控制器待厂家检修项，不是 PICO/PC 映射缺陷。操作说明见下方“PICO 连续头部跟随”，冻结边界见 [`docs/milestones/pico-g2-teleoperation-v1-1.md`](docs/milestones/pico-g2-teleoperation-v1-1.md)。

### 头部角度预览（不连接 G2、不发送运动）

当前位置：`XRFrame.head_pose → HeadFollowMapper → HeadOrientationIntent → 终端`。
这是输入/映射部分，尚未接到 `ControlRouter → SafetyGateway → RobotAdapter`。
程序从头显四元数提取看向的方向，计算相对起点的左右/上下角度，忽略歪头和平移。
G2 期望方向变化采用 1:1 比例；不是已经标定的 G2 关节绝对角度，不会创建 JointCommand。

**只需 PICO，不需要 G2 网络、视频桥或运动程序。** 先结束正在运行的真机遥操作会话，避免把其他入口产生的运动误认为本预览造成的。
如 PICO 未连接，按上一级 README 的“第 1 步：启动 PICO 数据接收服务”和“第 2 步：连接 PICO 并恢复端口”操作；不要执行其第 3 步或运动入口。确认应用为 `WORK`，并打开 `Head` 和 `Send Data`。

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
PYTHONPATH=src /home/yuan/miniforge3/envs/g2-teleop-py310/bin/python \
  apps/observe_head_follow.py --duration 60
```

1. 启动后有 3 秒准备时间，看向舒适的前方。出现 `status=anchored` 才表示零点已记录；程序不要求任何 Grip/Trigger/录制按键。
2. 左转约 10°：`pico_left_deg` 和 `g2_requested_left_deg` 应同为约 `+10`；右转为负。
3. 回到起点，再抬头约 10°：`pico_up_deg` 和 `g2_requested_up_deg` 应同为约 `+10`；低头为负。
4. 保持视线方向，轻轻歪头：上述两个值应基本不变。人体实际动作可能同时包含转头，因此不要要求人为歪头时数值绝对为零。
5. 所有输出都应有 `physical_robot_command_sent=False`。Ctrl-C 退出；重启预览会重新取起点。

预览默认采样 50 Hz、打印 5 Hz。`--duration` 可改成需要的秒数，但必须大于 `--anchor-delay`（默认 3 秒）。
无数据、超过 300 ms 的 stale 或无效姿态时，不显示新的有效预览；恢复后重新取零点并提示。几乎竖直看天/看地时左右角度不确定，会暂停预览。PICO 重置坐标原点后，请重启本预览。
`Head` 关闭时输入协议未必提供独立的头部追踪有效标志；不能只凭总体时间戳判定头部正在更新，必须实际转头核对数字。

没有设备时可验证确定的数学样例（不导入 PICO SDK）：

```bash
PYTHONPATH=src /home/yuan/miniforge3/envs/g2-teleop-py310/bin/python \
  apps/observe_head_follow.py --demo
```

它展示左右/上下各 10°、歪头 20° 不跟随；这只是合成数据验证，不代表真实 PICO 或 G2 已验收。

### PICO + G2 头部关节候选预览（读取真机，不发送运动）

当前位置：`HeadOrientationIntent → G2 头部关节映射 → schema 限位 → 终端候选值`。
这一项会读取一帧真实 G2 头部状态，但不会创建 `JointCommand`，也没有注入 GDK 命令发送器。
不需要按 Grip、Trigger 或其他手柄键，机器人不会跟随运动。

先按上一级 README 启动 `RoboticsServiceProcess`，确认 PICO 应用为 `WORK` 且 `Head`、`Send Data` 已打开。然后运行：

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime

sudo ../../g2-dp-release/scripts/g2-network up

bash apps/run_pico_g2_head_preview.sh \
  --duration 60 \
  --hz 10
```

启动时以第一帧新鲜 PICO 方向和当时的 G2 头部状态为共同零点。终端含义：

- `pico_left_deg`、`pico_up_deg`：相对零点的头显左右/上下角度。
- `idx11_current/target`：G2 左右轴的起点和候选目标。
- `idx12_hold`：只显示 G2 歪头轴当前值；该轴不在候选命令目标中。
- `idx13_current/target`：G2 上下轴的起点和候选目标。
- `clamped`：候选角度是否碰到 G2 schema 限位。

当前依据 URDF 轴和相机姿态采用临时符号假设：左转对应 `+idx11`，抬头对应 `-idx13`。
只读数据无法证明机器人的实际转动方向，因此输出明确保留
`direction_calibrated=False`。成功结束应看到 `previews>0`、
`state_available_at_end=True`，并且所有输出始终是
`joint_command_created=False physical_robot_command_sent=False`。

联合预览已由操作者运行：收到 259 帧、244 次候选预览，stale/rejected/state_unavailable 均为 0，Ctrl-C 正常退出。
这次未跑满 60 秒，确认了候选计算与限位；实际转动方向已在后续独立 5° 核对中由操作者确认。

### 头部未观察到运动时：只读诊断（不让机器人动）

2026-09-08 至 09：12 次独立/联合真机日志都在右转约 -10.3° 至 -10.8° 后跳到约 -169°；另有一次在本程序发送 0 条命令时，启动反馈已是约 -169°且错误码为 512。主机检查未发现残留遥操作/GDK进程或自动启动项。2026-09-09，操作者在 G2 本机操作界面复现相同右转受阻，进一步确认故障不在 PICO/PC 控制链。软件将右侧隔离在 -8°；具体硬件或控制器根因仍需厂家检查，不能关闭保护继续运行。

这是故障排查工具，不是每次启动的必做步骤。后续 5° 测试已得到操作者方向确认；若再次出现“日志变化、肉眼不动”，可先用下面的只读诊断，不反复执行运动命令。
不需要戴 PICO、不需要按手柄，也不要手动掰动机器人头部。先退出其他运动程序。

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
sudo ../../g2-dp-release/scripts/g2-network up
sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_head_diagnostics.sh --duration 10 --hz 2
```

默认采样约 10 秒（另有初始化及状态读取时间）；Ctrl-C 退出。厂商调用阻塞时不保证立即返回。
入口没有运动/使能/回零接口，也不接受 `--enable-motion`。输出含：

- `motor_position` / `motor_position_deg`：厂家电机位置，分别为弧度/角度；不是目标值，不单独证明相机实际运动。
- `position` / `velocity`：厂家另一组可选原始字段；`position_minus_motor_rad` 只用于对照，不自动替代或修正反馈。`joint_limits_rad` 来自已有 G2 关节表，越限显示 `motor_position_outside_joint_limits` 并返回非零退出码。
- `motor_velocity`、`motor_current`、`mode`、`error_code`：原始速度、电流、模式和错误码；不擅自解释未确认的模式编码。
- `source_timestamp_ns` / `source_progress`：厂家时间戳及推进情况。`advanced` 表示时间戳增加；`unchanged` 表示重复，`regressed` 表示回退，`unknown` 表示缺失/无效。推进不等同于已证明反馈足够新鲜。
- `read_ms`：本次状态读取耗时，不是运动发送耗时。原始时钟不与电脑时钟直接相减。
- `raw_source_timestamp_repr` / `raw_head_rows_repr`：原始值的文本表示，即使时间戳无效、字段缺失或行重复，也保留证据，避免只剩下空值。
- `raw_joint_rows_repr`：全部原始关节行，可核对错误值是否来自其他关节。
- `status`：启动时读取一次可用的全身/运动状态；接口不存在或失败会显示 `unavailable`，不会伪造正常。
- `summary`：样本数、异常样本数，始终 `physical_robot_command_sent=false`、`physical_direction_verified=false`。

退出码 0 表示关节样本基础检查未发现异常，**不表示运动通过验收**；2 表示读取失败、重复时间戳/异常样本等需要检查；130 表示用户退出。可选状态不可用会明确提示，但不单独令退出码变为 2。
请把完整输出交给诊断人员，先解释反馈与现场观察的不一致，再决定下一次运动验证。

### G2 头部小角度方向核对（操作者已确认两轴方向）

2026-09-07：5° 测试中，两轴反馈变化均约 4.95°；操作者确认正 yaw 向机器人自身左侧、负 pitch 抬头。这个结论适用于本次检查的安装与姿态，不等于任意 roll 下的相机标定。以下工具保留用于重新核对，不需要日常重复运行。

这个独立工具用于确认关节正负方向，不需要 PICO、手柄或 IK。
启动时读取真实位置，以此为起点；每次只选左右轴 `yaw=idx11` 或上下轴
`pitch=idx13`，最大关节变化 5°，速度最高 0.05 rad/s（约 2.86°/秒）。
每次发送目标相对新读取反馈最多变化 0.004 rad，发送间隔至少 0.1 秒；
反馈不可用、过期、异常跳变或其他头部轴明显移动时停止后续发送。
它复用 `JointPositionIntent → ControlRouter → DirectJointBackend → SafetyGateway → G2Adapter`。

先退出正在运行的单关节/双臂控制会话；工具与统一入口共用会话锁。
它不能阻止绕过该锁的其他厂商控制软件，请确保没有其他控制者。
首先查看计划（以下两条都是只读，不发送运动）：

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
sudo ../../g2-dp-release/scripts/g2-network up

sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_head_direction.sh --axis yaw

sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_head_direction.sh --axis pitch
```

真机步骤须在操作者当次确认、周围清空并备好硬件急停后执行。
下面命令会让机器人运动，逐条执行、观察完一条再执行下一条：

```bash
# 左右轴：相对启动位置 +5°，预期机器人向自身左侧转，需人观察确认
sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_head_direction.sh --axis yaw --delta-deg 5 --enable-motion

# 上下轴：相对启动位置 -5°，预期抬头，需人观察确认
sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_head_direction.sh --axis pitch --delta-deg -5 --enable-motion
```

默认 pitch 为负方向，因为此前反馈 +0.523101 rad 已靠近正向上限。
不指定 `--delta-deg` 时仍默认 yaw +2° / pitch -2°；上面的显式命令才是 5°。
不要反复运行：每次都会从新的位置再变化指定角度。`--delta-deg` 绝对值不得超过 5°，
越过关节限位会拒绝而不是悄悄改变幅度。
`idx12` 不在命令中；完成后留在新位置，不自动回原位。

- `G2_HEAD_DIRECTION_PLAN`：起点、固定目标、速度和单步限制；角度字段 `*_rad` 单位为弧度。
- `G2_HEAD_DIRECTION_FEEDBACK`：每次实际读取的关节角度。
- `SEND_BEGIN` / `SEND_RETURN` / `SEND_ERROR`：将要发送的单步目标、接口返回/抛错及 `call_ms` 耗时。返回不代替现场方向观察；反馈 `read_ms` 是读取耗时。
- `reached=True`：反馈距目标不超过 0.001 rad；仅证明位置达到，不证明“左/上”的视觉方向。
- `attempts/accepted`：调用尝试数/SDK 调用正常返回数，不能代替实际反馈。
- `direction_calibrated=False`：工具不会自行认定相机方向校准成功，需要人观察反馈。

发送窗口最多 45 秒，以容纳多次同步规划调用；读取到的反馈连续 4 秒没有至少 0.0005 rad 的向目标进展时，停止后续发送并报告失败。
速度及每步幅度未提高。上述时限由 Python 循环检查，不能强行中断阻塞的
厂商调用；100 ms TTL 用于请求时效，也不能把它解释成硬件急停保证。
Ctrl-C 停止继续发指令；已经被 GDK 接受的那一小步仍可能完成，紧急情况使用硬件急停。
退出会释放会话锁，不需要删除锁文件或重启 ADB。

### G2 头部左右轴安全回正（默认只读）

这是异常排查后的独立恢复工具，不是连续头部跟随，也不是编码器重新标定。它读取
G2 当前三轴反馈，只计划把左右轴 `idx11_head_joint1` 回到现有坐标系的 `0°`；
`idx12`（歪头）和 `idx13`（抬头/低头）不会进入命令。

安全边界：连续读取 3 次反馈并检查三轴稳定、时间戳推进和头部错误码；当前左右偏转
绝对值超过 `15°`、反馈波动超过 `0.001 rad`、反馈过期或关节故障时直接拒绝。
运动实现复用已经核对过的小角度规划路线，每次目标最多前进 `0.004 rad`，请求速度
`0.05 rad/s`，不调用曾出现异常的 `move_head_joint_servo` 连续接口，也不会开机自动运行。

先运行默认只读预览；它会连接并读取 G2，但不会发送运动：

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
sudo ../../g2-dp-release/scripts/g2-network up

sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_head_center.sh
```

正常时应看到 `mode=read-and-preview`、`target_deg=+0.000`、
`pitch_commanded=False`、`roll_commanded=False`、`attempts=0`。这里的 `0°` 是 G2 当前
反馈坐标定义的零位，不证明相机机械安装已经校准。工具内部保留显式运动开关，但鉴于
此前实体头部异常，本阶段只记录只读命令；任何真实回正仍需操作者当次重新授权。

### PICO 连续头部跟随（独立入口；受限范围已冻结）

对应总架构图中的绿色头部软件路线和橙色 G2 本体限制：[查看总图](docs/architecture/teleoperation-status.svg)；[读图说明与源码对应](docs/architecture/README.md)。先由 G2 头部映射生成 JointPositionIntent，再交给 Router；不是把 HeadOrientationIntent 直接交给 Router。

2026-09-09：正式项目在原定 Python 3.10 环境中全量 410 项离线测试通过；替身 SDK 的真实 CLI、双轴目标发送、异步日志及退出已验证。操作者确认软件链能够驱动实体头部，并在 G2 本机操作中复现右转受阻，因此按当前安全范围冻结。

它把你的左右转头、抬头低头映射成 G2 的相对转角，不跟随歪头和身体平移。
路径：`头显姿态 → HeadFollowMapper → G2HeadJointMapper → JointPositionIntent → ControlRouter / DirectJointBackend → SafetyGateway → G2Adapter → 头部伺服`，**不需要 IK**。

冻结结论只覆盖软件安全范围，不等于 G2 完整双向机械行程通过。最近“每重启一次又左转一点”已由操作者确认来自固定 +5° 诊断的重复执行；右转受阻则能在 G2 本机操作中独立复现。原诊断和原始日志仍保留，正常跟随不调用固定 +5° 工具。

#### 速度和保护

- 正常入口的软件目标变化速率上限为 **0.3 rad/s（约 17°/秒）**，100 Hz 时每次最多变化 0.003 rad；旧核心类的 0.05 默认值保留，不改变其他模式。GDK 伺服 API 没有直接速度参数，因此这不是实体速度保证；反馈落后时会限速。
- 连续读取三次稳定、源时间戳推进的 G2 状态后准备就绪。第一帧新鲜 PICO 数据记住人和机器人的起点，不发送运动；之后按相对角度 1:1 跟随，无需按手柄。
- yaw 暂时固定在 G2 绝对 **[-8°, +45°]**：-8° 是依据本机重复故障证据设置的软件隔离边界，不是厂家机械限位。俯仰目标固定在启动角度 **±20°** 与厂家限位的交集内。请求越界时平滑限幅；启动反馈已低于 -8°则零发送拒绝，不自动回正。断流重新锚定也不扩大范围。
- 不跟随歪头。厂商接口要求三个角度，所以 idx12 始终发送本次启动的保持值，偏离保持值超过 0.005 rad 则停发。
- 保留 0.01 rad 目标领先/步长限制、200 ms G2 源时效和 300 ms XR 时效，以及关节错误、无进展、慢 SDK 调用检查。旧数据不补发；新鲜输入恢复后重新锚定，首帧不动。
- 使用普通 `move_head_joint_servo`，目标循环 100 Hz、控制周期参数 20 ms，不开启低延时模式。**循环频率和 20 ms 参数都不是物理急停保证。**

#### 怎么启动

先退出其他机器人控制程序。G2 上电并连接网线；PICO 用 USB 连接电脑、授权调试，打开 Galaxis 应用，选择 USB ADB，开启 **Head、Send Data**。入口会尝试恢复端口转发，但不能代替调试授权和发送开关。

以下命令只读计算，机器人不动：

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
sudo ../../g2-dp-release/scripts/g2-network up
bash apps/run_pico_g2_head_follow.sh --duration 60
```

真实运动必须由操作者当次确认、清空周围并准备好硬件急停后手动执行。戴稳头显并看向起点方向再启动，**这条会让实体头部跟随，不是预览**：

```bash
bash apps/run_pico_g2_head_follow.sh --enable-motion --duration 60
```

`motion_enabled=False` 表示只读；`anchored_no_motion` 表示记住起点；
`tracking` 表示持续计算；`limited` 表示目标碰到限制。只读时的 `validated` 不是运动次数。

Ctrl-C 或时间结束会停止后续发送，不自动回零。阻塞中的 SDK 调用和已接受运动不能靠 Python 撤销，异常转动请使用硬件急停。头显重置空间原点后应退出并重启，当前协议无法可靠自动识别该操作。

这是**独立头部入口**，与其他本项目会话互斥，不能另开双臂控制；需要组合时使用下方统一入口的 `--head-follow`，不要并排启动两套脚本，也不要直接运行内部 worker。暂无 VR 内头部开关。锁不能阻止其他电脑或厂商软件控制机器人。

#### 日志与故障反馈

每次运行自动独占创建 `artifacts/head-follow/<时间>-<唯一编号>.jsonl`，终端打印完整路径。记录输入序号/姿态、候选关节目标、原始反馈、发送开始/返回和耗时；不会覆盖旧日志。

控制线程只往有界队列放记录，后台约每 0.1 秒批量同步到磁盘，避免每条命令都等待磁盘导致过期。队列满、写盘错误或超过 0.5 秒没有持久化进展会停止后续发送。断电可能丢失尚未落盘的尾部记录，不能把“入队”说成“已经安全保存”。

旧固定 +5° 诊断日志在 `artifacts/head-servo/`，不要与正常跟随日志混淆。重复运行旧诊断会在当前角度上继续增加 +5°，它不是回正工具。本次未改动 Unity、单关节、双臂、夹爪、视频或原始实机记录。

实现位置：`apps/pico_g2_head_follow.py`（会话入口）、`src/teleop/control/head_follow_pipeline.py`（通用控制链）、`src/teleop/robots/g2_head_servo.py`（G2 专用反馈/发送边界）、`src/teleop/recording/head_journal.py`（后台日志）。验收记录见本阶段计划。

### 头部＋双臂＋夹爪＋视频统一入口

状态：统一软件链已完成，既有双臂、双夹爪和视频保持原真机冻结结论；头部按 yaw `[-8°, +45°]` 的受限范围冻结。完整右转范围因 G2 本机也会受阻，明确不在验收范围。

**准备**：按项目根 README 的连接教程完成网线连接、PICO USB 调试与 PC 输入服务启动；头显打开 Head、Controllers、Send Data。停止其他控制程序，戴稳头显、看向起点方向；每次真实运动仍须当次确认并准备好硬件急停，然后手动执行：

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation
sudo ../g2-dp-release/scripts/g2-network up
sudo -v
bash run_teleoperation.sh \
  --enable-motion \
  --mode dual-arm \
  --head-follow \
  --video \
  --duration 60
```

- 不写 `--head-follow`，或写 `--no-head-follow`：头部关闭，原双臂/夹爪行为保持。两个开关同时出现会拒绝；不允许与 joint-jog 组合。改变开关要退出再启动。
- 统一入口沿用 PICO 自动端口恢复和输入预检；视频沿用独立进程。看到头部 READY 与双臂 READY 后开始操作，首次新鲜头显输入只建立起点，不自动回正。
- **头部无需 Grip，随后自动跟随**。Grip 仍控制对应手臂，Trigger 仍控制对应夹爪；松 Grip 不会暂停头部。双 Grip 和双 Trigger 松开后长按 B 1 秒退出全部控制，Ctrl-C 或到时也结束整个会话。
- 双臂上限仍 `0.5 rad/s`；头部软件目标速率最高 `0.3 rad/s`；夹爪仍最高 `20 Hz`。组合当前要求 `100 Hz`，是目标循环频率，不保证 SDK 或电机实际达到。单关节参数不变。
- 头部保持普通伺服、yaw 暂时固定为 [-8°, +45°]、pitch 相对启动 ±20°并受关节限位约束、roll 保持启动值；反馈领先量、故障与时效检查保留。stale 帧跳过并重新锚定，不补发旧指令。
- 新增组合故障边界：头部反馈异常、共享 SDK 调用异常/超过100ms、组合周期超过20ms（两个目标周期）、日志失败、手臂硬跟踪故障或未知发送结果会结束整个控制会话，需操作者检查后重新启动。计算中途若最新输入变为 stale，也整体停发；周期开始时就已 stale 则跳过并自动重新锚定。**只在开启头部时适用，不改变原双臂模式的恢复规则。**已进入 SDK 的阻塞调用和已被机器人接受的运动无法靠 Python 撤销；实体异常使用硬件急停。

日志自动写到 `artifacts/head-follow/combined-<会话ID>.jsonl`，终端打印完整路径。记录头显起点、头部候选/实际发送、各路调用耗时、退出原因；磁盘异步写入，断电可能丢失尾部记录。视频仍用原诊断输出，不是统一训练数据录制。

头部启动或控制故障会在退出前记录 `head_fault`：当前原始全关节行、上一有效反馈及源时间戳；运行故障还包括最后 SDK 成功返回的头部目标（不代表实体到位）、当前输入，以及带独立帧号的最近候选映射。重新锚定不会覆盖最后发送目标。完整前后快照只在故障时序列化，不增加常规每帧日志的全关节负担。日志存储本身故障时仍会停发，但不保证能保存故障事件。[-8°, +45°] 只防止本软件再次进入已知故障区；若无本项目进程时机器人上电仍自转，必须停用头部运动并由厂家检查，不能称为软件已修复硬件根因。

原理：同一份 XRFrame 分给原双臂链和 HeadFollowSession；G2 内部两个 Adapter 各自保存跟踪状态，共用一次控制 GDK 初始化和 Robot 对象，串行调用 SDK。头部只写三个头关节，双臂只写十四个臂关节，夹爪走工具接口；视频另有只读 GDK 进程。代码见 `apps/pico_g2_dual_arm_live.py`、`src/teleop/robots/g2_head_lane.py`；记录见 [整合计划](plans/2026-09-08-head-unified-session.md)。

### 双臂迁移实现与离线测试

迁移保留了旧实现的左右 Grip 独立离合、以头部朝向为参考的坐标转换和左右手旋转补偿。普通 Grip 释放增加 `30 ms` 抗抖，避免传感器瞬时波动切断手臂；stale、停止和急停不等待抗抖。数值回归测试证明，相同输入下新旧 Retargeting 输出一致。IK 已使用真实 G2 URDF 完成离线双臂求解，并生成 14 个通过 `SafetyGateway` 的关节目标；该过程不导入 GDK，也不发送真机命令。

离线验收使用已有的 Pinocchio 环境：

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
PYTHONPATH=src /home/yuan/miniforge3/envs/g2-teleop-py310/bin/python \
  apps/demo_g2_dual_arm_ik_offline.py
```

成功标志是：

```text
G2_DUAL_ARM_IK_OFFLINE sides=left,right joint_targets=14 request_joints=14 safety_validated=True physical_robot_command_sent=False
```

实时只读入口现已把这些模块串起来：

```text
host：PICO → XRInputAdapter → XRFrame → 严格JSON/FIFO
                                               ↓
g2ns：G2只读状态 → FK锚点 → Retargeting → RobotAdapter.solve_ik()
                 → JointCommand → SafetyGateway → 终端预览（不发送）
```

先确保 `RoboticsServiceProcess` 正在运行，PICO 应用已开启 `Send Data`，并且 G2 网络已经执行过 `g2-network up`。然后运行：

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
bash apps/run_pico_g2_dual_arm_preview.sh --duration 30 --hz 10
```

入口会自动恢复 PICO 的 ADB reverse 端口。看到 `PICO_G2_DUAL_ARM_WORKER_READY` 后：按住左 Grip 并移动左手柄只预览左臂；按住右 Grip 并移动右手柄只预览右臂；同时按住两个 Grip 则预览 14 个关节。松开 Grip 会清除对应控制状态，`Ctrl-C` 会同时停止两个进程。预览行中的 `left_trigger/right_trigger` 可先确认两个 Trigger 数据正常；只读模式不会发送夹爪命令。

终端中的 `PICO_G2_DUAL_ARM_PREVIEW` 只表示“如果允许发送，本周期会生成这些安全候选值”。因为机器人实际没有运动，预览每帧都从最新 G2 遥测重新计算，避免内部 IK 假装机器人已经跟随。每条结果都必须显示：

```text
safety_validated=True physical_robot_command_sent=False
```

为避免 GDK 初始化期间积压的 XRFrame 被顺序处理，worker 使用单槽 `latest-only` 策略：新帧会覆盖尚未处理的旧帧，200 ms 时效限制保持不变。GDK 偶发一次没有关节状态时，只跳过当前帧并显示 `PICO_G2_DUAL_ARM_STATE_UNAVAILABLE`；状态恢复后显示 `PICO_G2_DUAL_ARM_STATE_RECOVERED` 并继续。若会话结束时状态仍未恢复，则退出码为失败，不能算作验收通过。

2026-09-03 已完成真实 PICO + G2 左臂、右臂和双臂只读联合验收，双臂 `0.5 rad/s` 真机路线随后也由操作者验收通过。旧版 Trigger 夹爪逻辑已迁入同一条新架构路线，并由操作者确认真机工作正常。两种控制模式现已合入总入口 `run_teleoperation.sh`。2026-09-04 针对连续控制、双臂同时操作和双夹爪操作完成改进；最终 PICO + G2 双臂、双夹爪与视频联合真机复验由操作者确认正常，本路线已重新冻结。

### 双臂真机入口（0.5 rad/s 上限）

同一个脚本提供两个互斥模式。先重复只读预览：

```bash
bash apps/run_pico_g2_dual_arm_live.sh \
  --dry-run \
  --duration 60 \
  --hz 10
```

真机模式必须在每次获得明确授权并准备硬件急停后才能运行：

```bash
bash apps/run_pico_g2_dual_arm_live.sh \
  --enable-motion \
  --duration 60
```

脚本会先自动恢复 PICO 的 `63901/63902` ADB reverse，再启动新架构链路。按住左 Grip 只控制左臂，按住右 Grip 只控制右臂，同时按住则控制 14 个手臂关节；松开某侧 Grip 后立即停止为该臂生成新目标。左/右 Trigger 分别控制左/右夹爪，即使对应 Grip 已松开也可以单独开合夹爪。双 Grip 都松开时长按 B 一秒可结束，终端 `Ctrl-C` 也会停止。

夹爪使用机器人无关的 `0=打开、1=闭合` 最新绝对目标。启动或某侧发送失败后，系统先读取 G2 真实夹爪位置；每侧只要求自己的 Trigger 松开归零，随后再次改变该 Trigger 才会发送，防止启动瞬间跳动且不影响另一侧。每侧最多 `20 Hz`，并检查取值范围和发送前 `100 ms` TTL；不再人为拆成十个 `0.10` 小步。只有到了 `G2Adapter` 才转换成 G2 omnipicker 的 `-0.785..0 rad`：单夹爪使用 `left_tool/right_tool`，同一帧中的双夹爪使用一次原子 `dual_tool` 请求，不再连续调用两次 GDK。`move_ee_pos()` 是离散位置命令，100 ms 只能阻止过期命令开始发送，不能取消已接受的夹爪运动。夹爪不是 22 个身体/头部/手臂位置关节之一，所以 22 关节列表保持不变。

每个控制周期开始时只取 mailbox 中最新的 XRFrame。双臂默认按 `100 Hz` 运行，G2 完整关节遥测与控制解耦并约按 `10 Hz` 刷新，Grip 新接合时强制读取一次真实状态；夹爪仍按每侧最高 `20 Hz` 调用。状态读取、FK/IK 和安全检查期间到达的新帧不会取消当前仍在 TTL 内的批次，下一周期自然使用最新帧。

当前按键没有冲突：Grip=手臂离合，Trigger=夹爪，B=安全退出。Grip 低于释放阈值持续 `30 ms` 才按普通松手处理；短促抖动不会断开控制。stale/过期输入帧仍立即禁止发送并释放双臂锚点，但不清空夹爪映射状态；新鲜帧回来后自动重新锚定并续控。连续 stale 只按事件限频显示，恢复时会输出本次持续帧数，避免终端刷屏拖慢进程。发送前安全拒绝和已知耦合约束也只丢弃当前周期，不要求人为反复松手。某侧夹爪 GDK 结果未知时，只暂停该夹爪并要求松开对应 Trigger、重新读取反馈；双臂和另一夹爪继续。双夹爪原子请求若结果未知，则两侧都必须重新读取反馈。手臂 GDK 结果未知、严重跟踪异常和急停仍是全局故障，必须全部归零后恢复。旧版录制按键尚未迁移；以后应由独立 Recorder 使用 Y 等明确配置的边沿事件，不能复用上述控制按键。

`0.5 rad/s` 是手臂关节速度上限，不是每帧移动 `0.5 rad`。默认 `100 Hz` 下每周期限制为 `0.005 rad`。命令发送前依次检查 XR 数据时效、末端工作空间、IK、关节位置/步长、已知耦合约束和 `100 ms` TTL。连续伺服使用上一条已接受目标作为下一步参考。末端位置超出安全盒时，各坐标会饱和到最近边界，边界本身不放宽，因此手臂保持连续但不能继续向盒外移动。一次 G2 遥测读取失败后，在下一个 `10 Hz` 遥测周期重试，期间不向机器人发送新命令，也不会以 `100 Hz` 猛重试 SDK。真实关节反馈落后目标超过 `0.10 rad` 但小于 `0.20 rad` 时，只跳过当前周期并从真实状态自动重新锚定；达到 `0.20 rad`、手臂物理发送结果未知或状态不完整时才全局锁停并要求全部归零。正常成功和同类边界日志最多每秒显示一次，结束摘要会列出边界饱和、自动恢复、硬故障以及各阶段最大耗时。

当前 G2 保守末端工作空间（`base_link`）为：左右手 `x=0.10..0.85 m`、`z=0.35..1.35 m`；左手 `y=0.05..0.75 m`、右手 `y=-0.75..-0.05 m`。这可以阻止越界和跨过身体中线，但不等于完整自碰撞模型，因此首次真机验收仍必须清空周围并准备硬件急停。

## Run the supervised PICO + G2 unified session

The `SessionSupervisor` starts the verified video path and one selected control path as
independent children. It does not put video in `XRFrame` or bypass the existing
`ControlRouter`, `SafetyGateway`, or `G2Adapter` boundaries.

The launcher first restores PICO ADB reverse ports and performs a read-only XRFrame
check. If the PICO app is not sending fresh data, it exits before G2 setup and prints a
direct instruction to enable `Send Data`. It then prepares the isolated G2 network,
starts the video path, starts the selected `joint-jog` or `dual-arm` control path, and
stops the complete session when either critical child ends. A lock rejects a second
concurrent session.

Physical control is refused unless the command contains the explicit
`--enable-motion` acknowledgement:

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime

bash apps/run_pico_g2_teleop.sh \
  --enable-motion \
  --mode joint-jog \
  --video \
  --duration 300 \
  --initial-joint idx21_arm_l_joint1
```

双臂、夹爪和视频使用同一个长时间运行入口（`86400` 秒，即 24 小时；通常使用 `Ctrl-C` 主动结束）：

```bash
bash apps/run_pico_g2_teleop.sh \
  --enable-motion \
  --mode dual-arm \
  --video \
  --duration 86400 \
  --fps 60
```

为兼容原 V1，省略 `--mode` 时仍选择 `joint-jog`，省略 `--video` 时视频仍默认开启。单关节默认 `50 Hz`，双臂默认 `100 Hz`；显式传入 `--hz` 可以覆盖所选模式的默认值。模式在进程启动时选择，运行中不热切换；切换模式时先结束当前会话，再用另一个 `--mode` 重新启动。

排查控制卡顿时，可以临时加入 `--no-video`，只运行控制链做 A/B 对照：

```bash
bash apps/run_pico_g2_teleop.sh \
  --enable-motion \
  --mode dual-arm \
  --no-video \
  --duration 60 \
  --hz 100
```

`--no-video` 不是正式的新模式；正常联合运行仍不写该参数，让视频默认开启。

`--enable-motion` 只允许所选路线发送命令，本身不会让机器人运动。`joint-jog`
模式仍需按住右 Grip 并推动右摇杆；松开 Grip 或摇杆回中就停止产生新命令，
手臂上限保持 `0.2 rad/s`，身体和头部保持 `0.1 rad/s`。

`dual-arm` 模式中，左右 Grip 分别控制左右臂，左右 Trigger 分别控制左右夹爪；
双臂关节速度上限保持 `0.5 rad/s`。松开对应 Grip 会停止该臂的新目标，启动或
全局故障恢复时必须先松开两个 Grip 和 Trigger；单侧夹爪故障只需松开对应 Trigger。两种模式都可以在安全状态下长按 B
一秒结束，也可以按 `Ctrl-C` 统一停止控制和视频。

The supervisor implementation and child cleanup are covered by focused offline
tests. The operator completed the real PICO + G2 acceptance for both the
direct-joint combination and the dual-arm + dual-gripper + video combination. The
dual-arm continuity defect reported on 2026-09-04 was fixed and the final live
continuity re-test passed. Switching mode still requires stopping and restarting
the session.

会话锁只由最外层 `SessionSupervisor` 持有；启动前的 PICO/ADB 连接、PICO watcher、
视频和控制子进程都不会继承该锁。因此即使最外层终端异常结束、ADB server 或某个 watcher 暂时残留，重新启动也不会
被错误报告为 `another PICO + G2 session is already running`。正常停止仍应优先使用
`Ctrl-C`，以便同时清理所有子进程。

如果使用修复前的入口曾让 ADB 残留持锁，可执行下面三行恢复；这只会短暂重连
PICO，不会启动 G2 运动：

```bash
adb kill-server
adb start-server
bash apps/auto_connect_pico.sh --once
```

## Run the offline demo

```bash
PYTHONPATH=src python apps/demo_direct_joint.py
```

## Preview a G2 single-joint request

```bash
PYTHONPATH=src python apps/demo_g2_direct_joint_dry_run.py
```

The G2 demo starts from an injected offline state, applies a `+0.01 rad` jog to
`idx21_arm_l_joint1`, and prints a `JointControlReq`-shaped request preview. This demo
and the default G2Adapter mode never enable physical command output.

## Connect the real G2 network without disconnecting the host VPN

The host Cisco VPN and the wired G2 network both claim the `10.42.1.0/24`
address range. Do not work around this conflict with ad-hoc host route changes.
Work from this `Teleoperation/teleop-runtime` directory and call the shared G2
network tool from its sibling project:

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
sudo ../../g2-dp-release/scripts/g2-network up
```

The network namespace is a machine-level resource, so the absolute command works
from any directory. Using the runtime directory as the normal working directory
keeps the later teleoperation commands and their relative `src`/`apps` paths clear.

The connection is ready only when the command prints:

```text
SUCCESS: G2 IP networking works inside g2ns while the host VPN remains untouched.
```

`g2-network up` creates a Linux network namespace named `g2ns` and a macvlan
interface named `g2mac`. The namespace uses `10.42.1.103/24` for G2 traffic,
routes the G2 internal `10.42.0.0/24` network through `10.42.1.101`, and tests
ARP, ping, discovery port `2379`, and service port `8849`. Its routing table is
separate from the host routing table, so Cisco VPN continues to serve the host
while G2 traffic leaves through the wired `eno1` interface.

Programs that use GDK must also run inside this namespace; running them directly
in the normal host terminal can send their G2 traffic back into the VPN. Use the
following general form:

```bash
sudo --preserve-env=GDK_APP,PYTHONPATH,LD_LIBRARY_PATH,AORTA_DISPATCHER_THREAD_NUM \
  ../../g2-dp-release/scripts/g2-network \
  run -- /absolute/path/to/python /absolute/path/to/program.py
```

Creating the namespace only establishes IP connectivity. It does not import GDK,
send a joint command, or authorize physical motion. G2Adapter remains read-only unless
the separate live entry point explicitly injects a command sender and enables it.

## Read one real G2 joint-state snapshot

After `g2-network up` reports success, stay in the `teleop-runtime` directory and
run the read-only app inside `g2ns`:

```bash
sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_readonly.sh --settle-seconds 2
```

The wrapper loads the installed GDK environment inside the isolated namespace,
initializes GDK, calls `get_joint_states()` once, converts the 22 configured named
joints into one `RobotState`, and releases GDK. It never calls a GDK write method.
Success prints one `G2_READONLY_STATE joints=22` line ending with:

```text
physical_robot_command_sent=False
```

This read-only app constructs G2Adapter without a command sender, so
`send_joint_command()` remains disabled; connecting and reading alone never enables
physical control.

## Stream the G2 head camera to the PC VideoHub (read-only)

The video path is deliberately separate from XR input and robot control:

```text
g2ns: G2 kHeadColor -> validated 640x400 JPEG
                              |
                              v
host: Unix socket -> VideoHub latest frame -> independent TCP viewers on selected_host:63902
```

It does not put images in `XRFrame`, use TCP `63901`, construct a `JointCommand`, or
call a GDK robot-write method. The GDK worker skips repeated camera timestamps, rejects
an unexpected head-camera resolution, and always releases GDK. The host keeps only one
newest frame. Every viewer has an independent sender and timeout, so a slow or absent
viewer drops only its own old frames and cannot block other viewers or create an
application-level video backlog. `VideoBridge` remains only as a compatibility alias
for older imports; new code uses the clearer `VideoHub` name.

After `g2-network up` has succeeded, start a five-minute read-only camera session from
the runtime directory:

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime

bash apps/run_g2_video_bridge.sh \
  --duration 300 \
  --fps 15 \
  --host 127.0.0.1 \
  --port 63902
```

该入口默认同时运行 PICO ADB watcher：它只选择已授权且型号为 `A9210/product:sparrow` 的 PICO，自动建立并在 USB/ADB 恢复后重建 `63901`、`63902` reverse。无需再手工重复输入端口命令；如只需要 PC 预览，可增加 `--no-auto-pico` 禁用。

`15 FPS` is the conservative preview default, not the camera maximum. The verified
full-rate G2 profile uses a 60 Hz read-polling ceiling to capture and deduplicate the
approximately 30 FPS source:

```bash
sudo -v
bash apps/run_g2_video_bridge.sh \
  --duration 30 \
  --fps 60 \
  --host 127.0.0.1 \
  --port 63902
```

The worker prints two separate facts. `G2_CAMERA_CAPABILITY` is what the GDK API
declares for `kHeadColor`; `G2_CAMERA_WORKER_SUMMARY` reports the measured unique
source FPS, PC delivery FPS, skipped duplicate timestamps, average JPEG bytes, and
minimum/maximum source-frame interval. A requested value such as `--fps 30` is only
a read-polling ceiling and must never be reported as the measured camera or output
result. Polling above the declared source rate is valid for diagnosis because repeated
source timestamps are discarded rather than transmitted. This acceptance remains
camera-read-only and every result includes `physical_robot_command_sent=False`.
The physical-G2 acceptance measured `640x400` JPEG, GDK-declared `30.009 FPS`,
measured source `29.967 FPS`, and PC delivery `30.033 FPS` over 30 seconds. The
60 Hz value is not transmitted video FPS; duplicate source timestamps are discarded,
leaving approximately 30 unique frames per second.

The launcher asks for `sudo` once, runs only the camera worker inside `g2ns`, and by
default keeps the TCP bridge on normal host loopback. For Wi-Fi, pass the PC's exact
LAN IPv4 address to `--host`; do not expose all interfaces with `0.0.0.0` unless that is
an explicit deployment decision. Success first prints `VIDEO_BRIDGE_READY`, then
`G2_CAMERA_WORKER_PRECHECK`. Every camera-worker and bridge result includes:

```text
physical_robot_command_sent=False
```

To validate the PC output independently from the headset, use a second terminal:

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
PYTHONPATH=src python apps/observe_video_stream.py \
  --host 127.0.0.1 --port 63902 --frames 30
```

Each accepted frame prints its sequence, `kHeadColor`, `640x400`, JPEG byte count, and
PC-side age. `Ctrl-C` in the first terminal stops the worker, releases GDK, stops the
bridge, and removes only that launcher's temporary Unix socket.

To see the same frames in a live PC window, keep the bridge running and run this from
a second terminal after the text observer has exited:

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
PYTHONPATH=src python apps/preview_video_stream.py \
  --host 127.0.0.1 --port 63902
```

The app opens a local-only browser page such as `http://127.0.0.1:54321/`; the exact
free port is printed after `VIDEO_PREVIEW_READY`. If the browser does not open
automatically, copy that printed URL into the PC browser. Press `Ctrl-C` in the
preview terminal to stop it. It does not save an image or video, change Unity, or send
a robot command. `VideoHub` now supports simultaneous independent viewers, so the PC
preview, a text observer, the PICO client, and a future platform consumer can
connect at the same time. A slow client is disconnected after its send timeout without
stopping the other clients.

Protocol version 1 is network-byte-order header `!4sBBHQQQIII`: magic `GVID`, version,
JPEG encoding code, UTF-8 camera-name length, sequence, source timestamp, PC receive
timestamp, width, height, and JPEG length; the header is followed by the camera name
and one complete JPEG. The PICO `RobotVideoClient` connects to this dedicated `63902`
stream, receives on a background thread, and lets the Unity main thread display only
the newest complete frame. TCP `63901` remains unchanged.

For the current highest verified G2 profile over USB ADB, first restore both reverse
rules, then keep this command running:

```bash
sudo -v
bash apps/run_g2_video_bridge.sh \
  --duration 600 \
  --fps 60 \
  --host 127.0.0.1 \
  --port 63902
```

In the PICO app, open `视觉与录制` and click `打开视角`. The expected status changes
from `连接中` to `已连接` and then shows `640×400` plus the measured display FPS.

## Preview one joint command from the real G2 state

This stage reads the connected robot and then releases GDK before it builds a
single-joint request preview through the existing robot-independent path:

```text
real G2 RobotState -> JointJogIntent -> ControlRouter -> DirectJointBackend
                   -> SafetyGateway -> G2 JointControlReq preview only
```

From the `teleop-runtime` directory, run:

```bash
sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_joint_preview.sh \
  --joint idx21_arm_l_joint1 --delta-rad 0.01 --settle-seconds 2
```

The delta must be non-zero and no larger than `0.01 rad` in magnitude. Unknown
joints, stale state, position-limit violations, and invalid values are rejected.
The program prints the real current angle, calculated target, and the
`JointControlReq`-shaped fields. It does not call `joint_control_request()` or any
other GDK write method; every outcome reports:

```text
physical_robot_command_sent=False
```

## Observe PICO input as XRFrame

The input layer wraps the existing `xrobotoolkit_sdk` stream and emits immutable,
robot-independent `XRFrame` snapshots:

```text
PICO / Galaxis -> RoboticsService -> XRInputAdapter -> XRFrame
```

This observer does not import a robot SDK, solve IK, create a `TeleopIntent`, or send a
hardware command:

```bash
PYTHONPATH=src /home/yuan/miniforge3/envs/xrobotoolkit-pybind/bin/python \
  apps/observe_xr.py --duration 10
```

For controller-mode validation, keep Controllers enabled and temporarily disable Hand
Tracking in the headset application. Move both joysticks and press the trigger/grip and
A/X buttons during the observation. The final summary always reports
`hardware_command_sent=False`.

## Preview PICO single-joint intents

The next read-only stage maps deliberate controller gestures to a robot-independent
`JointJogIntent` without IK or robot commands:

```text
XRFrame -> JointJogMapper -> JointJogIntent -> terminal only
```

The app defaults to `observe`, where X/A are not consumed. Before enabling joint-jog,
stop the legacy controller-button recorder and turn off `Switch with A` in the headset;
then run:

```bash
PYTHONPATH=src /home/yuan/miniforge3/envs/xrobotoolkit-pybind/bin/python \
  apps/observe_joint_jog.py --mode joint-jog --duration 20
```

In `joint-jog` mode, X selects the previous joint, A selects the next joint, and a
right-stick up/down deflection while the right Grip is held emits one `+0.01/-0.01 rad`
intent. Return the stick to neutral before the next step. Releasing Grip, stale input,
leaving the mode, or pressing X+A together emits no intent. The app uses the static G2
dry-run schema only as a realistic 22-joint menu; it does not read robot state, import
GDK, create a `JointCommand`, or send hardware commands.

## Run the complete PICO-to-FakeRobot path

The next safe stage connects emitted joint-jog intents to the existing control core:

```text
PICO -> XRFrame -> JointJogIntent -> ControlRouter -> DirectJointBackend
     -> SafetyGateway -> FakeRobot
```

FakeRobot updates in-memory joint angles only. The app never imports a vendor SDK or
sends a physical robot command. Joint-jog mode must be enabled explicitly:

```bash
PYTHONPATH=src /home/yuan/miniforge3/envs/xrobotoolkit-pybind/bin/python \
  apps/run_pico_fake_robot.py --mode joint-jog --duration 20
```

Accepted steps print the selected joint's simulated before, target, and after angles.
Safety rejections leave the simulated state unchanged. Every command result and final
summary reports `physical_robot_command_sent=False`.

## Preview PICO single-joint input against the real G2 state

The XR SDK connects to `RoboticsServiceProcess` on the host loopback interface, while
GDK must run inside `g2ns`. The launcher therefore keeps two supervised PC processes
and connects them with a local intent-data FIFO:

```text
host: PICO -> XRFrame -> JointJogIntent -> JSON line
                                             |
                                             v
g2ns: G2 read_state -> ControlRouter -> DirectJointBackend -> SafetyGateway
                    -> JointControlReq preview only
```

### Restore the PICO USB connection

PICO input has two separate requirements: a PC receiver must listen for tracking data,
and ADB must forward the headset port to that receiver. USB detection or an ADB reverse
rule alone does not mean that the application data connection is ready.

After a PC restart, start exactly one XR tracking receiver with its installed launcher:

```bash
bash /opt/apps/roboticsservice/runService.sh
```

The service listens on TCP `63901` and exposes the received data to
`xrobotoolkit_sdk` on local port `60061`. It receives XR data only; starting it does not
connect to G2 or send a robot command.

ADB reverse rules can disappear after the PICO reboots, the USB cable reconnects, or
the ADB server restarts. The preferred recovery command restores both current ports:

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime
bash apps/auto_connect_pico.sh --once
```

The connector restores ADB rules only. It does not start `RoboticsServiceProcess`.
To restore and verify the rules manually, run:

```bash
adb -s PA921AMGL1300038G reverse tcp:63901 tcp:63901
adb -s PA921AMGL1300038G reverse tcp:63902 tcp:63902
adb -s PA921AMGL1300038G reverse --list
```

The verification output must contain:

```text
UsbFfs tcp:63901 tcp:63901
UsbFfs tcp:63902 tcp:63902
```

Before starting, confirm that the PICO App shows `WORK` and `Send Data` is enabled,
stop the legacy X/A recorder, turn off `Switch with A` in the headset, and make sure
`g2-network up` has already succeeded. Then run from `teleop-runtime`:

```bash
bash apps/run_pico_g2_preview.sh \
  --mode joint-jog --duration 30 --hz 50
```

The script asks for `sudo` once in the current terminal, then reuses that terminal's
authorization to enter the already configured `g2ns`; it does not run `g2-network up`,
install packages, or modify routes. Wait for
`PICO_G2_WORKER_READY`, return X/A, right Grip, and the right stick to neutral, then:

- Tap and release X/A to select the previous/next joint.
- Hold right Grip.
- Move the right stick up for `+0.01 rad` or down for `-0.01 rad`.
- Return the stick to neutral before another preview.

The worker rejects malformed, replayed, future, or older-than-`200 ms` intent messages.
It reads the selected joint's real G2 angle and prints `PICO_G2_PREVIEW`, but contains no
GDK write call. Every output path reports:

```text
physical_robot_command_sent=False
```

Native XR SDK diagnostics stay on the terminal; only versioned JSON intents are sent
through the launcher's dedicated file descriptor 3. This prevents SDK messages such as
`initialize sdk` from being misread by the G2 worker as control data.

## Control real G2 joints from PICO

This is the live direct-joint path. It reuses the same PICO input and safety checks as
the preview, but the G2Adapter translates each accepted `JointCommand` into the vendor
GDK `JointControlReq`. No IK is called.

From `teleop-runtime`, start a five-minute session with the left arm's first joint
selected initially:

```bash
cd /home/yuan/Work/Project/G2-VLA-Workspace/projects/Teleoperation/teleop-runtime

bash apps/run_pico_g2_live.sh \
  --mode joint-jog \
  --initial-joint idx21_arm_l_joint1 \
  --duration 300 \
  --hz 50
```

Enter the `sudo` password when prompted. Wait until both
`PICO_G2_LIVE_READY` and incoming PICO diagnostics appear. Start with X, A, Grip, and
the right stick released and centered. Do not run the legacy recording/collection
process at the same time: its B button owns stop-and-save, while joint-jog mode owns
the long-B exit gesture. Then use:

- X: select the previous joint.
- A: select the next joint.
- Joint selection is accepted only after Grip is released and the right stick is
  centered; X/A is ignored while a joint is moving.
- Hold right Grip and keep the right stick up/down: continuously move the selected
  joint in the positive/negative direction at a `10 Hz` command rate.
- Release Grip or center the stick: stop producing new commands. Input older than
  `200 ms` is also ignored by the worker watchdog.
- With X/A released, Grip released, and the stick centered, hold B for one second:
  exit joint-jog from the controller and release GDK. `Ctrl-C` remains the terminal
  stop path.

The terminal prints `PICO_SELECTED joint=...` whenever X/A changes the selection, and
prints `PICO_G2_LIVE_SENT joint=... current=... target=...` after a command reaches GDK.
PICO samples at `50 Hz`, but a single-slot mailbox overwrites old states and G2 consumes
only the newest state at `10 Hz`. Arms use at most `0.02 rad` and `0.2 rad/s` per cycle;
body and head joints remain capped at `0.01 rad` and `0.1 rad/s`. Every request keeps a
`100 ms` TTL, G2 position limits, message freshness checks, and a fresh state read before
sending. The worker checks the latest PICO state again after that robot-state read, so a
release that arrives during the read cancels the pending send. A successful controller
exit prints `PICO_G2_LIVE_EXIT`.

The live G2Adapter also checks the coupled-joint rule confirmed by the 2026-08-31 GDK
live rejection:

```text
-idx26_arm_l_joint6 + idx27_arm_l_joint7 <= 1.92
```

The PC uses a `0.001 rad` guard margin, so its software threshold is `1.919`. A target
above that threshold is not sent to GDK. The terminal prints one structured warning:

```text
PICO_G2_COUPLED_LIMIT_REJECTED ... action=command_not_sent session_continues=True
```

Release Grip or center the right stick once to rearm, then move in the opposite, safe
direction. The session remains active. This non-fatal handling applies only to a known
pre-send coupled-limit rejection; an unclassified GDK, communication, or hardware error
still stops the session and releases GDK. The installed public GDK configuration does
not expose a complete coupled-constraint table, so no unverified right-wrist rule is
assumed here.

## Accept the 22-joint adapter contract without motion

The adapter contract exposes an ordered joint menu, position/step/velocity limits,
body groups, independent-command flags, and runtime capabilities. The G2 contract
contains the 22 GDK position joints used by the direct-joint path. Run this read-only
check after `g2-network up` succeeds:

```bash
sudo ../../g2-dp-release/scripts/g2-network run -- \
  bash apps/run_g2_joint_acceptance.sh --settle-seconds 2
```

The command reads one real state snapshot and prints one `G2_JOINT_ACCEPTANCE` row per
configured joint. It checks presence, finite values, and configured position limits;
it does not construct a command sender or call a GDK write method. Success ends with:

```text
G2_JOINT_ACCEPTANCE_SUMMARY passed=True joints=22 physical_robot_command_sent=False
```

This is a contract/state acceptance, not an automatic movement sweep. Automatic motion
across all joints is intentionally excluded until a complete collision/workspace model
exists. The frozen milestone and its exact evidence boundary are recorded in
`docs/milestones/g2-direct-joint-v1.md`.

The stable `RobotAdapter` discovery surface is:

```text
get_capabilities()      available control families
get_joint_schema()      ordered metadata owned by this adapter
list_joints()           commandable controller menu
get_joint_info(name)    group and position/step/velocity limits
read_state()            latest named positions
send_joint_command()    validated named-joint command
solve_ik()              unified entry; calls the IKSolver injected at construction time
```
