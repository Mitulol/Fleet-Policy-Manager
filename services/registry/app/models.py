"""Request and response shapes for the Device Registry Service."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EnrollRequest(BaseModel):
    hostname: str = Field(min_length=1, max_length=128)
    os: str = Field(min_length=1, max_length=32)
    os_version: str = Field(default="unknown", max_length=32)
    device_group: str = Field(default="default", min_length=1, max_length=64)
    agent_version: str = Field(default="1.0.0", max_length=32)
    # Supplied by the fleet simulator so a restarted device keeps its identity
    # instead of enrolling twice.
    device_id: str | None = Field(default=None, max_length=64)


class Device(BaseModel):
    id: str
    hostname: str
    os: str
    os_version: str
    device_group: str
    agent_version: str
    enrolled_at: str
    last_heartbeat_at: str | None
    heartbeat_count: int
    status: str


class HeartbeatRequest(BaseModel):
    agent_version: str | None = Field(default=None, max_length=32)


class HeartbeatResponse(BaseModel):
    device_id: str
    received_at: str
    next_heartbeat_seconds: int


class GroupSummary(BaseModel):
    device_group: str
    total: int
    online: int


class RegistryStats(BaseModel):
    total_devices: int
    online: int
    offline: int
    groups: list[GroupSummary]
    heartbeat_ttl_seconds: int
