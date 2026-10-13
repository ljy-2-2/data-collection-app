# 机器人相机到头显的视频回传

Status: functionally frozen（G2 → PC → PICO/PC 实机显示验收通过，2026-09-03）

## What

- Problem / goal: 将机器人看到的画面经 PC 低延迟显示在 VR 头显中，同时保留 PC 预览；先支持 G2 + PICO，结构上允许替换为乐聚机器人或其他头显。
- User-visible behavior: 用户连接机器人和头显后，在 PICO 当前“视觉与录制”页面打开“机器人视角”，看到 G2 头部彩色相机画面；PC 仍可同时打开诊断预览。断流时显示明确状态，恢复后自动重连，不显示积压的旧画面。
- Success criteria:
  - 实测并记录 G2 `kHeadColor` 的 SDK 输出尺寸、源帧率和持续 30 FPS 能力；不把配置值当作实测结果。
  - 实时显示始终使用最新帧；跨模态录制同时保留设备原始时间和 PC 单调时钟接收时间，不能直接假设 G2 与 PICO 的设备时钟相同。
  - 机器人侧统一输出 `VideoFrame`，G2/乐聚差异不进入 PC 视频核心和 Unity 显示逻辑。
  - PC `VideoHub` 同时服务 PICO、PC 诊断和未来集合平台；慢客户端只丢自己的旧帧，不能拖慢其他客户端。
  - PICO 在后台接收，Unity 主线程只取最新完整帧并更新纹理；控制端口 `63901` 与视频端口 `63902` 完全独立。
  - G2 → PC → PICO 连续显示、断线重连、延迟/FPS/丢帧统计通过实机验收；整个视频流程不发送机器人运动指令。
- Out of scope: 本阶段不实现立体双目、360° 视频、音频、云端公网传输、乐聚实机接入，也不重写已冻结的单关节控制链路。

## How

- Approach: 保留已经验证的 G2 原始 JPEG 读取和统一 `VideoFrame`，新增机器人无关的 `RobotVideoSource` 能力接口；把当前单观看者 `VideoBridge` 升级为最新帧多路分发的 `VideoHub`。第一版 PICO 直接接收原始 JPEG，避免 PC 再编码造成画质损失；Unity 后台线程只覆盖一个最新帧槽，主线程复用纹理，不为每一帧创建新对象。显示按最新帧运行，Recorder 则用 PC 单调时钟接收时间对齐 XR/视频，并保留各设备原始时间用于审计。完成端到端指标后，再依据证据决定是否增加 H.264/WebRTC 硬件解码传输。浏览器预览只是 `VideoHub` 的一个诊断消费者，不是集合平台接口。
- Components / files touched:
  - PC: `src/teleop/vision/`、G2 camera source/launcher、视频指标与测试。
  - Unity（需单独确认后才修改）: 独立 `RobotVideoClient`、协议解码、最新帧槽、机器人视角 UI；不改现有 `TcpHandler` 63901 控制协议，不混入 `UICameraCtrl` 的 PICO VST 录制职责。
  - Docs: 本计划与 `docs/architecture/video-stream-status.*`。
- Phases:
  1. **已完成（2026-09-01）** 能力探测：只读查询 G2 相机 shape/FPS，测试帧大小、重复时间戳、实际交付 FPS 和抖动，确定“原生最高档”；输出同时标明 SDK 声明值和实际测量值。
  2. **已完成（2026-09-01）** PC 多路分发：实现 `RobotVideoSource`、G2 的 `G2VideoSource`、`VideoHub` 和每客户端 latest-only 发送；自动化测试验证两个客户端同时接收、单客户端断开隔离和慢客户端隔离。
  3. **已完成（2026-09-01）** PICO 接收器：经 USB ADB/Wi-Fi 连接 63902，后台收包，主线程解码最新 JPEG，在“视觉与录制”页面显示机器人视角和连接/FPS 状态。客户端独立于现有 `TcpHandler`；已验证分片读取、非法帧拒绝、latest-only 覆盖、自动重连和干净停止。
  4. **功能验收完成（2026-09-03）** G2 + PC + PICO 完成清晰度、连续显示、断流恢复、PICO/PC 并行观看以及与 63901 遥操作共存的实机验收。精确 glass-to-glass 延迟和标准化长稳统计尚未量化，作为后续性能基准，不阻塞 V1 功能冻结。
  5. 质量优化：若原始 JPEG 达不到目标，再在相同接口后增加 H.264/WebRTC transport；优先硬件编解码，不改变机器人或头显的上层接口。
  6. 平台与多设备：集合平台接入 `VideoHub`；新增 `LejuVideoSource` 或其他 `HeadsetVideoClient`，用相同验收合同验证。
- Risks / unknowns:
  - 当前 GDK `kHeadColor` 接口已实测为原生 `640x400` JPEG、约 30 FPS；这不等于物理传感器绝对上限，当前接口也没有验证过更高分辨率选择能力。
  - `VideoHub` 已支持多个独立观看者，并已用 G2 真机验证 PC 与 PICO 并行观看。
  - Unity `Texture2D.LoadImage` 适合第一版验证，但高分辨率/高帧率可能造成 CPU 与 GC 压力；是否升级硬件解码必须由 PICO 实测决定。
  - G2、PICO、PC 的原始时间戳可能属于不同时间域；低延迟显示不等待跨设备配对，数据录制先以 PC 接收单调时钟对齐，精密同步另做时钟偏移标定。
  - USB 需要额外 ADB reverse `tcp:63902 tcp:63902`；Wi-Fi 需要把视频服务安全绑定到选定局域网地址，而不是无条件暴露所有网卡。
- Safety boundary: 相机读取和视频显示为只读，不调用 GDK 写接口，不产生 `JointCommand`，不让机器人运动。用户已在 2026-09-01 明确确认 Phase 3 的 Unity 修改；只新增独立 63902 接收器并最小挂接动态 UI，不改现有 `TcpHandler` 63901、VST 录制职责或机器人控制。任何实机控制仍按控制链路原有确认规则执行。
- Verification plan: 每阶段先跑单元/协议测试，再跑本机双客户端测试，再做 G2 只读视频测试，最后由用户佩戴 PICO 完成可视验收；报告中严格区分配置值、程序测量值和肉眼确认结果。

## Phase 1 evidence

- 2026-09-01, physical G2 read-only, 30-second run with a 30 Hz polling ceiling:
  - GDK declared `kHeadColor` as `640x400` at `30.092 FPS`.
  - The worker delivered 712 unique frames: measured source `23.726 FPS`, PC delivery `23.779 FPS`.
  - It skipped 188 repeated source timestamps; unique source intervals ranged from `31.314 ms` to `68.325 ms`.
  - Average original JPEG size was `35,447 bytes` (about `6.74 Mbit/s` at the measured delivery rate; about `8.53 Mbit/s` at the declared rate).
  - `VideoBridge overwritten=711` is expected with no viewer: latest-only storage replaced every unpublished frame and did not build a queue.
  - Every layer reported `physical_robot_command_sent=False`.
- Interpretation: PC forwarding kept pace with accepted source frames, so the bridge is not the observed bottleneck. Polling at almost exactly the declared camera cadence can repeatedly sample the same latest frame and then miss the following frame; the `31..68 ms` interval range supports this sampling-alias explanation. Phase 1 therefore needs one 60 Hz polling run before deciding whether the real source ceiling is near 30 FPS or near 24 FPS. The 60 Hz value is a read polling rate, not a claim that the camera or output can produce 60 FPS; repeated timestamps remain discarded.
- 2026-09-01, physical G2 read-only, 30-second run with a 60 Hz polling ceiling:
  - GDK declared `640x400` at `30.009 FPS`.
  - The worker delivered exactly 900 unique frames: measured source `29.967 FPS`, PC delivery `30.033 FPS`.
  - It discarded 899 repeated timestamp reads, as expected when polling roughly twice per source frame.
  - Average original JPEG size was `35,617 bytes`, approximately `8.55 Mbit/s` at 30 FPS.
  - Unique source intervals ranged from `31.283 ms` to `66.693 ms`; one occasional long interval did not reduce the 30-second average below 29.9 FPS.
  - `VideoBridge overwritten=899` is expected because no viewer was attached, and all layers reported `physical_robot_command_sent=False`.
- Phase 1 conclusion: the verified current native G2 profile is `kHeadColor`, `640x400`, JPEG, approximately 30 FPS. Use a 60 Hz read-polling ceiling plus timestamp deduplication to reliably capture the 30 FPS source. The PC bridge kept pace and was not the limiting stage. Phase 1 is frozen; the next phase is the robot-independent `RobotVideoSource` contract and multi-viewer `VideoHub`.

## Phase 2 evidence

- Added an immutable `VideoStreamCapability` and runtime-checkable `RobotVideoSource` contract. `G2VideoSource` implements that contract while retaining GDK init/read/release ownership and timestamp deduplication.
- Replaced the sequential viewer loop with `VideoHub`: every TCP viewer owns an independent sender thread and last-sequence cursor, while all viewers read the same one-frame mailbox.
- Automated socket tests prove two viewers receive the same live frame, disconnecting one viewer does not stop the other, and a deliberately non-reading viewer cannot block a fast viewer from receiving the newest 512 KiB frame stream.
- `VideoBridge` remains a compatibility alias, so existing launch commands continue to work. No Unity or robot-control file changed, and no test invokes a physical robot command.
- Full regression passed `163/163`; Python compilation and launcher shell syntax checks passed. The real `apps/video_bridge.py` entry started with `viewers=multi` on a loopback random port, exited cleanly, and removed its owned Unix socket.
- Phase 2 conclusion: the PC fan-out foundation is complete. Phase 3 is the PICO `HeadsetVideoClient`; it will connect as one ordinary `VideoHub` viewer without changing the G2 source.

## Phase 3 implementation evidence

- Added an independent Unity `Galaxis.RobotVideo` assembly containing the validated GVID/JPEG protocol, a one-frame latest-only slot, a reconnecting background TCP receiver, and a main-thread `RobotVideoClient` that reuses one `Texture2D`.
- Added the robot view to the existing `视觉与录制` page without editing the serialized scene/Prefab and without changing `TcpHandler` or TCP 63901. The disconnected viewport is black instead of displaying an uninitialized white texture.
- Unity EditMode tests passed `5/5`: fragmented reads, malformed/oversized-frame rejection, latest-only overwrite, background receive/clean stop, and disconnect/reconnect with a subsequent valid frame.
- The existing Unity UI validator passed and rendered the robot viewport, connection text, open/stop button, and unchanged recording panel. The Android build succeeded with zero errors; a side-by-side acceptance APK was installed on PICO as `com.galaxis.vrteleop.robotvideoacceptance` because the existing app was signed with an unavailable key from another machine. The original app and its data were not removed.
- ADB reverse was initially verified for both `tcp:63901` and `tcp:63902`. During the startup-crash regression, `63901` was deliberately removed because a host `RoboticsServiceProcess` was listening; the current safe acceptance state restores only read-only video `63902`. The remaining check is a user visual acceptance with the G2 video bridge running.
- Phase 3 conclusion: code, automated tests, UI layout, Android packaging, installation, USB forwarding and PICO 实机画面验收均已完成。机器人视角支持小窗、可拖动大屏、退出大屏和关闭后清黑。
- The first side-by-side PICO launch exposed an Android IL2CPP native crash after about five seconds. Symbolizing the exact build IDs resolved the stack to `UICameraCtrl.Awake -> CustomButton.add_OnChange -> Delegate.Combine`; this was the hidden legacy recording button, not TCP 63902 or the new receiver. The current dashboard already calls `SetRecordingState()` directly, so the unused legacy startup subscription was removed.
- The repaired acceptance APK was rebuilt with zero errors, installed in place, cold-started with TCP 63901 deliberately disconnected, and observed as the resumed foreground activity for more than 15 seconds with no `SIGSEGV`; PICO metrics continued near 90 FPS beyond the original failure point. TCP 63902 alone was then restored. This paragraph records the state before visual acceptance; the later PICO + G2 result below is authoritative.

## Final functional acceptance and freeze（2026-09-03）

- 操作者已在 PICO 中确认 G2 `kHeadColor` 实时画面正常，并确认 PC 浏览器可同时观看同一视频流。
- 当前冻结的视频档位是 GDK 实测可用的 `640×400` 原始 JPEG、约 `30 FPS`；`--fps 60` 表示 PC 读取轮询上限，不会把相机变成 60 FPS。
- PICO 端已经验收小窗、可拖动大屏、退出大屏、关闭视角后立即黑屏和断线自动重连。
- 视频与单关节控制、双臂/双夹爪控制已通过统一会话共同运行，63902 不进入 XRFrame、ControlRouter 或 SafetyGateway。
- Unity 冻结版本为 `com.galaxis.vrteleop` `1.1.7`。冻结前 Unity EditMode 测试 `12/12` 通过；完整 PC 运行时回归 `249` 项通过、`5` 项环境跳过。
- 尚未给出可复现的精确 glass-to-glass 延迟数值和标准化 10 分钟统计报告；这两项保留为性能基准工作，不能把“肉眼流畅”写成已量化的延迟结论。
