# 修正跨机器人遥操作架构状态图

Status: frozen（与 2026-09-03 V1 真机验收状态一致）

## What

- Problem / goal: 当前 `teleoperation-status` 图漏画控制模式来源，把用户选择模式与 `ControlRouter` 按 Intent 类型分发混在一起，并且没有准确表达 `RobotAdapter.solve_ik()` 在安全检查前参与求解的真实调用关系。修正正式架构图，使其可作为当前实现与后续目标的可信总览。
- User-visible behavior: 阅读者能沿箭头从上到下区分输入事实、控制模式、映射策略、Intent、路由、命令、安全检查、机器人适配和独立视频链路；图中完成度与代码和验收记录一致。
- Success criteria:
  - 显式画出由运行入口/启动参数提供的控制模式选择，并标明当前 V1 固定单关节、双臂为独立只读预览入口。
  - 通过当前两种入口和 Router 后的两条路线区分 `JointJogMapper` 与 `DualArmRetargeter`；未实现的全身和行走只在未来框列名，不再把双臂末端等同于全身遥操作。
  - `ControlRouter` 标为按 Intent 类型自动分发；图不声称当前 Router 已主动执行机器人能力协商。
  - 直接关节和末端 IK 路线都产生 `JointCommand`，随后经过 `SafetyGateway`；只有单关节真机入口调用 `RobotAdapter.send_joint_command()`，双臂只读入口在验证后停止。
  - 图明确显示末端路线会在安全检查前调用 adapter-owned `solve_ik()`，且能力不足时拒绝。
  - 视频链路继续与 XR、Retargeting 和 SafetyGateway 分离；`SessionSupervisor` 只管理生命周期。
  - 蓝/紫、绿色、橙色、灰色分别表示当前公共步骤、实机端到端已验收、离线/只读已验证、尚未实现，不出现图例未定义的状态颜色。
  - Graphviz 源文件能无警告生成 SVG 和 PNG；渲染图文字可读且没有明显重叠或裁切。
  - 全部离线测试通过，证明文档变更没有意外影响运行代码。
  - 根据用户对上一版的反馈，正式图改为新手版：主阅读方向唯一，核心节点不超过约 16 个，中文解释优先、英文标识作为补充。
  - 控制主线、视频主线和“总管家/未来功能”分区清晰；未来功能折叠为一个灰色框，不使用跨区反馈线干扰主流程。
  - 最终 PNG 经过实际打开检查，节点、文字、连线和分区标题没有遮挡或裁切；读者可以在约 10 秒内找到模式选择与 `ControlRouter` 的职责边界。
- Out of scope: 修改遥操作运行逻辑、实现统一运行时模式切换、实现全身/行走、连接 PICO/G2、发送任何机器人命令、修改桌面旧版附件。

## How

- Approach: 以当前 `teleop-runtime/src` 的可执行调用关系为事实来源，以测试和里程碑记录决定完成度；XRoboToolkit 论文与旧 `test-teleop` 只作为设计来源和回归基线。重构现有 Graphviz 状态图，保留一张图，但清楚区分当前实线和未来灰色虚线。
- Components / files touched: 本计划、`docs/architecture/teleoperation-status.dot` 及其生成的 `.svg`、`.png`；只有在核验发现文字说明冲突时才同步更新直接引用该图的 README。
- Phases: 证据矩阵核对；修改 DOT；生成并视觉检查 SVG/PNG；运行离线测试；根据实际结果更新本计划状态。
- Risks / unknowns: 图过宽或节点过多会降低可读性；`teleop-runtime` 当前不是独立 Git 仓库，无法提供该目录内的提交历史；测试不能证明未实现的真机双臂、全身或行走安全性。
- Safety boundary: 只编辑本地文档与生成图。不得启动 ADB、XR 服务、GDK、网络命名空间、摄像头或机器人运动入口。
- Verification plan: 使用 `dot` 生成 SVG/PNG并检查退出状态；解析 SVG 标题和关键标签；打开 PNG 进行视觉检查；运行 `PYTHONPATH=src python -m unittest discover -s tests -v`；对照本页每条成功标准审查最终 DOT 和渲染结果。

## User-driven revision

- 上一版虽然补全了架构事实，但节点和依赖线过多，且渲染后存在遮挡，不适合作为新手的第一张图。
- 本轮不改变已经核实的架构结论，只改变信息层级：第一张图只解释“数据怎样一步步走”；RobotState、FK、Schema 等依赖写进所属节点，不再画跨区回线。
- 详细类名和未来后端不再逐个展开；需要深入时再配第二层专题图，而不是塞进这张总览。

## Verification result

以下内容记录 2026-09-02 当时的历史状态；当前权威状态以文末“2026-09-03 V1 冻结修订”为准。

- 根据用户反馈完成新手版重排：核心图为 16 个功能节点，控制主线只有一个自上而下的阅读方向；关节直控和双臂 IK 在 Router 后并排分支，视频与未来功能不再穿插控制主线。
- Graphviz 2.43.0 无警告生成正式 SVG 和 1261×2017 PNG。正式文件与实际打开检查的临时渲染逐字节一致；视觉检查确认标题、节点、箭头、分区边框和图例没有遮挡或裁切。
- 关键文字检查确认图中同时包含 `XRInputAdapter → XRFrame → Mode/Retargeting → Intent → ControlRouter`，并明确写出“Router 不选择模式”、双臂 `solve_ik()`、统一关节安全检查、双臂只读不发送和独立视频链路。
- 本轮运行 16 个最相关的离线测试，覆盖 IK 路由、单关节完整流水线、双臂只读预览和 SessionSupervisor，结果为 `Ran 16 tests ... OK`。本轮只修改 DOT、SVG、PNG 和本文档，未改运行代码；此前同一架构修正的完整套件仍为 `Ran 196 tests ... OK (skipped=5)`，项目指定 G2 IK 环境补跑项为 `Ran 5 tests ... OK`。
- 最终自审未发现架构事实回退：模式仍在 Router 前决定；Router 只按 Intent 类型分路；控制和视频只由 SessionSupervisor 共享生命周期。这里的旧版“双臂仅预览”描述已被 2026-09-03 真机验收结果取代。
- 挑刺复核修正了五处不一致：区分 XRoboToolkit 传输层与 `XRoboToolkitInputAdapter` 组装职责；将错误类名 `G2CameraSource` 改为实际的 `G2VideoSource`；统一视频状态色与图例；限定 V1 `SessionSupervisor` 只管理单关节真机控制和视频；修正文档遗留的横向阅读与双臂发送表述。
- 修正后运行 92 个与本图直接相关的离线测试：XR、映射/重定向、Router/IK、安全、G2 Adapter、双臂只读、视频源/协议和 SessionSupervisor。91 个在沙箱内通过；唯一的本机 `socketpair` 测试因沙箱权限报错，允许本机 socket 后单独重跑通过。三个相关 Bash 启动脚本通过 `bash -n`。
- 限制：本次没有连接 PICO、GDK 或真实 G2，也没有发送机器人命令；`teleop-runtime` 不是独立 Git 仓库，因此无法提供该目录的独立 Git diff。

## 2026-09-03 V1 冻结修订（当前权威状态）

- 图按五个区域表达：输入与模式、控制路线、RobotAdapter、独立视频、生命周期与辅助系统；模式选择仍在 Router 之前，Router 只按 Intent 类型分发。
- 已验收绿色路线包括：22 关节单关节控制、双臂末端 URDF IK 真机控制、双夹爪控制、G2 视频向 PICO/PC 并行显示，以及统一 SessionSupervisor 启停。
- 当时频率记录为：PICO 实测 Tracking Hz 约 `90 Hz`；PC 每 `50 Hz` 读取最新 XRFrame；单关节控制最高 `10 Hz`；双臂控制 `50 Hz`、最高 `0.5 rad/s`；夹爪命令最高 `20 Hz`；视频约 `30 FPS`、PC 轮询上限 `60 Hz`。2026-09-04 最终冻结时，双臂控制默认更新为 `100 Hz`，PICO 实测 Tracking Hz 记为约 `90–100 Hz`；当前值以总 README 和 `teleoperation-status` 为准。
- `RobotAdapter` 统一 `read_state`、关节 schema/能力、`solve_ik`、关节/夹爪发送和停止；当前 G2 使用 Adapter 内部 URDF IK，未来厂商可覆盖 `solve_ik()`。
- 全身重定向、行走、乐聚 Adapter、集合平台 VideoClient、PC 统一 Recorder/Monitor 仍保持灰色，图未把计划写成已完成。
- 主图不再展示 `dry-run`；它仍是源码中保留的安全测试工具，不属于正常真机主流程。
- Graphviz 正式源图、SVG 和 PNG 已同步；冻结前完整 Python 回归结果为 `249` 项通过、`5` 项环境跳过、`0` 项失败。

## 2026-09-04 最终冻结后的措辞复核

- 标题改为“可扩展机器人遥操作目标架构 · PICO + G2 V1 冻结状态”，避免让读者误以为乐聚等第二种真机已经完成。
- 状态颜色重新收紧：绿色只表示 V1 已实现并完成相应验收；橙色只表示通用接口已存在、但尚待第二种真实机器人验证；灰色表示未实现；白色虚线仅表示旧代码参考，不参与完成度判断。
- 明确区分 PICO Tracking Hz、PC 控制 Hz、G2 遥测 Hz 和视频 FPS；单关节写明“50 Hz 取最新输入、最高 10 Hz 发送 G2 指令”，双臂写明“100 Hz 控制、约 10 Hz 遥测”。
- `solve_ik()` 改为“Adapter 选择并持有 IKSolver”，当前 G2 明确写为 `G2URDFIKSolver = Pinocchio 运动学计算 + G2 URDF 模型`，不再暗示 URDF 本身是算法或 Adapter 独自实现 Pinocchio。
- 双臂关节与夹爪明确为两种独立命令，并分别经过 `SafetyGateway` 与 `GripperSafetyGateway`；最终都通过统一 RobotAdapter 边界发送。
- JointJogMapper 明确为 X/A 选关节、右 Grip + 右摇杆产生增量；DualArmRetargeter 明确 Grip 建立锚点、手柄相对位移映射末端、Trigger 控制夹爪。
- Graphviz 2.43.0 无警告重新生成 SVG 与 PNG；最终 PNG 为 `3317×2604`，实际打开检查未发现文字裁切、节点重叠或状态颜色歧义。
- 本轮只改图和说明，没有改运行代码；冻结前同一轮 G2/Pinocchio 专用环境 `273/273` 测试通过，最终真机验收结论不变。

## 2026-09-04 IK 选择路线显式化

- 将原先一句“厂商 IK 可替换”展开为可见分支：`RobotAdapter.solve_ik()` 是统一入口，本身不是算法；会话启动时选择并固定一个 `IKSolver`。
- 厂商官方 IK 已完成接入、验证并在配置中启用时，目标路线为 `VendorIKSolver`；否则默认路线为 `URDFIKSolver`。当前 G2 明确走 `G2URDFIKSolver = Pinocchio + G2 URDF`，并已完成真机验收。
- `VendorIKSolver` 保持灰色，表示尚未接入真实厂商 IK；跨厂商选择策略保持橙色，表示统一边界已有、仍待第二种真机验证。
- 明确禁止会话运行中因求解失败而静默切换 IK 实现；失败应拒绝当前命令并按安全策略处理，避免两种求解器输出差异造成关节目标跳变。
- 简化版 Mermaid 同时拆开双臂末端和夹爪：末端位姿进入 IK，夹爪始终绕过 IK 并进入自己的安全检查。
- 本轮仅更新架构图与说明，不修改冻结控制代码，不连接 PICO/G2，也不发送机器人命令。
- Graphviz 2.43.0 无警告生成正式 SVG 与 `4379×3044` PNG；实际打开 PNG 检查，新 IK 分支、颜色、文字和连线没有遮挡或裁切。当前环境未安装 Mermaid CLI，因此两份 `.mmd` 完成源文本和节点/连线一致性检查，但未在本轮生成额外 Mermaid 图片。

## 2026-09-04 代码对图逐项复核

- 复核范围：`RobotAdapter`/`IKSolver` 契约、`ControlRouter`、`DualArmLivePipeline`、`SafetyGateway`、`GripperSafetyGateway`、`G2Adapter`、`G2URDFIKSolver`、统一启动脚本和独立视频链路。仍只改图和直接说明，不改冻结运行代码，不连接硬件。
- 修正 IK 状态：当前 G2 启动入口显式创建并注入 `G2URDFIKSolver`；`RobotAdapter` 只调用已注入的求解器，不会自动识别厂商 IK，也不存在已经实现的通用 `URDFIKSolver` 或具体 `VendorIKSolver` 类。
- 修正未来策略：官方 IK 必须先封装成 `IKSolver(kind="native")`；没有官方 IK 时，也需要为该机器人配置 URDF、基座/末端坐标系、关节组和求解器，不能只复制一份 URDF 就自动适配。
- 修正执行顺序：双臂末端工作空间限制属于 `DualArmLivePipeline`，发生在 `ControlRouter → solve_ik()` 之前；夹爪 Intent 不经过该步骤，也不经过 IK。
- 修正职责：`ControlRouter` 只按 Intent 类型分发；关节/夹爪网关检查统一命令契约；G2Adapter 负责厂商映射和最终约束；跟踪误差恢复、通道隔离、Grip 抗抖和 stale 处理属于实时运行循环。
- 修正生命周期表述：`SessionSupervisor` 是架构职责，当前由 `run_pico_g2_teleop.sh` 落实；一次只选一种控制模式，视频默认开启但可用 `--no-video` 关闭。
- Graphviz 正交布线正式生成 SVG 与 `5384×3270` PNG；实际打开 PNG 检查，五个分区、颜色、文字和控制主线没有裁切或重叠。去掉了一条只表达配置关系的反向虚线，避免把配置策略误看成实时数据流。
- 运行 93 个与图直接相关的离线测试：RobotAdapter/IK 路由、双臂实时管线、安全网关、G2 Adapter、统一会话和视频协议。92 个在沙箱内通过；唯一使用本机 `socketpair` 的测试因沙箱权限报错，在允许本机进程通信后单独重跑通过。没有连接 PICO、G2 或发送物理命令。

## 2026-09-04 新手优先总图精简

- Problem / goal: 当前图已与代码一致，但把类内部机制、频率和安全阈值都展开为节点，主命令流不够醒目。重排为一张新手能先看懂、开发者又不会被误导的正式总图。
- User-visible behavior: 第一眼按顺序看到“PICO 输入 → XRFrame → Intent → 三条控制路线 → 安全 → RobotAdapter → G2”；视频和生命周期独立成行；未来功能保持灰色。
- Success criteria: 保留当前 G2 URDF IK、单关节、双臂、夹爪、统一 Adapter、独立视频和未来乐聚/官方 IK；不再绘制跨区反馈线；绿色/橙色/灰色/白色规则不变；正式 SVG/PNG 无裁切、重叠或难以追踪的交叉线。
- Out of scope: 不改变任何运行代码、阈值、频率、接口或冻结验收结论；不连接 PICO/G2。
- Approach: 合并只服务于实现细节的节点，把频率与阈值移回 README；用节点内短句保留 `RobotState/FK` 反馈和同一个 Adapter 的事实；只画前向命令主线。
- Components / files touched: 正式 DOT/SVG/PNG、两份 Mermaid、直接引用说明和本计划；桌面副本及原 V1 冻结包同步。
- Verification plan: Graphviz 正式渲染、实际打开 PNG、关键措辞一致性检查、相关离线测试和冻结包校验；不做硬件验证。

### 精简图验证结果

- 正式图从原先约 30 个可见功能节点收敛为 22 个，控制主线只保留一次分叉和一次汇合；RobotState/FK 反馈、频率和阈值不再画成长回线或独立节点，详细值仍保留在 README。
- Graphviz 正交布线无警告生成 SVG 与 `2677×2495` PNG；实际打开正式 PNG，未发现文字裁切、节点重叠或无法追踪的交叉线。
- 对照代码确认 `ControlRouter`、G2URDFIKSolver 注入、IK 前工作空间限制、RobotAdapter 边界、G2VideoSource 和统一启动脚本的措辞；未来乐聚、厂商 IK 配置、全身/行走和平台功能均保持灰色。
- 运行 38 个相关离线测试，覆盖 IK 路由、双臂实时管线、统一会话和机器人无关视频源契约，结果全部通过。未修改运行代码，未连接 PICO/G2，未发送物理机器人命令。

## 2026-09-04 机器人 IK 分类与头部跟随计划

- 机器人适配区改为两个明确类别：G2 与计划中的乐聚并排归入“本地 URDF IK（由我们求解）”；未来直接使用厂商官方 IK 的机器人单独归入“厂商原生 IK”。完成状态仍独立着色，因此 G2 为绿色，乐聚和厂商原生 IK 为灰色。
- 新增灰色“头部跟随”路线。代码证据仅包括 `XRFrame.head_pose`、G2 三个头部关节和现有关节发送能力；HeadFollowMapper、HeadOrientationIntent、机器人专用头部映射和连续运行链均未实现，不能涂绿。
- 头部跟随详细成功标准和验证边界记录在 `plans/2026-09-04-g2-head-follow.md`；本轮只更新架构与计划，不实现或运行头部控制。
- 109 个相关离线测试通过，覆盖 XR 输入契约、单关节/双臂/夹爪映射、Intent 路由、RobotAdapter/G2 契约、视频最新帧链路和统一会话管理。头部跟随仍只有前置能力，不能视为已实现。
- Graphviz 已无警告生成 SVG 和 `3124×2470` PNG；人工检查未发现文字裁切、节点重叠或难以追踪的跨区回线。
- 复查真实调用顺序后修正一处逻辑表达：Mapper/Retargeting 必须先从 `XRFrame` 生成 Intent，`ControlRouter` 再按 Intent 类型分流；双臂和计划中的头部 Mapper 不再画在 Router 后面。
- 本地 URDF IK 与厂商原生 IK 的分组边框统一改为中性颜色，只由内部节点颜色表达完成状态，避免将包含未实现乐聚适配的整个分组误读为已完成。
- 独立审查后补回启动入口的 `--mode` 来源和 IK 前末端工作空间限制；并明确同一个 Adapter 实例在安全前提供 `solve_ik()`、安全后只发送已验证命令，避免流程被误读为 Mapper 自选模式或 IK 调用两次。
- 二次复审进一步校正拓扑：双臂末端工作空间限制属于 Intent 准备阶段，明确放在 `ControlRouter → solve_ik()` 之前；夹爪 Intent 保持独立，不经过该限制。
- 最终独立复审未发现剩余或新增的 HIGH、MEDIUM、LOW 问题。
