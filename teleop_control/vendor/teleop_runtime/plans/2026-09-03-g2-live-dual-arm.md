# G2 双臂末端真机执行

Status: implemented（2026-09-04：连续性修复已完成最终真机复验并重新冻结）

## What

- Problem / goal: 已验收的 PICO + G2 双臂路线目前只生成和显示安全的 IK 关节候选，不会调用机器人写接口；需要在同一套统一架构中增加明确授权的真机执行入口。
- User-visible behavior: 左 Grip 控制左臂、右 Grip 控制右臂、双 Grip 控制双臂；松开对应 Grip 后该臂停止接收新目标。G2 双臂最高关节速度为 `0.5 rad/s`，启动必须显式提供 `--enable-motion`。现有单关节模式保持手臂 `0.2 rad/s`、身体和头部 `0.1 rad/s`。
- Success criteria:
  - 真机路线仍为 `XRFrame → DualArmRetargeter → EndEffectorPoseIntent → ControlRouter → RobotAdapter.solve_ik() → SafetyGateway → RobotAdapter.send_joint_command()`。
  - 复用旧版已验证的 GDK `joint_servo_control()`，但调用只存在于 G2 Adapter 的注入 sender 内。
  - 50 Hz 时 IK 单周期最大关节步长不超过 `0.010 rad`，最终 GDK 请求报告最高 `0.5 rad/s`。
  - 每周期开始只取 mailbox 最新帧；计算期间到达的新帧留给下一周期。当前帧真正过期、松开 Grip、遥测缺失、IK/限位/耦合检查失败均不得发送命令。
  - 末端目标必须位于 G2 左右手各自的保守工作空间；连续目标与真实遥测误差超过 `0.10 rad` 时停止发送。
  - 双 Grip 松开时长按 B 一秒退出；启动脚本可显式选择 `--dry-run`，真机仍必须选择 `--enable-motion`。
  - 只读预览和现有单关节 `0.2 rad/s` 入口行为不变。
  - 离线测试验证左臂 7、右臂 7、双臂 14 关节发送以及全部拒绝路径；真实 G2 运动必须另行获得当次授权。
- Out of scope: Unity 修改、夹爪、腰部/头部/行走、视频链路修改、在本轮自动启动真实机器人。

## How

- Approach: 新增 `DualArmLivePipeline`，它保留连续 IK 积分，但在 Grip 释放或异常时清除对应锚点与 IK 状态；候选命令先通过现有 SafetyGateway，再在发送前二次检查 XRFrame 仍新鲜且仍是最新帧。G2 新增窄接口的 servo sender，将统一关节命令转换为 `JointServoControlReq`。新增独立 live worker 和带 `--enable-motion` 门闩的启动脚本，避免只读入口意外变成可运动入口。
- Components / files touched: `src/teleop/control/dual_arm_live.py`、`src/teleop/robots/g2.py`、`apps/pico_g2_dual_arm_live.py`、两个启动脚本、测试及 README。
- Risks / unknowns: `0.5 rad/s` 是最大关节速度而非每帧角度；真实跟随率仍受 GDK、网络和 IK 耗时影响。当前已知左腕耦合约束继续在 Adapter 最终边界检查，其他厂商内部约束仍可能由 GDK 拒绝。保守长方体工作空间不是完整自碰撞模型。
- Safety boundary: 本功能代码默认不能隐式运动；启动必须显式选择 dry-run 或真机确认，Grip 是持续使能开关，XR 消息 200 ms 过期，命令 TTL/servo 周期有限，末端工作空间、关节位置、单周期跳变、连续目标跟踪误差及已知耦合约束在发送前检查。双 Grip 松开时长按 B 一秒可结束；真实测试还必须准备硬件急停。本轮只进行离线测试，不连接 PICO/G2，不执行物理运动。
- Verification plan: 先运行新增测试确认缺少 live 管线而失败；最小实现后运行双臂、G2 Adapter 和完整单元测试；最后只做假 GDK 端到端运行，确认发出 7/14 关节 servo 请求以及松开/过期/错误时零发送。真实 PICO + G2 验收留待用户再次明确授权。
- Verification result: 失败测试先确认缺少连续伺服参考、末端工作空间、B 停止以及 GDK `None` 成功返回兼容；修复后完整测试 214 个通过、5 个因可选环境跳过，Pinocchio 环境中的 13 个真实 URDF IK/双臂关键测试通过。Shell 语法、Python 编译、帮助和缺少模式时返回退出码 2 的拒绝行为均通过。该条记录实现阶段；真实硬件结果见下节。

## Physical acceptance result

- 在用户逐次明确授权、清空机器人周围并准备硬件急停后，PICO + G2 双臂真机路线完成验收。
- 双臂以 `50 Hz` 控制循环运行，最高关节速度最终冻结为 `0.5 rad/s`；左/右 Grip 分别作为对应手臂的持续使能开关。
- 左臂、右臂和双臂共同控制均已走通；URDF IK、SafetyGateway、工作空间、TTL、关节限位和 G2Adapter 最终边界均保留。
- 夹爪是随后增加的独立能力，冻结结果记录在 `2026-09-03-g2-gripper-migration.md`；它没有改变本计划的双臂速度或 IK 路线。
- 冻结前完整 Python 回归为 `249` 项通过、`5` 项环境跳过、`0` 项失败。

## 2026-09-04 连续性问题与修复计划

- 操作者报告：双臂运行一段时间后会暂停，需要等待并重新操作 Grip 才恢复；旧 `test-teleop/send.py` 没有该问题，因此撤销“双臂 + 夹爪 + 视频已冻结”的结论，单关节和视频的既有验收不受影响。
- 已确认的结构差异：旧版 100 Hz 循环只以约 10 Hz 刷新关节遥测，末端 FK 只在 Grip 接合时取一次；新版在每个 50 Hz XRFrame 中同步读取 G2 状态、计算 FK/IK、安全检查和发送。
- 主要怀疑点：新版在上述计算完成后要求原 XRFrame 仍是 mailbox 的绝对最新帧。20 ms 内到达的正常下一帧会触发 `XR frame was superseded before send`，随后进入全局 safety latch，必须松开两个 Grip/Trigger 才能恢复。
- 测试缺口：现有测试覆盖“新帧在手臂/夹爪发送期间到达”，没有覆盖“新帧在 G2 状态读取或 IK 期间到达”。
- 最小修复：先用失败测试复现；只要选中的帧仍满足严格时间 TTL，就允许本周期完成，下一周期自然处理最新帧。正常帧覆盖不再作为安全故障；真正过期、工作空间/限位/耦合错误和未知物理结果仍保持拒绝。
- 同时把 FK 改为只在 Grip 新接合时惰性计算，保持 RobotAdapter 边界不变并减少每帧计算。增加无视频对照入口只用于定位资源竞争，不改变默认视频行为。
- 本阶段先完成离线替身、专用 Pinocchio IK 和完整回归；未经用户再次明确授权，不运行 PICO + G2 真机运动。

## 2026-09-04 连续性修复结果

- 已用确定性并发测试复现根因：G2 状态读取期间收到正常的新 XRFrame，旧逻辑会把仍在 TTL 内的本轮帧判为 `superseded`，进入全局安全锁并要求操作者松手恢复。
- 已改为：本周期开始取最新帧；只要该帧在每次物理调用前仍满足严格时效，就完成本周期。期间到达的新帧由下一周期处理，不再把正常高频输入当作故障。
- 末端 FK 改为 Grip 新接合时惰性计算；空闲帧和持续握持帧不再重复计算 FK。Retargeting、IK、RobotAdapter 和所有安全边界保持不变。
- 统一入口新增 `--no-video`，只用于控制链/视频链 A/B 诊断；默认仍开启视频。
- 通用环境完整回归：`252` 项通过、`5` 项因可选 IK 环境跳过、`0` 项失败。G2 Pinocchio 环境中的 `14` 项 URDF IK、旧版映射等价和双臂只读专项测试全部通过。
- 最终结论：后续连续性修复、完整离线回归和真实 PICO + G2 双臂、双夹爪、视频联合复验均已完成，本阶段已经重新冻结；最终参数与证据见 `2026-09-04-dual-arm-smooth-loop.md` 和冻结里程碑。
