# 总架构图说明

当前总图：[SVG](teleoperation-status.svg) / [PNG](teleoperation-status.png)。
Graphviz 源文件是 [teleoperation-status.dot](teleoperation-status.dot)；两份 Mermaid 分别为
[从上往下](teleoperation-overall.mmd) 和 [从左往右](teleoperation-overall-lr.mmd)。
更新日期：2026-09-09。

## 配色与当前进度

- 绿色：对应能力已实现并完成所标范围的验收。头部绿色只表示 `[-8°, +45°]` 软件安全范围内的链路已冻结，不表示 G2 完整右转范围正常。
- 橙色：部分能力、外部硬件限制或跨厂商验证待完成。G2 右转硬件限制与 RobotAdapter 的第二种机器人验证是两个不同问题。
- 灰色：计划中。乐聚适配、官方 IK、全身/行走和统一数据采集等尚未实现。
- 白色虚线：旧代码参考，不参与正式运行。

## 头部这条线怎么读

PICO 的 head_pose 经 HeadFollowMapper 变成“相对起点左转/抬头多少”的 HeadOrientationIntent。
G2HeadJointMapper 使用 G2 的真实起点、轴方向和关节限位，将它变成 JointPositionIntent。
ControlRouter 再把关节目标交给 DirectJointBackend，得到 JointCommand。
HeadFollowSession 限制角度、目标变化和反馈领先量，SafetyGateway 检查命令；
通过后由 G2Adapter 调用注入的 G2NormalHeadSender，最终调用 GDK 头部伺服接口。
这条路线不调用 solve_ik()。idx12 维持启动角度，因为厂商接口要求三个头部角度。

图中共享节点表达复用同一套类和接口，不表示几个进程同时持有一个对象。
独立头部入口仍与统一会话互斥。统一入口已支持 dual-arm + --head-follow：一个控制 worker 内部两个 Adapter 各自保存臂/头状态，共用一个 Robot，串行调用；视频另有只读进程。
2026-09-09，操作者又在 G2 本机操作界面复现了“向右只能转一点/受阻”的现象，因此该不对称不能归因于 PICO 姿态映射、PC 控制循环或统一会话。软件临时使用绝对 `[-8°, +45°]` 隔离已知异常区；这不是厂家机械限位，也没有修复 G2 本体故障。图中头部软件链标绿，旁边的 G2 本体限制保持橙色。

| 架构职责 | 当前源码 |
|---|---|
| 头显角度意图 | src/teleop/retargeting/head_follow.py |
| G2 轴映射 | src/teleop/robots/g2_head.py |
| 控制、限幅、路由与安全 | src/teleop/control/head_follow_pipeline.py |
| G2 反馈与最终伺服发送检查 | src/teleop/robots/g2_head_servo.py |
| 独立会话入口 | apps/pico_g2_head_follow.py |
| 统一会话组装 | apps/pico_g2_dual_arm_live.py |
| G2 头通道与共享 SDK 调用检查 | src/teleop/robots/g2_head_lane.py |
| 异步诊断日志 | src/teleop/recording/head_journal.py |

末端控制才需要选 IK。G2 当前使用本地 G2URDFIKSolver；未来乐聚也可以接专用 URDF 求解器。
官方 IK 完成封装和验证后可作为另一种求解器；图中的分组是求解方式，不是机器人品牌永久分类。
单关节、夹爪、头部都绕过 IK。

## 日志、验收与下一步

正常头部入口自动保存 JSONL 诊断日志，它还不等于用于训练的统一 Recorder。
详细参数、设备连接、运行命令和停止方法见 [运行时教程](../../README.md#怎么启动)。
原定 Python 3.10 环境的完整 `410` 项离线测试通过。操作者已经验证头部链路可驱动实体，且在机器人本机操作中复现相同右转问题；因此软件按当前受限范围冻结，完整右转范围在厂家检修后重新验收。关闭头部时保留原流程。

统一启动、头部开关与退出行为见[整合设计](../../plans/2026-09-08-head-unified-session.md)。
冻结边界见[受限冻结里程碑](../milestones/pico-g2-teleoperation-v1-1.md)。

## 重新生成图片

在 teleop-runtime 目录执行：

```bash
dot -Tsvg docs/architecture/teleoperation-status.dot -o docs/architecture/teleoperation-status.svg
dot -Tpng -Gdpi=130 docs/architecture/teleoperation-status.dot -o docs/architecture/teleoperation-status.png
```

生成后应打开图片检查文字、颜色和连线。Mermaid 文件是同一逻辑的文字版本，
当前环境未安装 Mermaid 渲染器，因此本次只检查其节点和连线文本一致性。
