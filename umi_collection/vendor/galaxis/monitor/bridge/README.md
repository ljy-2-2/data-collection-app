# 桥接服务(bridge)

部署在**采集主机**上的轻量服务,只读订阅采集功能栈的 ROS 2 话题与视频流,聚合为 JSON 快照并通过 HTTP/WebSocket 提供给监看页面。

- 不修改、不拦截任何采集数据流,采集数据仍由采集功能栈自行落盘
- 无 ROS 环境时可 `--mock` 运行,用于界面开发与演示
- 随采集流程以 `galaxis_monitor` 容器自动启动(见采集仓库 docker-compose)

## 安装与运行

```bash
pip install -e .

# 采集主机(已 source ROS 2 环境)
python -m galaxis_studio_bridge --host 0.0.0.0 --port 8770

# 模拟模式(开发/演示)
python -m galaxis_studio_bridge --mock

# 如需页面内直接控制录制(默认关闭)
STUDIO_ENABLE_CONTROL=1 python -m galaxis_studio_bridge
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STUDIO_HOST` / `STUDIO_PORT` | `0.0.0.0` / `8770` | 监听地址与端口 |
| `STUDIO_MOCK` | `0` | 模拟数据模式 |
| `STUDIO_ENABLE_CONTROL` | `0` | 开启录制控制接口 |
| `STUDIO_VIDEO_HOST` / `STUDIO_VIDEO_PORT` | `127.0.0.1` / `9090` | 视频流服务地址 |
| `STUDIO_PROFILE_PATH` | `/etc/galaxis/collection_profile.yaml` | 采集 profile(自适应相机/编码器) |
| `STUDIO_TOPIC_*` | 见 `config.py` | ROS 话题覆盖 |

## 订阅的 ROS 2 话题

| 话题 | 用途 |
| --- | --- |
| `/task_receiver/episodes` | Episode 列表(1 Hz) |
| `/task_receiver/tasks` | 任务列表(任务种类) |
| `/task_sequence_manager/status` | 任务状态与任务名 |
| `/record_manager/recording` | 录制状态(锁存) |
| `/record_manager/recording_saved` | 回合已安全停止保存 → Episode 计数 +1 |
| `/record_manager/recording_completed` | 最终文件已就绪；兼容计数并按路径去重，供上传器使用 |
| `/record_manager/used` / `free` | 存储统计(页面仅角落小字) |
| `/task_sequence_manager/episode_elapsed_sec` / `subtask_elapsed_sec` | 计时 |
| `/galaxis/gripper/{left,right}/joint_state` | 磁编码器角度(约 100 Hz) |
| `/galaxis/gripper/{left,right}/id` | 编码器设备 ID |
| `/collection_profile` | 编码器启用侧 |
| `/pico/hmd/state`、`/pico/controller/{left,right}/state` | VR 位姿与追踪状态(不订阅电量) |

## 自适应设备数量

相机与编码器数量由采集栈设备设置写出的采集 profile 决定:

```yaml
# /etc/galaxis/collection_profile.yaml(示例)
collection_profile: single-right
active_cameras: [right, center]
active_arms: [right]
```

ROS 模式下桥接还会用实际话题列表探测兜底,确保未启用的相机不会出现在页面。
