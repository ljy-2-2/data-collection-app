"""Tianji-only requests. Joint units follow the vendor SDK (degrees)."""

from typing import Literal
from functools import partial

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .tianji_gripper_config import gripper_parameter_default, validate_gripper_endpoints, GRIPPER_ANGLE_LIMIT


class TianjiParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)
    scale: float = Field(default=1, ge=0.1, le=2)
    max_joint_speed_deg_s: float = Field(default=180, ge=0.1, le=180)
    max_joint_accel_deg_s2: float = Field(default=450, ge=1, le=450)
    left_gripper_speed: float = Field(default_factory=partial(gripper_parameter_default, 'left_gripper_speed'), ge=0.01, le=100)
    right_gripper_speed: float = Field(default_factory=partial(gripper_parameter_default, 'right_gripper_speed'), ge=0.01, le=100)
    left_gripper_accel: float = Field(default_factory=partial(gripper_parameter_default, 'left_gripper_accel'), ge=0.1, le=200)
    right_gripper_accel: float = Field(default_factory=partial(gripper_parameter_default, 'right_gripper_accel'), ge=0.1, le=200)
    left_gripper_open_rad: float = Field(default_factory=partial(gripper_parameter_default, 'left_gripper_open_rad'), gt=-GRIPPER_ANGLE_LIMIT, lt=GRIPPER_ANGLE_LIMIT)
    left_gripper_closed_rad: float = Field(default_factory=partial(gripper_parameter_default, 'left_gripper_closed_rad'), gt=-GRIPPER_ANGLE_LIMIT, lt=GRIPPER_ANGLE_LIMIT)
    right_gripper_open_rad: float = Field(default_factory=partial(gripper_parameter_default, 'right_gripper_open_rad'), gt=-GRIPPER_ANGLE_LIMIT, lt=GRIPPER_ANGLE_LIMIT)
    right_gripper_closed_rad: float = Field(default_factory=partial(gripper_parameter_default, 'right_gripper_closed_rad'), gt=-GRIPPER_ANGLE_LIMIT, lt=GRIPPER_ANGLE_LIMIT)

    @model_validator(mode="after")
    def validate_endpoints(self):
        validate_gripper_endpoints(self.model_dump())
        return self


class TianjiStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["dual", "left", "right"] = "dual"
    upload_mode: Literal["dual", "left", "right"] | None = None
    task: str = Field(min_length=1, max_length=120)
    parameters: TianjiParameters = Field(default_factory=TianjiParameters)

    @model_validator(mode="after")
    def validate_upload_mode(self):
        if self.mode != "dual" and self.upload_mode not in (None, self.mode):
            raise ValueError("上传曲线必须来自本次控制的机械臂")
        return self


class TianjiMove(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["dual", "left", "right"] = "dual"
    kind: Literal["zero", "jog", "pose", "gripper"]
    side: Literal["left", "right"] | None = None
    joint: int = Field(default=1, ge=1, le=7)
    delta: float = Field(default=0, ge=-10, le=10)
    targets: dict[str, list[float]] | None = None
    closure: float = Field(default=0, ge=0, le=1)
    parameters: TianjiParameters = Field(default_factory=TianjiParameters)


class TianjiCameraSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode_id: str | None = Field(default=None, min_length=1, max_length=64)
    brightness: int | None = Field(default=None, strict=True, ge=-32768, le=32767)

    @model_validator(mode='after')
    def require_setting(self):
        if self.mode_id is None and self.brightness is None:
            raise ValueError('请提供相机分辨率或硬件亮度')
        return self


class TianjiCameraSettingsBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    changes: dict[Literal['usb_1', 'usb_2', 'usb_4'], TianjiCameraSettings] = Field(min_length=1, max_length=3)
