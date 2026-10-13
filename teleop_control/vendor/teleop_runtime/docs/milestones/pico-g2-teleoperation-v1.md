# PICO + G2 遥操作 V1 冻结里程碑

首次冻结：2026-09-01  
历史完整冻结：2026-09-03  
最终重新冻结：2026-09-04  
当前状态：V1 已冻结；单关节、双臂、双夹爪、视频和统一入口均完成相应验收

## V1 scope

当前 V1 包含两条数据独立、可同时运行的主线：

```text
control: PICO -> XRFrame -> 单关节或双臂/夹爪 Intent -> ControlRouter
        -> 直接关节或 IK/夹爪路线 -> SafetyGateway -> G2Adapter -> G2

video:   G2 kHeadColor -> VideoFrame -> VideoHub -> TCP 63902
        -> PICO RobotVideoClient
```

- 单关节直控覆盖 G2 已配置的 22 个身体、头部和双臂位置关节，不使用 IK。
- 双臂末端路线使用迁移后的 Retargeting 和 G2 URDF IK；左右 Trigger 分别控制左右夹爪。
- Robot video uses the verified G2 `kHeadColor` source at `640×400` and about `30 FPS`.
- Video and control share lifecycle management only; video never enters `XRFrame`, retargeting, or joint safety.
- `test-teleop` is a legacy reference and is not imported or launched by V1.

## Verified evidence

- Operator-supervised PICO-to-G2 direct-joint commands reached GDK, including continuous Grip + stick control and safe long-B exit.
- The G2 camera reached the PC and the PICO small/large display; closing clears the texture to black.
- Video and direct-joint control ran simultaneously using the existing two-terminal launchers.
- A PICO left-eye acceptance screenshot is stored at `Galaxis-VR-Teleop/Builds/Screenshots/PICO-G2-left-eye-acceptance-2026-09-01.png` relative to the Teleoperation project root.
- The one-command `SessionSupervisor` is covered by seven focused offline tests: explicit authorization refusal, exclusive session locking, PICO/XR preflight failure, early video failure, normal child completion, unexpected PICO-watcher exit, and signal cleanup.
- On 2026-09-01 the operator completed the authorized 60-second unified-entry live acceptance and reported no problem: G2 video was visible in PICO, direct-joint control worked, and the supervised session exited normally.
- On 2026-09-04 the operator completed the final continuous PICO + G2 dual-arm, dual-gripper, and video live re-test and confirmed that the reported interruptions were resolved.
- Final offline freeze regression: the normal environment passed `268` tests with `5` optional Pinocchio tests skipped; the G2/Pinocchio environment passed `273/273` tests, including real G2 URDF IK.

## Freeze decision

V1 单关节、双臂、双夹爪、G2-to-PICO/PC 视频和统一 `SessionSupervisor` 入口均为冻结范围。2026-09-04 发现的双臂间歇性停顿曾临时解除“双臂 + 夹爪 + 视频”路线冻结；完成连续性修复、完整离线回归和最终真机联合复验后，该路线已经重新冻结。全身、行走、第二种真实机器人和统一 Recorder/Monitor 不属于本次冻结范围。

## Deferred work

- whole-body pose retargeting and locomotion
- Leju robot and other headset adapters
- unified Recorder, Monitor, and collection-platform output
- complete collision, workspace, and vendor coupled-constraint models
- vendor-native `solve_ik()` override（统一接口已存在，当前尚无第二个厂商实现）

## 2026-09-03 历史冻结记录（当时参数）

下面的 `50 Hz` 是 2026-09-03 首次完整冻结时的历史参数，已经由文末 2026-09-04 最终重新冻结记录中的 `100 Hz` 双臂控制参数替代。

### 已验收能力

- 统一入口可用 `--mode joint-jog|dual-arm` 选择单关节或双臂 + 双夹爪，并与视频共同启停；运行中不热切换。
- 单关节控制循环最高 `10 Hz`：手臂最高 `0.2 rad/s`，身体和头部最高 `0.1 rad/s`。
- 双臂控制循环 `50 Hz`，最高关节速度 `0.5 rad/s`；夹爪每侧命令最高 `20 Hz`。
- PICO 实测 Tracking Hz 约 `90 Hz`；PC 侧按 `50 Hz` 读取最新 XRFrame。
- G2 `kHeadColor` 冻结为 `640×400` 原始 JPEG、约 `30 FPS`；PC `60 Hz` 是读取轮询上限。
- PICO 支持视频小窗、可拖动大屏、退出大屏和关闭后清黑；PICO 与 PC 可同时观看。

### 软件与交付证据

- 冻结前完整 PC 回归：`249` 项通过，`5` 项因需要可选 IK 环境而跳过，`0` 项失败。
- Unity EditMode：`12/12` 通过；PICO 已验收 Tracking Hz 开启时约 `90 Hz`、停止发送后为 `-- Hz`。
- 正式 APK：`Galaxis-VR-Teleop/Builds/Android/Galaxis VR Teleop_1.1.7.apk`。
- APK SHA-256：`b80a79924338e7dc428c4e318f0105a625d9d95cb48f1288232f5bb043c9ccf1`。
- 追踪验收截图位于 `Galaxis-VR-Teleop/Builds/Screenshots/`。

### 当前仍未完成

- 全身重定向、腰腿平衡和行走。
- 乐聚等第二种真实机器人 Adapter。
- 集合平台 VideoClient、PC 统一 Recorder/Monitor。
- 完整自碰撞模型、统一硬件急停接口，以及标准化视频延迟/长稳量化报告。

`test-teleop` 继续作为旧双臂映射、URDF IK 和录制逻辑的回归参考，不参与正式运行。`dry-run` 继续作为安全测试工具保留，不属于正常真机主流程。

## 2026-09-04 连续性缺陷重新打开

- 根因：控制周期在 G2 状态读取/FK/IK 期间收到下一帧时，把仍在 TTL 内的本轮帧误判为 `superseded`，触发全局安全锁，所以操作者会感到“停几秒、需要重新松握手柄”。
- 修复：每周期开始消费最新帧；周期中到达的新帧留给下一周期。当前帧仍在每次发送前接受 TTL 检查，真正安全错误仍会锁停。
- 性能：FK 只在 Grip 新接合、建立锚点时执行，不再每个 50 Hz 周期重复执行。
- 诊断：统一入口保留默认视频，并新增 `--no-video` A/B 开关。
- 离线证据：通用回归 `252` 项通过、`5` 项环境跳过；专用 G2/Pinocchio 环境 `14` 项全部通过。尚未把离线结果冒充为真机通过。

## 2026-09-04 最终重新冻结记录

- 双臂控制按 `100 Hz` 消费最新 PICO 帧，双臂最高 `0.5 rad/s`；G2 完整关节遥测约 `10 Hz` 独立刷新，夹爪每侧最高 `20 Hz`。
- 工作空间越界采用原边界饱和；`0.10–0.20 rad` 跟踪误差自动重锚，达到 `0.20 rad` 才锁停；Grip 普通释放使用 `30 ms` 抗抖。
- 同帧双夹爪由 G2Adapter 合并为一次 `dual_tool` 请求；夹爪操作不再中断仍按住 Grip 的手臂路线。
- stale 输入仍禁止当前帧发送，但不清空夹爪映射；新鲜帧恢复后自动继续，连续日志按事件汇总。
- 冻结前通用环境 `268` 项通过、`5` 项可选 Pinocchio 测试跳过；G2/Pinocchio 专用环境 `273/273` 全部通过。
- 操作者完成持续双臂、双夹爪与视频的最终真机联合复验并确认无问题，冻结结论成立。
