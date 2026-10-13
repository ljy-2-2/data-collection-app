"""Galaxis Studio 桥接服务配置。

所有配置均可通过环境变量覆盖,默认值与现有采集功能栈
(Galaxis-Data-Collection)保持一致,开箱即用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class Settings:
    """运行时配置。"""

    # 桥接服务监听
    host: str = field(default_factory=lambda: os.environ.get("STUDIO_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("STUDIO_PORT", 8770))

    # 模拟模式:无 ROS 环境时用于开发与界面预览
    mock: bool = field(default_factory=lambda: _env_bool("STUDIO_MOCK", False))

    # 采集控制接口(调用 ROS 服务),默认关闭;开启后页面出现开始/停止录制按钮
    enable_control: bool = field(default_factory=lambda: _env_bool("STUDIO_ENABLE_CONTROL", False))

    # 视频流服务地址(采集栈 web_video_server 端口,与 .env 的
    # WEB_VIDEO_SERVER_PORT 一致;9090 已被 galaxis 容器内 rosbridge 占用)
    video_host: str = field(default_factory=lambda: os.environ.get("STUDIO_VIDEO_HOST", "127.0.0.1"))
    video_port: int = field(default_factory=lambda: _env_int("STUDIO_VIDEO_PORT", 9091))

    # 采集 profile(由采集栈设备设置写入),用于自适应相机/编码器数量
    profile_path: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_PROFILE_PATH", "/etc/galaxis/collection_profile.yaml"
        )
    )

    # ROS 2 话题(与采集栈既有话题一致,不可随意修改;仅用于只读订阅)
    topic_episodes: str = field(
        default_factory=lambda: os.environ.get("STUDIO_TOPIC_EPISODES", "/task_receiver/episodes")
    )
    topic_recording: str = field(
        default_factory=lambda: os.environ.get("STUDIO_TOPIC_RECORDING", "/record_manager/recording")
    )
    topic_recording_completed: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_RECORDING_COMPLETED", "/record_manager/recording_completed"
        )
    )
    topic_recording_saved: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_RECORDING_SAVED", "/record_manager/recording_saved"
        )
    )
    topic_storage_used: str = field(
        default_factory=lambda: os.environ.get("STUDIO_TOPIC_STORAGE_USED", "/record_manager/used")
    )
    topic_storage_free: str = field(
        default_factory=lambda: os.environ.get("STUDIO_TOPIC_STORAGE_FREE", "/record_manager/free")
    )
    topic_status: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_STATUS", "/task_sequence_manager/status"
        )
    )
    topic_metadata: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_METADATA", "/metadata_handler/metadata_json"
        )
    )
    topic_episode_elapsed: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_EPISODE_ELAPSED", "/task_sequence_manager/episode_elapsed_sec"
        )
    )
    topic_recording_elapsed: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_RECORDING_ELAPSED", "/record_manager/recording_elapsed_sec"
        )
    )
    topic_subtask_elapsed: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_SUBTASK_ELAPSED", "/task_sequence_manager/subtask_elapsed_sec"
        )
    )

    # 任务列表(任务种类)与采集 profile 广播
    topic_tasks: str = field(
        default_factory=lambda: os.environ.get("STUDIO_TOPIC_TASKS", "/task_receiver/tasks")
    )
    topic_collection_profile: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_COLLECTION_PROFILE", "/collection_profile"
        )
    )

    # 左右手磁编码器(采集栈编码器节点,约 100 Hz;话题名为采集栈既有接口)
    topic_encoder_left_state: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_ENCODER_LEFT", "/galaxis/gripper/left/joint_state"
        )
    )
    topic_encoder_right_state: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_ENCODER_RIGHT", "/galaxis/gripper/right/joint_state"
        )
    )
    topic_encoder_left_id: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_ENCODER_LEFT_ID", "/galaxis/gripper/left/id"
        )
    )
    topic_encoder_right_id: str = field(
        default_factory=lambda: os.environ.get(
            "STUDIO_TOPIC_ENCODER_RIGHT_ID", "/galaxis/gripper/right/id"
        )
    )

    # VR(PICO)状态话题(位姿与追踪状态;不订阅电量)
    topic_hmd_state: str = field(
        default_factory=lambda: os.environ.get("STUDIO_TOPIC_HMD", "/pico/hmd/state")
    )
    topic_left_state: str = field(
        default_factory=lambda: os.environ.get("STUDIO_TOPIC_LEFT", "/pico/controller/left/state")
    )
    topic_right_state: str = field(
        default_factory=lambda: os.environ.get("STUDIO_TOPIC_RIGHT", "/pico/controller/right/state")
    )

    # 相机话题名(按 key 固定;实际启用哪几路由采集 profile / ROS 话题探测决定)
    camera_topics: dict[str, str] = field(
        default_factory=lambda: {
            "left": "/left_camera/image_raw",
            "right": "/right_camera/image_raw",
            "center": "/center_camera/image_raw",
        }
    )
    camera_labels: dict[str, str] = field(
        default_factory=lambda: {"left": "LEFT", "right": "RIGHT", "center": "CENTER"}
    )
    camera_order: tuple[str, ...] = field(default_factory=lambda: ("left", "right", "center"))


def stream_url_for(settings: Settings, topic: str) -> str:
    """构造 web_video_server MJPEG 流地址。

    与现有采集栈一致:压缩话题用 ros_compressed 类型 + sensor_data QoS。
    """
    params = f"topic={topic}&type=ros_compressed&qos_profile=sensor_data"
    return f"http://{settings.video_host}:{settings.video_port}/stream?{params}"


def preview_stream_url_for(
    settings: Settings,
    topic: str,
    *,
    width: int = 0,
    height: int = 0,
    quality: int = 90,
) -> str:
    """Default to the recorded JPEG stream, with no lossy resize/re-encode."""
    if width <= 0 and height <= 0:
        return stream_url_for(settings, topic)
    params = (
        f"topic={topic}&type=mjpeg&qos_profile=sensor_data"
        f"&width={width}&height={height}&quality={quality}"
    )
    return f"http://{settings.video_host}:{settings.video_port}/stream?{params}"
