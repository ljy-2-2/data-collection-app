# 单关节/双臂/视频统一入口

Status: frozen（PICO + G2 统一入口真机联合验收通过，2026-09-03）

## What

- Problem / goal: 现有总入口只能组合“单关节 + 视频”，双臂与夹爪仍需单独启动；需要用同一个 PC 入口选择单关节或双臂，并由同一个 SessionSupervisor 管理视频、PICO watcher 和退出清理。
- User-visible behavior: `run_teleoperation.sh --mode joint-jog --video` 启动单关节与视频；`--mode dual-arm --video` 启动双臂、夹爪与视频。为兼容原 V1，省略 `--mode` 仍为 `joint-jog`，省略 `--video` 仍默认开启视频。
- Success criteria: 未带 `--enable-motion` 时在任何设备操作前拒绝；未知模式在预检前拒绝；两种模式只启动各自的控制入口；双臂入口保持 `0.5 rad/s`，单关节速度配置不变；PICO、视频和控制由一个会话统一停止；原有入口和完整离线测试继续通过。
- Out of scope: 修改 Unity、控制算法、IK、RobotAdapter、速度/限位、视频协议、录制、全身、行走、乐聚适配，以及本次执行任何 PICO/G2 真机测试。

## How

- Approach: 在现有 `apps/run_pico_g2_teleop.sh` 上增加严格的 `--mode joint-jog|dual-arm` 解析，根据模式选择现有 `run_pico_g2_live.sh` 或 `run_pico_g2_dual_arm_live.sh`；保留既有 PICO/XR 预检、G2 网络、视频启动、互斥锁和子进程清理。双臂子入口增加内部使用的 `--no-auto-pico`，避免总入口已经拥有 PICO watcher 时重复恢复端口。
- Components / files touched: `apps/run_pico_g2_teleop.sh`、`apps/run_pico_g2_dual_arm_live.sh`、会话/PICO 启动测试、根目录与 runtime README、现有架构状态源图和生成图（只更新统一入口状态）。
- Phases: 先写失败测试；实现模式分发；运行模拟端到端与完整回归；独立审查；更新文档和图。
- Risks / unknowns: Bash 子进程退出可能遗漏孙进程；模式参数可能错误传给不同控制入口；测试使用假 PICO、假网络、假视频和假控制脚本覆盖这些路径。三个长期子进程各自在独立进程组中运行但保留父终端会话，以同时满足完整清理和终端范围的 sudo 凭证复用；停止时先终止整组，超时后再强制终止整组。
- Safety boundary: 本功能只修改本地代码并运行离线替身测试。真实控制仍必须显式使用 `--enable-motion`；本轮不连接 PICO、不建立 G2 网络、不调用 GDK。
- Verification plan: 先观察新增模式测试失败；通过后运行 Bash 语法检查、会话管理测试、完整 Python 测试；使用全部注入的假命令分别实际运行两个 CLI 模式并检查启动参数、失败拒绝和统一清理；不进行物理硬件验证。

## Result

- 统一入口现支持 `--mode joint-jog|dual-arm --video`；原 V1 省略模式和视频参数的行为保持不变。
- 双臂模式复用现有双臂/夹爪入口，并通过 `--no-auto-pico` 让父级 SessionSupervisor 独占 PICO watcher；没有复制控制算法或速度配置。
- 新增测试先在旧代码上按预期失败。首次真机启动暴露后台 `setsid` 进程无法使用终端 sudo 凭证；回归测试复现后改为“同一终端会话、独立进程组”，并由父管理器通过 `G2_SUDO_PREAUTHENTICATED=true` 告知视频和控制子入口跳过重复 `sudo -v`。18 项定向测试通过；其中两项会实际运行视频与控制基础启动器，证明独立运行时执行 `sudo -v`，继承父级认证时跳过 `sudo -v`，但两种情况的实际特权调用都保持 `sudo -n`。完整运行时测试 245 项通过、5 项按环境跳过。
- 控制启动前会再次确认 PICO watcher 存活；测试证明 watcher 在视频等待期间退出时不会启动任何控制进程。另一个测试创建忽略 TERM 的控制孙进程，证明会话退出会终止整个进程组，不留下运动相关后台进程。
- 三个 Bash 入口通过语法和帮助页检查；真实入口在没有 `--enable-motion` 时以退出码 2 在任何设备操作前拒绝。
- 真机复验前发现一个上次遗留的 `auto_connect_pico.sh --watch` 继承了父级锁 FD 9，导致主会话结束后新入口仍被错误拒绝。已停止该只负责 ADB 的残留 watcher，并让 PICO watcher、视频、控制三个长期子进程启动时显式关闭 FD 9。新增测试以 `SIGKILL` 模拟父管理器无法执行 cleanup 的异常退出，确认孤儿子进程不再占用会话锁；完整离线测试现为 248 项通过、5 项按环境跳过。
- 实现阶段没有连接 PICO/G2；随后在用户逐次明确授权和硬件急停准备下完成了真实联合验收。单关节 + 视频、双臂 + 双夹爪 + 视频均可由同一个入口启动，退出时统一停止控制和视频。

## 冻结验收（2026-09-03）

- `--mode joint-jog`：22 个已配置位置关节可选；控制循环最高 `10 Hz`，手臂最高 `0.2 rad/s`，身体和头部最高 `0.1 rad/s`，行为保持不变。
- `--mode dual-arm`：本计划初版使用 `50 Hz`；2026-09-04 连续性阶段已将最终冻结默认值更新为 `100 Hz`，最高仍为 `0.5 rad/s`。左右 Trigger 控制左右夹爪，每侧夹爪命令最高 `20 Hz`。
- `--video`：G2 `kHeadColor` 使用 `640×400` 原始 JPEG，实际约 `30 FPS`；PC 以最高 `60 Hz` 轮询并按时间戳去重，只分发最新帧。
- 操作者完成 PICO + G2 真机联合操作并确认双臂、双夹爪和视频正常。模式在启动时选择，运行中不热切换；切换时先结束当前会话再用另一个 `--mode` 启动。
- 冻结前完整 Python 回归结果为 `249` 项通过、`5` 项因需要可选 IK 环境而跳过、`0` 项失败。
