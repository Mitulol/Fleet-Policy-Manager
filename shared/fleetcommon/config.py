"""Environment-backed configuration helpers."""

from __future__ import annotations

import os


def env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    """Comma-separated list from the environment, blanks discarded."""
    raw = env_str(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# The name of the Redis stream every service publishes fleet events onto.
EVENT_STREAM = env_str("FLEET_EVENT_STREAM", "fleet.events")

# How long a device may go without a heartbeat before it counts as offline.
HEARTBEAT_TTL_SECONDS = env_int("HEARTBEAT_TTL_SECONDS", 45)
