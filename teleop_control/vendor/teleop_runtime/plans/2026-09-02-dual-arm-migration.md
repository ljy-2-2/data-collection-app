# 双臂末端遥操作迁入统一架构

Status: complete (2026-09-03); physical PICO + G2 read-only route accepted

## What

- Problem / goal: `test-teleop/` 已有并经用户真机使用确认的双臂末端映射、URDF IK 和夹爪控制，但它直接调用 GDK，尚未进入 `teleop-runtime` 的统一意图、路由、安全和 `RobotAdapter` 边界。
- User-visible behavior: 保留旧入口；新增一个 PICO + G2 双臂实时只读预览入口。用户按左右 Grip 时，终端显示对应末端目标和 IK 生成的 7/14 个关节候选值，但任何路径都不得调用 G2 运动接口。
- Success criteria:
  - 新运行时能用机器人无关的数据结构表达左、右或双侧末端目标。
  - 旧 `retarget.py` 的 Grip 离合、头显朝向坐标和左右手映射行为被迁入并由离线测试覆盖。
  - 旧 `ik.py` 的 G2 双臂 URDF IK 被适配到 `RobotAdapter.solve_ik()`，输出统一 `JointCommand`，不直接调用 GDK。
  - `ControlRouter` 能将末端意图交给所选 `RobotAdapter` 求解；结果仍必须经过现有 `SafetyGateway`。
  - 同一离线输入下，迁移后的 Retargeting 与旧实现输出一致；相关新增与回归测试通过，完整套件无功能断言失败。
  - PICO 进程只传输严格校验、带序号和时间戳的 `XRFrame`；G2 worker 只读取实时状态、计算 FK/IK、经过 `SafetyGateway` 并打印预览。
  - G2 初始化或一次 FK/IK 计算较慢时，worker 持续消费输入但只处理最新 `XRFrame`，不顺序执行已经积压的旧帧。
  - GDK 短暂没有关节状态时，本周期安全跳过并继续等待新帧；输入恢复后会话可继续，持续无可用状态则以未验收结束。
  - Grip 松开或 XR 数据过期时清除相应 Retargeting/IK 状态；所有日志明确包含 `physical_robot_command_sent=False`。
- Out of scope: 修改 Unity/PICO、控制夹爪、统一 Recorder、头部/腰部/全身映射、行走，以及任何物理机器人命令。

## How

- Approach: 保留稳定的 `XRFrame`、`JointCommand` 和 `SafetyGateway`。增加末端位姿意图及双臂 Retargeter；将旧 Pinocchio IK 包装成 G2 Adapter 持有的可注入 solver，并让 Router 只负责选择“直接关节”或“末端 IK”路线。旧代码保持不变，作为回归基线。真实验收发现 FIFO 会在 GDK 初始化期间积压，因此 worker 改为与单关节真机路线相同的 latest-only 单槽邮箱；G2 短暂缺少遥测只拒绝当前预览，不放宽 200 ms 输入时效限制。
- Components / files touched: `src/teleop/contracts/intent.py`、`src/teleop/retargeting/`、`src/teleop/robots/`、`src/teleop/control/`、XRFrame 进程间协议、新只读预览 app/wrapper、相应测试和说明文档。
- Phases: 已完成离线迁移；本阶段增加可测试的只读预览管线和 host-PICO / g2ns-GDK 双进程入口。先用假 XR/GDK 验证，真实 PICO+G2 只读验收需用户后续明确允许并手动操作 Grip。
- Risks / unknowns: 当前系统 Python 没有 NumPy/Pinocchio，因此含 IK 的离线验收必须显式使用 `/home/yuan/miniforge3/envs/g2-teleop-py310/bin/python`；IK 多关节步长需要与现有 SafetyGateway 的逐周期限制保持一致。
- Live preview note: 只读 G2 不会实际跟随 IK 候选值，因此预览管线每帧从最新遥测重新播种 IK，仅展示“如果本周期允许发送，会得到什么安全候选指令”，不模拟机器人已经运动。
- Safety boundary: 只修改 PC 端源码、测试和文档；本轮不启动 XR 服务、ADB、GDK、网络命名空间、相机或真机运动。新入口构造的 G2Adapter 必须保持 read-only，且代码中不得调用 `send_joint_command()`。
- Verification plan: 先用测试重现“启动期间旧帧积压”和“一次 GDK 状态缺失导致会话退出”，确认修复前失败；实现 latest-only 邮箱与非致命状态缺帧处理后运行双臂相关测试和完整离线套件；使用假 XR/GDK 执行端到端预览，确认产生 7/14 个通过 SafetyGateway 的候选目标、释放/过期时清状态，且输出始终为 `physical_robot_command_sent=False`；最后再由用户授权真实 PICO + G2 只读复验。

## Verification result (2026-09-02)

- 机器人无关意图、双臂 Retargeting 和 Router：9 tests passed。
- G2 URDF IK 与旧/新 Retargeting 数值回归：4 tests passed。
- 离线端到端：输出 14 个关节目标，经过 SafetyGateway 和 G2 dry-run request preview，`physical_robot_command_sent=False`。
- 新增实时只读预览：XRFrame wire、预览管线、PICO emitter 和只读 GDK worker 共 11 tests passed；其中真实 G2 URDF solver 已配合不含任何写方法的假 GDK 完成 14 关节端到端预览，并覆盖消息过期后的立即复位。
- 放宽本地临时 socket 权限后，全项目测试完整通过：`Ran 196 tests ... OK`。
- 尚未启动 PICO、GDK 或真实 G2。必须完成一次真实 PICO + G2 只读联合验收后，才能把“实时只读预览”从橙色改为绿色；双臂运动入口仍未实现。

## 2026-09-03 首次真实只读验收发现的问题

- 验收安全失败，未发送运动命令。PICO emitter 产生 `82` 个新鲜帧，但 worker 顺序读到 `81` 个超过 200 ms 的积压帧；随后一次 `get_joint_states()` 暂无消息被当作致命错误，会话退出，`previews=0`。
- 根因位于 host XRFrame 到 g2ns worker 的消费策略和只读遥测容错，不是 PICO 断线、Retargeting 或 IK 数学错误。
- 修复不得提高 200 ms 时效阈值，不得加入 GDK 写接口，也不得把旧帧用于候选命令显示。

## 2026-09-03 修复与离线验证

- worker 在初始化 GDK 前启动独立 XRFrame 读取线程，并通过单槽邮箱始终只保留最新帧；启动积压不再按 FIFO 顺序进入 FK/IK。
- 一次 `G2StateReadError` 只会复位双臂离合与 IK、跳过当前帧并继续等待；恢复后明确输出恢复日志。若会话结束时仍无状态，退出码保持失败。
- 继续执行消息前后两次 200 ms 年龄校验；计算期间已被更新帧取代的候选结果不会显示。
- 双臂专项测试 `10/10` 通过；完整离线回归 `Ran 199 tests ... OK`；Python 与三个启动脚本语法检查通过。
- 本轮没有连接 PICO、启动 GDK 或发送机器人命令。架构图中的双臂路线仍保持橙色，等待一次真实 PICO + G2 只读复验。

## 2026-09-03 第二次真实只读复验

- G2 网络、PICO ADB、XRFrame 和 G2 实时状态链路保持正常：`messages_received=592`、`frames=576`、`stale=0`、`rejected=0`、`state_unavailable=0`、`state_available_at_end=True`。
- 修复前出现的连续 `XR frame message is stale` 和一次无状态即退出的问题均未复现；全过程仍为 `physical_robot_command_sent=False`。
- 本次 `previews=0`、`idle=576`，说明运行期间没有检测到任一控制器 Grip 超过迁移自旧实现的 `0.95` 接合阈值，因此没有实际覆盖 Retargeting、IK 和 7/14 关节候选输出。
- 随后的纯 PICO 观测收到 `714` 个新鲜时戳，但头部/左右手柄位姿、Grip、Trigger 和按键始终全部为零，`controller_activity_seen=False`。这证明通道已连接，但 PICO Runtime 当时没有输出有效追踪内容；不是 Grip 阈值、G2、Retargeting 或 IK 故障。
- 结论：连接与稳定性复验通过，双臂只读功能验收未完成，架构图继续保持橙色。下一步让 Galaxis 应用保持前台和头显唤醒，开启 `Head`、`Controllers`、`Send Data`，在控制器模式验收时暂时关闭 `Hand Tracking`，再用纯 PICO `observe_xr.py` 确认非零位姿及左右 Grip 可达到 `1.00`。

## 2026-09-03 最终真实只读验收

- PICO 输入恢复后，纯 XR 观测确认头显与双手柄位姿非零、左右 Grip 均可达到 `1.00`、`stale_frames=0`、`controller_activity_seen=True`。
- 真实 PICO + G2 预览共取得左臂 `26` 帧、右臂 `22` 帧、双臂 `47` 帧：分别生成 `7`、`7`、`14` 个关节候选，覆盖三种控制组合。
- 所有候选均显示 `safety_validated=True`，最大关节单步不超过 `0.002 rad`；日志未出现 `REJECTED`、`STATE_UNAVAILABLE` 或 `physical_robot_command_sent=True`。
- 最后一份粘贴日志未包含第二次运行的最终 summary；完整 60 秒无过期、无拒绝、状态正常和正常释放已由紧邻的上一轮复验 summary 单独证明。两轮证据合并满足本阶段验收标准。
- 结论：双臂末端 Retargeting → `EndEffectorPoseIntent` → `ControlRouter` → `G2Adapter.solve_ik()` → `SafetyGateway` → 只读预览路线验收通过并冻结。该结论不代表双臂真机运动入口已经实现或授权。
