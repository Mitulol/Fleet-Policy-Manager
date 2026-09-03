"""Request and response shapes for the Policy Service."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    target_group: str = Field(min_length=1, max_length=64)
    settings: dict[str, Any] = Field(default_factory=dict)


class PolicyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    target_group: str | None = Field(default=None, max_length=64)
    settings: dict[str, Any] | None = None


class Policy(BaseModel):
    id: str
    name: str
    description: str
    target_group: str
    settings: dict[str, Any]
    version: int
    published: bool
    created_at: str
    updated_at: str


class Rollout(BaseModel):
    policy_id: str
    version: int
    target_group: str
    published_at: str
    event_id: str


class PolicyStats(BaseModel):
    total_policies: int
    published_policies: int
    draft_policies: int
    total_rollouts: int
    event_bus_connected: bool
