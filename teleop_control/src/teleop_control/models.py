from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DeviceStatus(BaseModel):
    id: Literal["robot", "xr", "headset"]
    name: str
    status: Literal["checking", "connected", "offline", "warning"]
    detail: str = ""


class SessionStatus(BaseModel):
    state: Literal["idle", "running", "unknown"]
    session_id: str | None = None
    started_at: datetime | None = None


class MotionStatus(BaseModel):
    state: Literal["idle", "moving", "unknown"]
    command_id: str | None = None
    completed_command_id: str | None = None
    failed_command_id: str | None = None


class StatusResponse(BaseModel):
    checked_at: datetime
    devices: list[DeviceStatus]
    session: SessionStatus
    motion: MotionStatus


UmiState = Literal["idle", "starting", "ready", "recording", "stopping", "error"]


class UmiStartRequest(BaseModel):
    task: str = Field(default="Galaxis", min_length=1, max_length=120)
    collection_profile: Literal["dual", "single-left", "single-right"] = "dual"


class UmiDeviceConfigureRequest(BaseModel):
    collection_profile: Literal["dual", "single-left", "single-right"] = "dual"
    camera_roles: dict[Literal["left", "right", "center"], str]
    capture_encoder_zero: bool = False
    encoder_minima: dict[Literal["left", "right"], float] = Field(
        default_factory=dict,
        description="闭合最小位置的传感器原始弧度值；界面显示度数，不改变标定单位",
    )


class UmiCameraSelectionRequest(BaseModel):
    role: Literal["left", "center", "right"]
    device: str = Field(pattern=r"^/dev/video\d+$", max_length=40)


class CameraResolutionRequest(BaseModel):
    mode_id: str = Field(min_length=1, max_length=80, strict=True)


class UmiStartResponse(BaseModel):
    session_id: str
    state: UmiState
    started_at: datetime


class UmiStatus(BaseModel):
    state: UmiState = "idle"
    message: str = ""
    session_id: str | None = None
    task: str | None = None
    record_format: Literal["rosbag", "hdf5"] = "rosbag"
    collection_profile: Literal["dual", "single-left", "single-right"] = "dual"
    started_at: datetime | None = None
    monitor_connected: bool = False
    recording: bool = False
    episodes_completed: int = 0
    pending_processing: int = 0
    failed_processing: int = 0
    pending_imports: int = 0
    imported_episodes: int = 0
    monitor: dict = Field(default_factory=dict)
    log_tail: list[str] = Field(default_factory=list)


CameraId = Literal[
    "head_color",
    "head_stereo_left",
    "head_stereo_right",
    "head_back_fisheye",
    "head_left_fisheye",
    "head_right_fisheye",
    "hand_left_color",
    "hand_right_color",
    "head_depth",
    "hand_left_depth",
    "hand_right_depth",
]
ExtraCameraId = Literal["head_depth", "hand_left_depth", "hand_right_depth"]


class CameraPreview(BaseModel):
    id: CameraId
    available: bool
    updated_at: datetime | None = None


class PreviewStatus(BaseModel):
    available: bool
    recording: bool = False
    updated_at: datetime | None = None
    joint_positions: dict[str, float] = Field(default_factory=dict)
    cameras: list[CameraPreview]


class StartSessionRequest(BaseModel):
    robot_model: Literal["agibot-g2"]
    mode: Literal["arm"]
    # Omitted by older clients: retain the launcher's configured task label.
    task: str | None = Field(default=None, min_length=1, max_length=120)
    scale: float = Field(default=0.5, ge=0.1, le=1.0)
    max_joint_vel: float = Field(default=0.5, ge=0.1, le=1.0)
    extra_cameras: list[ExtraCameraId] = Field(default_factory=list, max_length=3)

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("任务名称不能为空或包含控制字符")
        return value


class StartSessionResponse(BaseModel):
    session_id: str
    started_at: datetime
    extra_cameras: list[ExtraCameraId] = Field(default_factory=list)


class StopSessionResponse(BaseModel):
    stopped_at: datetime


class MoveRequest(BaseModel):
    command_id: str = Field(min_length=1, max_length=128)
    robot_model: Literal["agibot-g2"]
    mode: Literal["arm"]
    joint_names: list[str] = Field(min_length=1, max_length=24)
    joint_positions: list[float] = Field(min_length=1, max_length=24)
    preset: str | None = None
    changed_joint: str | None = None


class MoveResponse(BaseModel):
    accepted: bool
    motion_id: str | None = None


DaggerState = Literal[
    "idle",
    "starting",
    "inference",
    "teleop",
    "switching",
    "stopping",
    "finished",
    "saving",
    "error",
]


class DaggerStartRequest(BaseModel):
    checkpoint: str = Field(min_length=1, max_length=1024)
    task: str = Field(default="DAgger 纠正采集", min_length=1, max_length=200)


class DaggerStartResponse(BaseModel):
    session_id: str
    state: DaggerState


class DaggerCommandRequest(BaseModel):
    command: Literal["takeover", "resume", "end"]
    save_mode: Literal["full", "corrections"] | None = None
    pre_seconds: float | None = Field(default=None, ge=0, le=30)
    post_seconds: float | None = Field(default=None, ge=0, le=30)


class DaggerStatus(BaseModel):
    state: DaggerState = "idle"
    message: str = ""
    session_id: str | None = None
    session_dir: str | None = None
    saved_count: int | None = None
    log_tail: list[str] = Field(default_factory=list)


HilSerlState = Literal[
    "idle",
    "validating",
    "converting",
    "starting_learner",
    "starting_actor",
    "resetting",
    "waiting_scene",
    "running",
    "training_bc",
    "stopping",
    "finished",
    "error",
]


class HilSerlStartRequest(BaseModel):
    mode: Literal["fresh", "resume", "bc"]
    checkpoint_path: str | None = Field(default=None, max_length=1024)
    demo_path: str | None = Field(default=None, max_length=1024)
    replay_path: str | None = Field(default=None, max_length=1024)
    use_bc: bool = True
    bc_checkpoint_path: str | None = Field(default=None, max_length=1024)
    learner_ip: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    task_name: str = Field(default="agibot_can_pick", min_length=1, max_length=80)
    train_steps: int = Field(default=20_000, ge=100, le=10_000_000)
    startup_timeout: int = Field(default=300, ge=30, le=3600)


class HilSerlProgress(BaseModel):
    phase: str = ""
    current: int = 0
    total: int = 0
    control_source: str | None = None
    pico_valid: bool | None = None
    right_grip: float | None = None
    arm_action_norm: float | None = None
    teleop_servo_hz: float | None = None


class HilSerlStatus(BaseModel):
    state: HilSerlState = "idle"
    mode: Literal["fresh", "resume", "bc"] | None = None
    message: str = ""
    session_id: str | None = None
    checkpoint_path: str | None = None
    demo_path: str | None = None
    started_at: datetime | None = None
    scene_waiting: bool = False
    actor: HilSerlProgress | None = None
    learner: HilSerlProgress | None = None
    log_tail: list[str] = Field(default_factory=list)


class HilSerlStartResponse(BaseModel):
    session_id: str
    state: HilSerlState
    checkpoint_path: str


class HilSerlCommandRequest(BaseModel):
    command: Literal["scene_ready", "stop"]


class HilSerlArtifact(BaseModel):
    id: str
    kind: Literal["model", "bc_model", "demo", "replay", "source"]
    name: str
    path: str
    modified_at: datetime
    size_bytes: int | None = None
    file_count: int | None = None
    latest: bool = False


class HilSerlArtifacts(BaseModel):
    root: str
    models: list[HilSerlArtifact] = Field(default_factory=list)
    bc_models: list[HilSerlArtifact] = Field(default_factory=list)
    demos: list[HilSerlArtifact] = Field(default_factory=list)
    replays: list[HilSerlArtifact] = Field(default_factory=list)
    source_folders: list[HilSerlArtifact] = Field(default_factory=list)
