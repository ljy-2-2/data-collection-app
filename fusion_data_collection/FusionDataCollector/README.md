# 双臂、双夹爪和三路图像采集

> App 集成说明：当前模块已同步新版 MJPEG/YUYV 和夹爪反馈采集逻辑，App 通过同一模块接入遥操作及平台上传。App 使用 `head_color` / `right_color` / `left_color`，不保存 native。完整操作、保存和上传链路以 [App README](../../README.md#tianji-天机) 为准；下文保留独立 CLI 的历史部署说明，不代表当前控制器状态。

启动入口：SDK 根目录 `collect_data.py`。默认输出 `~/桌面/1/日期时间_fusion_随机编号/`。采集器不连接机器人原生 SDK，不发机械臂/夹爪命令；复用正在运行的 Marvin 遥操作反馈。图像与 100 Hz 关节来自独立相机服务的只读接口。

## 当前交付状态

电脑端脚本、可选遥操作反馈出口和相机服务 v2.2 更新文件已提供。有线路由现已修复，控制器和相机 TLS 接口可达；双臂 UDP 两秒只读检查通过。当前 SSH 拒绝现有公钥，**控制器更新尚未部署，实机联合采集未验证**。旧 v2.1 接口有静止速度检查，运动采集会退出，不能直接用于本脚本。

如果 SSH 密钥被拒绝，先在本机交互终端执行 `python3 FusionDataCollector/repair_ssh.py`，仅在 OpenSSH 提示中输入现有 root 密码；脚本修复本机公钥及权限，保留其他公钥并备份。无需向助手提供密码。重启后的 SSH 公钥恢复机制尚待登录后检查。

密钥登录恢复后，在 SDK 根目录执行一次：

```bash
python3 FusionDataCollector/update_camera_service.py
python3 collect_data.py --check
```

更新脚本只更新 `/mnt/fusion-camera/server.py`、`streaming.py`，自动备份，检查相机服务空闲后只重启 `fusion-camera` 服务；不修改启动配置、厂家程序或内核，也不启动图像采集。普通采集不需要 SSH。持久化沿用之前的 `/mnt/fusion-camera` 和已有自动启动项；本次没有执行整机重启验证。

## 使用

1. 在你原来的遥操作启动命令末尾加 `--collection-telemetry`。已经运行的旧进程需要由你按原操作流程退出后重新启动。保留原先所有标定/安装/准备等参数。例如原本使用 `./start-live.sh ...`，改为 `./start-live.sh ... --collection-telemetry`。**该命令本身是原来的实机遥操作，可能使能机器人；采集脚本不会代你启动它。**
2. 另一个电脑终端：

```bash
cd ~/桌面/TJ_FX_ROBOT_CONTRL_SDK-master
python3 collect_data.py
```

3. 先松开 A/B，再按右手 **A** 开始本轮。等终端显示“正在录制”后再进行本轮操作，避免把启动相机的准备时间误认为已录制。
4. 右手 **B** 结束并保存。按住 A 不会重复创建数据集，同时按 A/B 时 B 优先。最长一轮 600 秒，到时自动保存。Ctrl+C 保存已有数据并标为不完整。

**保留现有 B/Y 急停：B 不仅结束采集，还会使原遥操作急停并锁定，下一轮运动需要按原遥操作恢复流程（当前实现需重启遥操作程序）。没有擅自把 B 从急停中移除。** 如果希望 B 仅结束采集，必须另行确定并实现急停键方案。

采集进程不写控制器机器人状态。反馈断开、相机异常、磁盘余量低于 2 GB 或写入队列溢出只停止本采集器，不向机器人发送停止/运动指令。原机器人安全逻辑继续独立运行。

常用选项：

```bash
python3 collect_data.py --max-seconds 60
python3 collect_data.py --task "双臂抓取" --output ~/桌面/1
python3 collect_data.py --retain-native  # 另外保留 1024x768 原始 MJPEG
```

默认保存 640×480 JPEG。相机以原生 1024×768 MJPEG 约 30 Hz 传输，在电脑解码、缩小；并非相机原生 640×480 输出。只检查时 `--check` 不打开 USB 视频流。

## 左右相机命名

编辑 `FusionDataCollector/camera_map.json`。当前安装位置未知，保留 USB 标识，不猜测左右。确认实物后把值分别改成 `hand_left_color`、`hand_right_color` 和 `head_color`（第三路若不是头部可用 `front_color`）。例如仅在确实对应时：

```json
{"usb-1":"hand_left_color","usb-2":"hand_right_color","usb-4":"head_color"}
```

USB 编号表示控制器的物理 USB 路径；换插口后需重新确认。每个 episode 保留实际 mapping 和 USB 标识。名称只允许小写字母、数字和下划线，不能重复。

## 保存结构和语义

```text
episode/
  meta_info.json
  camera/<相机名称>/frames/00000000.jpg
  camera/<相机名称>/<相机名称>.txt
  record/bundles.jsonl
  record/joints.jsonl
  record/left_gripper.jsonl
  record/right_gripper.jsonl
  record/teleop_feedback.jsonl
  record/<相机名称>_frames.jsonl
  record/clock_before.json
  record/clock_after.json
  record/stream_start.json
  record/stream_end.json
  record/validation.json
```

沿用 agibot episode 的 `camera/`、`record/`、`meta_info.json` 和 `state__...` 结构。没有伪造 actions。所有 JSONL 每行一条记录。

- `state__joint__name`：`left_joint_1..7`、`right_joint_1..7`。
- `state__joint__position`：以上顺序的 14 个实际关节角，rad；另有 velocity(rad/s)、effort(Nm)。
- `state__left_arm__position`、`state__right_arm__position`：各 7 个角，便于直接使用。
- `state__left_effector__position`、`state__right_effector__position`：各 1 个实际夹爪电机角，rad，始终保留原始反馈。
- `state__left_effector__opening_angle`、`state__right_effector__opening_angle`：相对配置闭合端点、朝打开方向的电机角，rad。未标定/越界为 `null`，**不裁剪、不用 0 冒充**；它不是独立测量的指爪夹角或毫米开口。
- 夹爪原始文件同时保存 `motor_position_rad`、`opening_angle_rad`、`opening_fraction`、`opening_calibration_valid`、标定端点和原始 motor feedback。
- 三路图像组合的 `cam_ts`、`cam_delta_ms`、`joint_per_camera`、`gripper_delta_ms` 明确保留匹配偏差。
- App 的 Tianji 夹爪目标采用 G2 的记录策略：首次用新鲜、有效的实际位置初始化，之后保留最近控制目标，不随实际位置每帧变化。缓存跨同一次遥操作内的 A/B 轮次保留，重新开始遥操作时重置；不向电机发送额外指令。
- 新 Episode 的 `gripper_action_policy=initial_feedback_then_last_target`。`actions.jsonl` 和 `bundles.jsonl` 的 `gripper_target_sources` 标明每侧值来自 `initial_feedback` 或 `last_controller_target`，以及当前是否存在控制器目标 `controller_target_available`。尚无有效初始化值时不会填零，本轮不能标为完整。原始 `teleop_feedback.jsonl` 中的 `target_rad` 仍保留原值，包括等待时的 `null`。

## 时间戳与质量

关节目标 100 Hz、图像每路约 30 Hz。夹爪使用原遥操作已收到的反馈，有更新才保存；缓存重复不计为新样本，不额外发 CAN 查询来凑 30 Hz。实际独立夹爪频率需实机测量。

图像使用控制器帧完成时间；关节使用控制器只读共享内存采样时间。夹爪使用电脑接收时间，采集前后做时钟测量并拟合偏移/漂移。三相机组合跨度最多 25 ms，夹爪匹配距离最多 50 ms；保留没有匹配成组合的原始图像和所有关节记录。

**这不是硬件曝光同步，传感器/总线内部延迟未标定。** 可用 `validation.json` 查看实际频率、序号缺口、图像和关节偏差、夹爪偏差、缺失样本、标定有效性。`complete` 表示本轮正常收尾且所需字段齐全，`data_validate` 还要求目标频率、无检测到的缺口和开合标定有效；保存成功不等于满足训练数据质量。

静止/遥操作模式分离：旧调用默认仍执行静止速度检查，新采集器显式请求 `teleop_observation`，仅允许只读观察运动中的关节；反馈过期、机械臂故障、原生采集器实时计数监控、CPU/内存约束和审计继续保留。没有实机运动/60秒/10分钟验证，不能保证三路相机与遥操作并行时不会影响实时性；应先短时验收现有监控指标。

依赖已有 `FusionCameraSDK`、Python 3（电脑）、Pillow。无需 USB/IP、内核模块或每次 SSH 登录。保持当前能到达 `192.168.1.190` 的网络配置；本脚本没有增加自动发现或自动配置网络功能。

## 目标指令的时间对齐

结束录制时，先把目标指令的 PC 单调时钟映射到控制器时钟，再针对每个 bundle 的关节采样时刻，比较前后两条指令，选择绝对时间差最小的一条（允许选择稍晚的指令）。等距时选较早的一条；完全同一时间戳的重复记录选最后一条。最大绝对时间差沿用 150 ms，超出后保留缺失，不补造目标。相机与关节、夹爪反馈原本也使用最近时间匹配。

`action_sample_index` 指向原始 `record/actions.jsonl`；`action_timestamp_ns` 保留所选指令的映射时间；`action_delta_ms = (指令时间 - bundle 时间)`，正值表示指令晚于该帧，负值表示早于该帧。`action_alignment_kind` 和 `meta_info.json` 的 `action_alignment` 标记最近时间匹配。这是离线时间匹配，不表示该指令在帧时刻已经生效。原始指令与时间戳保持保留，计算目标轨迹导数应使用原始指令序列的时间。

此规则只影响更新后新录制的数据；已有录制文件不会自动重排。
