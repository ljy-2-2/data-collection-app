# FUSION 连续相机与双臂只读采集

独立用户态服务，通过既有 TLS 端口 `192.168.1.190:57491` 按请求传输三路约 30 Hz MJPEG 和默认 100 Hz 双臂状态。取流、停止、保存均不调用 SSH。原单帧接口保持可用，但同时只允许一项采集；流进行中服务不并行处理其他请求。

## 笔记本命令

```bash
cd ~/桌面/TJ_FX_ROBOT_CONTRL_SDK-master/FusionCameraSDK
python3 examples/record_episode.py --seconds 60 --hz 30 --joint-hz 100 --output ~/桌面/1
```

参数支持 `--host`、`--credentials`、`--seconds 1..600`，图像频率目前限定 30，`--joint-hz {30,100}` 默认 100。600 秒是接口允许时长，不代表已验证 10 分钟。使用已有 `client.json` 与服务证书；不要公开令牌或私钥。

```python
from contextlib import closing
from fusion_camera import FusionStreamClient
from fusion_camera.episode import record_episode

with FusionStreamClient("192.168.1.190") as client:
    directory, validation = record_episode(client, "/home/yeap/桌面/1", seconds=60)

# 或自行处理流；退出 closing / client.close() 均停止此取流。
with FusionStreamClient("192.168.1.190") as client:
    with closing(client.stream(seconds=60, hz=30, joint_hz=100)) as stream:
        for event in stream:
            if event.metadata["type"] == "image":
                rgb = event.frame().to_pil().resize((640, 480))
            elif event.metadata["type"] == "joints":
                q_degrees = event.metadata["position_deg"]  # 左7 + 右7
                stamp = event.metadata["timestamp_ns"]
```

原接口 `FusionCameraClient.read()` / `read_all()` 仍是限速单帧拍摄，不能用来代替此连续接口。

## 持久化与边界

- 控制器程序、已验证的原生用户态 helper、证书和配置位于 `/mnt/fusion-camera`。此前 `/tmp` helper 已复制到这里，运行不再引用 `/tmp`。
- 复用既有 `/etc/init/fusion-camera.conf` 自动启动项，本次未修改 `/etc` 或任何厂家功能。重启后按照该配置自动启动；未实际重启机器人验证。
- 服务空闲时不打开相机或关节共享区。CPU3、nice19，沿用 native helper 内存上限、有界队列、发送超时、实时计数监测和停止清理。
- 目前是**静止验证模式**：观测到任一关节速度超过 1 度/秒、关节错误或状态停更会停止本次采集，不会停止/改变机器人。运动工况尚未验证，此版本不能宣称支持机器人运动中的数据采集。
- 断开客户端会停止采集并释放相机；主动取消不锁死服务。真正的采集故障锁定后需要检查，避免自动反复重试。
- 不依赖 root SSH 密钥或 `/root` 是否持久化。电脑仍须能够访问控制器 IP；本服务不修改电脑/机器人网络配置。
- 不更改内核、模块、GRUB、Xenomai/I-pipe。默认图像/关节接口不运行厂家 SDK；显式 `--grippers` 会在电脑上打开独占厂家 SDK 会话，只发送版本请求和白名单 CAN 读取，不发送运动、使能、零点或参数写入。夹爪 60 秒联合验证未通过，当前属于实验接口，见下文。

## 双臂状态来源

`/dev/FXDcss` 以 `O_RDONLY` 打开并用 `mmap(PROT_READ)` 映射。没有 ioctl、写映射、共享区初始化或读指针修改。

布局证据：FXPSI 的 `InitRtUDP` 初始化 `0x596=1430` 字节、头 `FX` 后接 DCSS；厂家 `libFXShm` 返回映射基址加8；SDK Python定义 DCSS 大小1428、输出偏移760、每臂输出312。部署清单锁定当前厂家二进制、共享内存库、内核配置及 helper 的 SHA256，固件变化时拒绝沿用旧布局。

每次最多4次双读比较并检查左右序号一致，保留读取跨度、重试次数、两臂帧序号。这个检查减少读到更新中间状态的机会，不等于厂家提供了原子快照保证。原始位置/速度保留度、度/秒；episode `tensors` 转成 rad、rad/s。顺序固定左臂1..7、右臂1..7；不包含夹爪。

## Episode 格式

参考 `/home/yeap/agibot_episodes/right_arm/20260725_150042_62c1cdc4` 的目录、时间戳索引和 bundle 字段。这里扩展为三台相机、14关节，以及可选的左右独立夹爪反馈，不冒用 agibot 机器人关节名称或未知相机安装位置。

```text
<episode>/
  meta_info.json
  camera/usb_1_color/usb_1_color.txt   # timestamp_ns frame_index
  camera/usb_1_color/frames/00000000.jpg  # 640×480
  camera/usb_1_color/native/00000000.jpg  # 原始1024×768 MJPEG帧
  camera/usb_2_color/...
  camera/usb_4_color/...
  record/bundles.jsonl
  record/joints.jsonl                # 全部100Hz原始双臂状态
  record/usb_*_color_frames.jsonl     # 原始序号/设备时间字段/电脑收包时间
  record/grippers.jsonl              # 可选，约20Hz双夹爪只读反馈
  record/gripper_read_audit.json      # 可选，全部查询寄存器和异常
  record/gripper_clock_mapping*.json # 可选，电脑/控制器时钟估计
  record/clock_offset.jsonl
  record/image_preprocessing.json
  record/stream_start.json
  record/stream_end.json             # helper统计、前后审计
  record/health.jsonl                # 实时计数
  record/validation.json
```

`bundles.jsonl` 使用 `seq,timestamp_ns,pc_recv_ns,cam_ts,cam_delta_ms,tensors`，增加配对索引和关节序号。`tensors` 包含 `state__joint__name/position/velocity/effort`。有夹爪时额外使用 `state__left_effector__position`、`state__right_effector__position`，均为实际反馈角度 rad，不拼入14维关节向量。不伪造动作或没有采集的传感器数据，不将关节状态冒充 `actions.jsonl`。

## 时间戳和配对

图像是控制器 libuvc 完成帧缓冲的单调时钟；关节是同一控制器读共享区的单调时钟。二者可直接相减，但均不是经过标定的曝光/编码器采样时间。机器人系统时间没有修改。

先配对时间跨度不超过25ms的三路图像，再选其时间中点最近的关节样本作为 bundle 锚；另外 `joint_per_camera` 为每张图保存最近的100Hz关节索引。只有三路都匹配成功才生成一个 bundle，不重用图像、不改写时间戳、不用复制帧凑30Hz。未匹配图像和关节仍完整保存在各自记录中。`cam_delta_ms` 是图像时间减关节时间的有符号差；三路彼此跨度另行记录。实际完整 bundle 频率可能低于30Hz。

`clock_offset.jsonl` 的 robot/pc 对是控制器样本时刻和电脑收到记录的时刻，包含传输延迟；它不是精确时钟校准。跨重启使用不同 boot_id 区分单调时钟。

## 验证

本地测试：`python3 -m unittest discover -s tests -v`。真实连接测试包含3秒联合流、主动断开释放和用户授权的60秒保存。测试结果以 episode 内 `record/validation.json` 和单独报告为准。已通过短测不代表10分钟或运动工况的实时安全保证。

控制器回退备份在 `/mnt/fusion-camera/releases/stream-20260920-v2/previous/server.py`。只在相机服务空闲时回退这一独立服务；不得因此重启厂家机器人进程。

## v2.1 验证结果与夹爪边界

2026-09-20：关节100Hz+三路图像独立60秒验证通过，6000条14维关节、5374张图像、1786个三路bundle。相机约29.99/29.84/29.83Hz，逐图最近关节时间差P95 5.64ms，最大6.92ms。报告见 `REPORT_100HZ_GRIPPERS.md`。这里是静止工况，不能外推到运动和10分钟。

夹爪基于本地DM-J4310手册和既有SDK实现。`--grippers --gripper-hz 20` 是显式实验选项，不默认开启。5秒预检通过；60秒联合测试在约33秒收到未请求的寄存器0x00回包后停止，未通过稳定性验证。已保存661组双夹爪原始反馈，不能将其视为已验证训练真值。来源可能是缓存、其它查询或传输异常，尚未确定；没有清缓存、忽略异常或修改厂家程序绕过。

只读字段：0x50位置角 rad、0x51输出编码器角 rad、0x3D驱动温度、0x3E电机温度。0x51不是速度。左右反馈均超出旧config标定范围，所以 `closure_fraction=null`；左侧温度返回0，保留raw值但标为无效。没有自动调零或更改标定。速度、力矩、物理开口宽度为null。

夹爪20Hz不等于100Hz；双侧及各寄存器顺序读取，各自保留查询/回复时间。成功完整采集后，用采集前后的TLS时钟估计作双锚点映射；若中断只有初始映射，不应据此声称长时精确对齐。均非硬件采样/曝光时间。

未来App必须只有一个厂家SDK/CAN会话所有者。已有控制连接时，复用该连接已有的夹爪反馈，通过 `gripper_source` 注入；不要同时运行独立 `--grippers` 读取器。详见 `APP_INTEGRATION.md`。当前不能宣布完整采集链“全部通过”。
