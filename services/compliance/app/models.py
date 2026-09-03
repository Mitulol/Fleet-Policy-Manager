"""Request and response shapes for the Compliance Service."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ComplianceReport(BaseModel):
    """What a device agent sends in.

    The device reports facts about itself -- which policy versions it has
    applied and how its local checks came out. It does not report a verdict.
    Deciding compliance is this service's job, because only this service knows
    what the fleet is currently expected to be running.
    """

    device_id: str = Field(min_length=1, max_length=64)
    device_group: str = Field(default="default", min_length=1, max_length=64)
    # Policy id to the version of it the device has actually applied.
    applied_policies: dict[str, int] = Field(default_factory=dict)
    # Local posture checks, e.g. {"disk_encryption": true, "firewall": false}.
    checks: dict[str, bool] = Field(default_factory=dict)


class ReportAccepted(BaseModel):
    device_id: str
    compliant: bool
    findings: list[str]
    served_by: str
    evaluated_at: str


class DeviceCompliance(BaseModel):
    device_id: str
    device_group: str
    compliant: bool
    findings: list[str]
    applied_policies: dict[str, int]
    last_report_at: str
    last_served_by: str
    report_count: int


class RolloutProgress(BaseModel):
    policy_id: str
    name: str
    target_group: str
    version: int
    targeted_devices: int
    converged_devices: int
    percent_complete: float


class InstanceActivity(BaseModel):
    instance: str
    reports_handled: int
    events_handled: int
    last_seen: str
    seconds_since_last_seen: float


class ComplianceStats(BaseModel):
    total_devices_reporting: int
    compliant: int
    non_compliant: int
    stale: int
    rollouts: list[RolloutProgress]
    instances: list[InstanceActivity]
    tracked_policies: int
    served_by: str


class PolicyExpectation(BaseModel):
    policy_id: str
    name: str
    target_group: str
    version: int
    active: bool
    received_at: str
    settings: dict[str, Any]
