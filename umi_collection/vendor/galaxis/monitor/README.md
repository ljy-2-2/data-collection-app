# Galaxis Monitor(采集监看)

数据采集功能栈的实时监看页,替代原浏览器监看界面。由两部分组成:

- `bridge/`:桥接服务,只读订阅采集栈 ROS 2 话题与视频流,聚合为 HTTP/WS 状态快照
- `webui/`:监看页面(零构建依赖,深色工业风自研设计)

## 运行方式

**随采集流程自动启动(推荐)**:执行 `./tools/galaxis_collection.py sample --task "任务名"` 后,
采集入口会启动 `galaxis_monitor` 容器并自动打开 `http://localhost:8770/`。

**手动启动(开发/调试)**:

```bash
# 本机模拟模式(无 ROS 环境预览界面)
cd monitor/bridge
python3 -m galaxis_studio_bridge --mock --host 0.0.0.0 --port 8770

# 容器模式(采集主机,真实 ROS 数据)
docker compose -f ../docker-compose.yml up -d galaxis_monitor
```

## 功能

- 相机实时画面(数量按设备设置自适应,含 FPS 徽标、断流重连、单路全屏)
- 采集统计:任务种类、回合命名、Episode 累计计数、录制状态与计时
- 磁编码器:启用侧实时角度、变化曲线与消息帧率
- VR 状态:头显/左右手柄的帧率、位置与姿态
- 事件日志与存储概况(底部小字)

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STUDIO_HOST` / `STUDIO_PORT` | `0.0.0.0` / `8770` | 监听地址与端口 |
| `STUDIO_MOCK` | `0` | 模拟数据模式 |
| `STUDIO_VIDEO_HOST` / `STUDIO_VIDEO_PORT` | `127.0.0.1` / `9090` | 视频流服务地址 |
| `STUDIO_PROFILE_PATH` | `/etc/galaxis/collection_profile.yaml` | 设备设置写出的采集 profile |

> `STUDIO_PROFILE_PATH` 指向采集栈设备设置写出的系统文件路径,该路径由采集栈维护,桥接只读取。
