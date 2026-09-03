"""Device Registry Service.

Owns device identity: enrollment, group membership and heartbeat liveness.
Whether a device counts as online is derived at read time from the age of its
last heartbeat, so there is no background sweeper to fall behind or die.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiosqlite
from fastapi import FastAPI, HTTPException, Query, Response

from fleetcommon.config import HEARTBEAT_TTL_SECONDS, env_int, env_str
from fleetcommon.logging import configure_logging

from .db import connect
from .models import (
    Device,
    EnrollRequest,
    GroupSummary,
    HeartbeatRequest,
    HeartbeatResponse,
    RegistryStats,
)

SERVICE_NAME = "registry-service"
DB_PATH = env_str("REGISTRY_DB_PATH", "/data/registry.db")
HEARTBEAT_INTERVAL = env_int("HEARTBEAT_INTERVAL_SECONDS", 15)

logger = configure_logging(SERVICE_NAME)
state: dict[str, aiosqlite.Connection] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_for(last_heartbeat_at: str | None) -> str:
    if not last_heartbeat_at:
        return "never_reported"
    try:
        seen = datetime.fromisoformat(last_heartbeat_at)
    except ValueError:
        return "never_reported"
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_TTL_SECONDS)
    return "online" if seen >= cutoff else "offline"


def _to_device(row: aiosqlite.Row) -> Device:
    return Device(
        id=row["id"],
        hostname=row["hostname"],
        os=row["os"],
        os_version=row["os_version"],
        device_group=row["device_group"],
        agent_version=row["agent_version"],
        enrolled_at=row["enrolled_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        heartbeat_count=row["heartbeat_count"],
        status=_status_for(row["last_heartbeat_at"]),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    state["db"] = await connect(DB_PATH)
    logger.info("registry ready", extra={"context": {"db_path": DB_PATH}})
    yield
    await state["db"].close()


app = FastAPI(
    title="Device Registry Service",
    description="Device identity, enrollment and heartbeat liveness.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, object]:
    db = state.get("db")
    if db is None:
        raise HTTPException(status_code=503, detail="database not ready")
    await db.execute("SELECT 1")
    return {"status": "healthy", "service": SERVICE_NAME}


@app.post("/devices", response_model=Device, status_code=201)
async def enroll_device(request: EnrollRequest) -> Device:
    """Enroll a device, or refresh it if it is enrolling again after a restart."""
    db = state["db"]
    device_id = request.device_id or str(uuid.uuid4())
    now = _now()

    await db.execute(
        """
        INSERT INTO devices (
            id, hostname, os, os_version, device_group,
            agent_version, enrolled_at, last_heartbeat_at, heartbeat_count, retired
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, 0)
        ON CONFLICT(id) DO UPDATE SET
            hostname      = excluded.hostname,
            os            = excluded.os,
            os_version    = excluded.os_version,
            device_group  = excluded.device_group,
            agent_version = excluded.agent_version,
            retired       = 0
        """,
        (
            device_id,
            request.hostname,
            request.os,
            request.os_version,
            request.device_group,
            request.agent_version,
            now,
        ),
    )
    await db.commit()

    async with db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)) as cur:
        row = await cur.fetchone()
    return _to_device(row)


@app.get("/devices", response_model=list[Device])
async def list_devices(
    device_group: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(online|offline|never_reported)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[Device]:
    db = state["db"]
    sql = "SELECT * FROM devices WHERE retired = 0"
    params: list[object] = []
    if device_group:
        sql += " AND device_group = ?"
        params.append(device_group)
    sql += " ORDER BY enrolled_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()

    devices = [_to_device(row) for row in rows]
    if status:
        devices = [d for d in devices if d.status == status]
    return devices


@app.get("/devices/{device_id}", response_model=Device)
async def get_device(device_id: str) -> Device:
    db = state["db"]
    async with db.execute(
        "SELECT * FROM devices WHERE id = ? AND retired = 0", (device_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="device not found")
    return _to_device(row)


@app.post("/devices/{device_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(device_id: str, request: HeartbeatRequest) -> HeartbeatResponse:
    """The hot path: every device in the fleet calls this on an interval."""
    db = state["db"]
    now = _now()
    cursor = await db.execute(
        """
        UPDATE devices
           SET last_heartbeat_at = ?,
               heartbeat_count   = heartbeat_count + 1,
               agent_version     = COALESCE(?, agent_version)
         WHERE id = ? AND retired = 0
        """,
        (now, request.agent_version, device_id),
    )
    await db.commit()

    if cursor.rowcount == 0:
        # An unknown device must re-enroll rather than be silently created.
        raise HTTPException(status_code=404, detail="device not enrolled")

    return HeartbeatResponse(
        device_id=device_id,
        received_at=now,
        next_heartbeat_seconds=HEARTBEAT_INTERVAL,
    )


@app.delete("/devices/{device_id}", status_code=204)
async def retire_device(device_id: str) -> Response:
    db = state["db"]
    cursor = await db.execute(
        "UPDATE devices SET retired = 1 WHERE id = ? AND retired = 0", (device_id,)
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="device not found")
    return Response(status_code=204)


@app.get("/groups", response_model=list[GroupSummary])
async def list_groups() -> list[GroupSummary]:
    db = state["db"]
    async with db.execute(
        """
        SELECT device_group, last_heartbeat_at
          FROM devices
         WHERE retired = 0
        """
    ) as cur:
        rows = await cur.fetchall()

    totals: dict[str, list[int]] = {}
    for row in rows:
        bucket = totals.setdefault(row["device_group"], [0, 0])
        bucket[0] += 1
        if _status_for(row["last_heartbeat_at"]) == "online":
            bucket[1] += 1

    return [
        GroupSummary(device_group=group, total=count, online=online)
        for group, (count, online) in sorted(totals.items())
    ]


@app.get("/stats", response_model=RegistryStats)
async def stats() -> RegistryStats:
    """Fleet-wide counts, consumed by the gateway's dashboard aggregate."""
    groups = await list_groups()
    total = sum(g.total for g in groups)
    online = sum(g.online for g in groups)
    return RegistryStats(
        total_devices=total,
        online=online,
        offline=total - online,
        groups=groups,
        heartbeat_ttl_seconds=HEARTBEAT_TTL_SECONDS,
    )
