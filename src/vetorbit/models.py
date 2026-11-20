from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


class CommandType(str, Enum):
    SLEW_TO_TARGET = "SLEW_TO_TARGET"
    FIRE_THRUSTER = "FIRE_THRUSTER"
    ORBIT_AVOIDANCE_BURN = "ORBIT_AVOIDANCE_BURN"
    CAPTURE_PAYLOAD = "CAPTURE_PAYLOAD"
    DOWNLINK_DATA = "DOWNLINK_DATA"
    ENTER_SAFE_MODE = "ENTER_SAFE_MODE"


class PointingMode(str, Enum):
    NADIR = "NADIR"
    SUN_POINT = "SUN_POINT"
    TARGET = "TARGET"
    SAFE = "SAFE"


class DebrisObject(BaseModel):
    object_id: str
    relative_distance_km: float
    closing_velocity_kms: float
    time_to_closest_approach_s: float
    collision_probability: float

    @field_validator("relative_distance_km", "closing_velocity_kms", "time_to_closest_approach_s", "collision_probability")
    @classmethod
    def non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("must be non-negative")
        return v


class SpacecraftState(BaseModel):
    timestamp_s: float
    altitude_km: float = Field(default=550.0, ge=0)
    orbital_phase_deg: float = Field(default=0.0, ge=0, lt=360)
    in_eclipse: bool = False
    battery_soc: float = Field(default=100.0, ge=0, le=100)
    bus_temp_c: float = Field(default=20.0)
    sun_angle_deg: float = Field(default=90.0, ge=0, le=180)
    pointing_mode: PointingMode = PointingMode.NADIR
    downlink_buffer_mb: float = Field(default=0.0, ge=0)
    propellant_kg: float = Field(default=100.0, ge=0)
    tracked_debris: list[DebrisObject] = Field(default_factory=list)


class SpacecraftCommand(BaseModel):
    command_id: str
    cmd_type: CommandType
    params: dict = Field(default_factory=dict)
    issued_by: str = "autonomous_planner"


class ValidationResult(BaseModel):
    approved: bool
    command_id: str
    veto_code: Optional[str] = None
    reason: str
    predicted_state: Optional[SpacecraftState] = None
    safety_margins: dict = Field(default_factory=dict)