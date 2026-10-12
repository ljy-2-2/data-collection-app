# Galaxis 数据采集系统

本仓库是 Galaxis 双手机器人数据采集的操作端，负责设备配置、PICO 4 Ultra 遥操作接入、ROS 2 启动、Episode 录制以及 LeRobot 3.0 数据转换。当前头显端唯一适配基准是同级目录 `../Galaxis-VR-Teleop`；采集端不依赖头显工程源码，也不需要额外的 PC 中转服务。

## 系统组成

| 路径/服务 | 职责 |
| --- | --- |
| `tools/galaxis_collection.py` | 操作员主入口：设备检查、启动服务、连接 PICO、创建任务并监看采集 |
| `galaxis_pico/` | 当前 Galaxis TCP 协议、ROS 消息、PICO 位姿与按键桥接 |
| `galaxis_bringup/` | 相机、编码器、脚踏板、PICO bridge 与采集节点编排 |
| `galaxis-core/` | 门禁、rosbag、Episode 状态、上传与任务状态机 |
| `monitor/` | 采集监看桥接服务与页面(Galaxis Monitor,采集时自动打开) |
| `rebake/` | rosbag 解码、同步和 LeRobot 3.0 聚合 |
| `firmware/ESP32C6_MT7V0/` | MT6835 磁编码器固件 |

运行数据默认写到 `.env` 的 `DATA_MOUNT_PATH`，回合文件在本地永久保留。停止服务
不会删除 rosbag 或任务数据。

## 依赖与首次配置

宿主机需要 Docker、Docker Compose、Make、ADB；设备规则脚本还会使用系统 Python 和 udev。

```bash
git submodule update --init --recursive
cp .env.example .env
```

在 `.env` 中选择设备：

```dotenv
ROBOT_VARIANT=stationary
# ROBOT_VARIANT=portable
```

- `stationary`：左右手相机、RealSense 中心相机、双编码器、脚踏板、PICO。
- `portable`：左右手相机、USB 中心相机、双编码器、PICO 按键与夹爪组合控制。

所有采集参数统一放在 `galaxis_bringup/config/galaxis_config.yaml`（含
`robot_config` / `galaxis_devices` / `recording_gate` / `upload_targets` 四个段落），
修改后重新执行采集即可生效。启动时叠加 `/etc/galaxis/collection_profile.yaml` 的
设备设置（相机模式、激活手臂）生成 `galaxis_bringup/config/_runtime/single/`，
不要直接编辑生成文件；需要手动重建时可运行 `make build-config`。

## 编码器与相机设置

每块 ESP32-C6-WROOM-1 烧录 `firmware/ESP32C6_MT7V0/ESP32C6_MT7V0.ino`，并将 `DEVICE_ID` 设置为 `L###` 或 `R###`。MT6835 通过 SPI mode 3 连接：GPIO23=MISO、GPIO22=MOSI、GPIO21=SCK、GPIO20=CSN；完整接线与供电要求见固件头部注释。

更换或重新烧录编码器主控后，应重新运行下方设备设置。采集配置固定读取设备设置生成的 `/dev/galaxis_left_esp32c6` 与 `/dev/galaxis_right_esp32c6`，不再依赖旧开发板的 `/dev/serial/by-id` 名称。固件对上位机继续使用 115200 baud 的 `DEVICE_ID,radians` 文本协议，因此 ROS 话题和数据格式不变。

连接全部相机和编码器后运行：

```bash
sudo -E bash tools/galaxis_udev_setup.sh
# 便携设备：
sudo -E bash tools/galaxis_udev_setup.sh --variant portable
```

默认会并行探测所有 USB 串口，仅接纳实际输出 `L###/R###,radians` 协议的编码器，因此同时兼容 ESP32-C6 原生 USB 与 CH340/CP210x 等 USB-UART 芯片。如需限定型号，可显式传入 `--esp-vid 1a86 --esp-pid 7523`（CH340）或 `--esp-vid 10c4 --esp-pid ea60`（CP210x）。可运行 `python3 -m serial.tools.list_ports -v` 查看实际 VID/PID。没有唯一序列号的转换器会按物理 USB 端口生成稳定左右规则，因此设置完成后不要互换两个编码器所插的 USB 插口。探测失败时，终端和弹窗会逐个显示串口的拒绝原因。

在界面中确认左右相机、左右编码器和中心相机映射。每个相机区域都有独立的采集模式下拉框，选项来自该物理设备通过 V4L2 实际公布的“分辨率 + 帧率 + 像素格式”组合；LEFT、RIGHT、CENTER 可以分别设置。portable 的 CENTER 卡片还可选择“USB 中心相机”或“G2 头部彩色相机（GDK）”，右手相机仍独立选择，不受中心来源影响。闭合每个夹爪后采集最小值，再应用配置。脚本会写入：

Setup 的页面、白色卡片、蓝色主操作、状态色和深色实时曲线面板与 `Galaxis-VR-Teleop` 的操作界面保持同一套视觉语言。相机设备、采集模式和亮度都有独立标题，可以在每个相机卡片中分别修改。

Setup 使用与头显端相同的“Header → 顶部导航 → 单页卡片 → 底部操作栏”结构，分为相机、执行器、检查与应用三页，不再把所有预览和曲线堆在一个长滚动页中。

### Setup 与实时监看稳定性（已实现，2026-08-17）

- 目标：Setup 退出、刷新或切换相机模式时不再发生 OpenCV `cvarrToMat` 异常；采集页的相机和传感器持续更新。
- 实现：`VideoCapture.read()` 与 `release()` 使用同一互斥锁；每台 Setup 相机最多保留一帧待显示图像，预览上限 15 FPS。
- 实时页：ROS 与 rosbag 仍使用 Setup 选择的原始分辨率和帧率；浏览器预览独立限定为 `640x480` / JPEG 70，不会因选择高分辨率而产生无上限网络与解码压力。
- 传感器页：高频 PICO 与编码器数据仅对网页显示进行 20 Hz 传输节流和 10 Hz 合并渲染，不改变 ROS 发布或录制频率。
- 验收：当前 `single-right` 实机的 RIGHT/CENTER `camera_info` 和 `image_raw/compressed` 均稳定在约 30 FPS，右夹爪上游数据稳定在约 100 Hz。

### 可选 G2 中心相机（已实现，2026-08-18）

- 目标：采集时可把 `/home/ljy/桌面/galaxis/app/gdk` 提供的 G2 `head_color` 作为 CENTER，同时保留原 USB 中心相机作为默认选项。
- 实现：Setup 将来源保存到 `camera_sources.center`。选择 G2 时，运行时只移除中心 USB 节点和亮度控制器，RIGHT/LEFT USB 相机、编码器、PICO、录包话题与中心翻转节点保持原流程。
- 数据路径：主机 CPython 3.10 通过 GDK 读取 `640x400` JPEG 和标定内参，经仅监听 `127.0.0.1:64002` 的有界协议送到 ROS 节点，再发布既有 `/center_camera/image_raw`、`/center_camera/image_raw/compressed` 与 `/center_camera/camera_info`。
- 安全与失败行为：G2 与采集电脑的系统时钟可能存在固定偏差，因此以 G2 图像时间戳持续递增判断实时性，而不与电脑墙上时间直接比较；时间戳不再推进超过 3 秒时会结束桥接。G2 未开机、机器人网络断开或 `head_color` 停止发布时，采集在创建 Episode 前失败并给出错误，不会录入固定缓存图像。退出采集会结束该 GDK 进程；切回 USB 后不会启动它。
- 验收：USB/G2 两种配置都固定生成 CENTER `640x480 @ 30 FPS`；G2 配置中保留所选右手相机，仅将 `center_camera/usb_cam` 替换为 `center_camera/g2_bridge`。

CENTER 的保存与最终 ROS 输出固定为 `640x480 @ 30 FPS`。USB 来源会固定请求 `pixel_format: yuyv2rgb`，Setup 中 CENTER 的采集模式不可修改；RIGHT/LEFT 仍可独立选择。G2 的 GDK 输入本身是 `640x400 JPEG`，桥接节点解码后缩放为 `640x480` 并同步缩放相机内参，因此 G2 不会被错误描述为硬件原生 YUYV。

- `/etc/udev/rules.d/99-galaxis.rules`
- `/etc/galaxis/encoder_limits.yaml`
- `/etc/galaxis/collection_profile.yaml`

采集模式会随 profile 保存，例如：

```yaml
cameras:
  right:
    image_width: 1280
    image_height: 720
    framerate: 60.0
    pixel_format: "mjpeg2rgb"
  center:
    image_width: 640
    image_height: 480
    framerate: 30.0
    pixel_format: "yuyv2rgb"
```

选择 G2 中心相机时会额外保存：

```yaml
camera_sources:
  center: "g2_head_color"
```

运行时配置生成器会把这些值覆盖到每个当前启用的 `<side>_camera/usb_cam`，未启用的相机仍按采集 profile 完整移除。修改模式后应通过 `--device-setup` 重新保存并刷新运行时。

设备重新插拔后应出现 `/dev/galaxis_left_camera`、`/dev/galaxis_right_camera`、`/dev/galaxis_left_esp32c6` 和 `/dev/galaxis_right_esp32c6`；portable 还应有 `/dev/galaxis_center_camera`。

## 当前 Galaxis VR 接口

`Galaxis-VR-Teleop` 中的客户端主动连接采集电脑 TCP `63901`。当前协议包固定为：

```text
0x3F | command(1) | payload_length(uint32 LE) | UTF-8 payload
     | unix_time_ms(int64 LE) | 0xA5
```

采集端回包使用 `0xCF` 包头，其余布局相同。当前使用的命令包括连接 `0x19`、追踪函数 `0x6D` 和服务端函数 `0x5F`。追踪函数信封为：

```json
{"functionName":"Tracking","value":"{...tracking json...}"}
```

采集桥直接解析当前字段：

- `timeStampNs`
- `Head.pose`
- `Controller.left/right.pose`
- `axisX`、`axisY`、`axisClick`
- `grip`、`trigger`
- `primaryButton`、`secondaryButton`、`menuButton`

位姿字符串顺序为 `x,y,z,qx,qy,qz,qw`。边界处统一转换为 ROS REP-103：X 前、Y 左、Z 上。左右控制器分别发布 state、Joy、BatteryState 和 TF；桥接节点还根据手柄安装变换发布 `left_hand_root` 与 `right_hand_root`。

新版客户端启动后默认启用头显、控制器、手部追踪和数据发送，因此采集端只维护当前四字节长度协议，不保留旧 APK 的异常零长度包兼容。

### USB ADB 连接

启用 PICO USB 调试并授权电脑；只有连接多台 Android 设备时才需要在 `.env` 设置 `PICO_ADB_SERIAL`。启动采集服务后执行：

```bash
make pico-adb
```

该命令建立 `adb reverse tcp:63901 tcp:63901`。PICO 或 ADB 重启后需要重建。日常使用 `galaxis_collection.py` 时会自动发现唯一已授权设备并恢复 tunnel。

当开始采集、建立 PICO 连接或断开连接时，主程序会按可执行文件的完整路径精确检查并结束已废弃的 `/opt/apps/roboticsservice/RoboticsServiceProcess`：先发送 `SIGTERM`，3 秒后仍未退出才发送 `SIGKILL`。不会按进程名或端口模糊批量结束其他程序；当前 ROS bridge 和头显内的 Galaxis VR 应用不在该清理范围内。

### Wi-Fi 或 USB 网卡连接

Wi-Fi 模式在头显应用中填写采集电脑 IPv4 地址。若使用直连 USB 网卡，可运行：

```bash
sudo ./tools/galaxis_pico_dhcp_setup.sh setup enxAABBCCDDEEFF \
  --pico-mac AA:BB:CC:DD:EE:FF --pico-ip 10.0.0.10
```

可用 `restart`、`status`、`remove` 子命令维护或恢复系统网络配置。

## 构建与启动

```bash
make build-config
make docker
docker compose up -d
```

常用单独构建命令：

```bash
make docker-galaxis
make docker-galaxis-core
docker compose config -q
```

`config-init` 在服务启动前生成运行时配置；`galaxis` 启动硬件和 PICO bridge；`galaxis_core` 负责采集状态和 rosbag；`web_video_server` 提供相机预览。

## 日常数据采集

推荐只使用统一入口：

```bash
./tools/galaxis_collection.py sample --task "Galaxis"
```

首次或硬件映射变化时：

```bash
./tools/galaxis_collection.py sample --task "Galaxis" --device-setup
```

默认保持 MCAP rosbag 格式。如需保存为压缩 HDF5，在命令末尾加上：

```bash
./tools/galaxis_collection.py sample --task "Galaxis" --device-setup --format hdf5
```

可选值为 `rosbag` 和 `hdf5`；不传 `--format` 时仍为 `rosbag`。HDF5 模式
仍由 rosbag2/MCAP 链路安全采集，停止后转换为紧凑 HDF5 v3：非图像消息保留
原始 CDR，JPEG 相机消息转换为 H.264 MP4（CRF 14、medium、640×480、30 FPS），
HDF5 保留全局消息顺序、原始图像时间戳和视频帧编号。每个回合最终包含
`<回合>.h5`、每路相机 MP4、`manifest.json` 和 `meta.json`。转换器逐条验证
非图像 CDR、全局时间线、视频帧数/尺寸/FPS/SHA-256 后才删除 staging；失败时
完整临时 MCAP 会保留供恢复。H.264 会轻微改变图像像素，现有 Rebake/LeRobot
流程仍只读取 rosbag，尚未直接读取 HDF5 v3。

按 B/Y 后 MCAP 会先完整落盘并立即进入暂停，HDF5 压缩与校验在单个后台
队列中串行进行，不会阻塞下一回合。暂停后按 `Ctrl+C` 会立即取消尚未开始的转换、
在数秒内结束当前转换，然后像 rosbag 模式一样停止 ROS、监看、相机和 PICO
服务。每个回合的 `meta.json` 在进入后台队列前就已落盘；被取消转换的完整 MCAP
会保留，下一次以 `--format hdf5` 启动时自动扫描并继续转换。文件管理器默认隐藏的
`.rosbag_staging` 目录表示“数据已安全录制、HDF5 尚未最终完成”，不是空回合。
只有最终 HDF5、全部 MP4、`manifest.json` 全量校验通过且 `meta.json` 存在后，
staging 才会删除。

脚本会检查配置、增量构建、启动服务、连接 PICO、等待相机/编码器/追踪门禁，并打开任务监看页面。任务按键为：

- 右 A 或左 X：开始当前 Episode。
- 右 B 或左 Y：结束并保存当前 Episode，随后回到暂停。
- `Ctrl+C`：结束监看并按当前选项停止采集服务；数据不会被删除。

跳过 PICO 的 `--skip-vr` 仅用于诊断，不应用于正式采集。

### 回合文件命名与保留

`--task` 只是任务大类：不存在时会自动创建。每个回合保存在
`${DATA_MOUNT_PATH}/rosbags/<任务名>/` 下；HDF5 保存在
`${DATA_MOUNT_PATH}/hdf5/<任务名>/` 下。目录名为 `<任务名>_<回合序号>_<时间戳>`
（如 `Galaxis_001_20260819-123456`）。回合序号按磁盘已有回合自动累加，
无论重复采集多少次都不会覆盖或清理历史回合；上传后本地文件同样保留。

## ROS 数据契约

关键实时接口：

```text
/pico/hmd/state
/pico/controller/left/state
/pico/controller/right/state
/pico/controller/left/joy
/pico/controller/right/joy
/pico/controller/left/battery
/pico/controller/right/battery
/tf
/tf_static
/joint_states
/left_camera/image_raw
/right_camera/image_raw
/center_camera/image_raw
```

Joy 轴约定：0/1 为摇杆，2 为 Trigger，3 为 Grip；按键映射由 `galaxis_pico_bridge/pico_bridge_node.py` 定义。每一帧 state 保存设备时间、PC 单调时钟、递增序号和设备标识，便于跨 topic 对齐。时间戳倒退的追踪帧会在同一连接中被丢弃。

验证实时数据：

```bash
docker exec galaxis bash -lc 'source /root/ros_entrypoint.sh && ros2 topic echo --once /pico/controller/left/state'
docker exec galaxis bash -lc 'source /root/ros_entrypoint.sh && ros2 topic echo --once /pico/controller/left/joy'
docker exec galaxis bash -lc 'source /root/ros_entrypoint.sh && ros2 run tf2_ros tf2_echo pico_origin left_hand_root'
```

`recording_gate` 综合 tracked/valid、TF 新鲜度、相机、编码器和任务状态：`gate_level=0` 可开始，`1` 暂时禁止，`2` 硬停止。

## 数据转换

将已有 rosbag 转换并按任务聚合为 LeRobot 3.0：

```bash
./tools/rebake_lerobot_v30.py --jobs 1
```

流程会按采集 profile 分组，解析 rosbag、补全 TF、同步图像/动作/状态，生成 Episode 中间结果，再聚合 Parquet、任务元数据和 H.264 视频。不同相机数量或关节维度的 profile 不会被静默混合。

检查最终结果时确认：

- `meta/episodes` 数量等于成功采集的 Episode 数。
- 每个 Episode 的索引区间等于帧数。
- 每路视频时间范围覆盖对应 Episode。
- 输出文件属于当前宿主用户。

## URDF 与标定

`galaxis_description/urdf/galaxis_hand.urdf.xacro` 会影响 `/tf`、`/tf_static` 和 Rebake 导出的手根、相机、指尖位姿。STL 只提供显示/碰撞外形，不包含运行时追踪、编码器零位或传动关系。

更换夹爪或安装方式后必须重新测量：

1. `hand_root` 到两根手指转轴的 XYZ、RPY 与轴方向。
2. 手指局部坐标到真实夹取接触点的 XYZ、RPY。
3. `left/right_hand_root` 到对应 PICO 控制器追踪参考的 XYZ、RPY。
4. 两指传动比例、机械零位、主动手指安全角度范围。
5. 编码器闭合零位与旋转方向。

长度统一使用米，角度统一使用弧度。SolidWorks 中的毫米除以 1000，角度乘以 `pi/180`；安装姿态必须以软件追踪参考坐标为准，不能只按控制器外壳方向估计。

## 故障排查

- PICO 已连接但无数据：检查 `docker compose logs galaxis`，确认 `:63901` 正在监听；USB 模式重新运行 `make pico-adb`。
- 日志出现 `Address already in use`：说明 Galaxis bridge 没有获得 `63901`。运行 `ss -lntp | grep :63901` 找到占用者并退出旧服务；采集入口现在会读取 bridge 自己的 `listening/listen_error` 诊断并直接报告进程，不再把任意监听者误认为 bridge。
- 控制器不动：查看 state 的 `seq`、`device_time_ns` 和 `tracked/valid` 是否持续更新，再检查 TF。
- 相机无图：重新插拔设备，检查 `/dev/galaxis_*_camera`，重新运行设备设置并查看 USB 带宽。
- 无法开始 Episode：查看 `recording_gate/gate_level`、`task_sequence_manager/status` 与 `docker compose logs galaxis_core`。
- 修改配置未生效：运行 `make build-config`，再重建相关容器。
- Compose 语法检查：运行 `docker compose config -q`。

## 开发与验证

```bash
pytest -q \
  galaxis_pico/galaxis_pico_bridge/test/test_transport_galaxis_tcp.py \
  galaxis_pico/galaxis_pico_bridge/test/test_pico_bridge_node.py \
  tools/test_galaxis_collection.py
python -m compileall -q galaxis_pico tools galaxis_bringup
docker compose config -q
```

`galaxis-core` 子模块内的版权、flake8 和 pep257 测试需要安装对应 `ament_*` 包。修改 `../Galaxis-VR-Teleop` 的网络包格式、Tracking JSON 或默认发送行为时，必须同步更新 `galaxis_tcp.py`、协议测试和本节接口说明。

## License

见 [LICENSE](LICENSE)。
