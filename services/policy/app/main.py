"""Policy Service.

Defines configuration policies and rolls them out to device groups. Publishing
a policy writes a durable event onto the fleet event stream; the Policy Service
does not call the Compliance Service, does not know its address, and does not
block on it. That is the decoupling the architecture is meant to demonstrate:
a rollout succeeds whether or not any consumer is awake to see it.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, Query, Response

from fleetcommon.config import env_str
from fleetcommon.events import (
    POLICY_DELETED,
    POLICY_PUBLISHED,
    POLICY_UPDATED,
    EventPublisher,
    make_redis,
)
from fleetcommon.logging import configure_logging

from .db import connect
from .models import Policy, PolicyCreate, PolicyStats, PolicyUpdate, Rollout

SERVICE_NAME = "policy-service"
DB_PATH = env_str("POLICY_DB_PATH", "/data/policy.db")
REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")

logger = configure_logging(SERVICE_NAME)
state: dict[str, Any] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_policy(row: aiosqlite.Row) -> Policy:
    return Policy(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        target_group=row["target_group"],
        settings=json.loads(row["settings"]),
        version=row["version"],
        published=bool(row["published"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _load(db: aiosqlite.Connection, policy_id: str) -> aiosqlite.Row:
    async with db.execute(
        "SELECT * FROM policies WHERE id = ? AND deleted = 0", (policy_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return row


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    state["db"] = await connect(DB_PATH)
    state["redis"] = make_redis(REDIS_URL)
    state["publisher"] = EventPublisher(state["redis"], source=SERVICE_NAME)
    logger.info("policy service ready", extra={"context": {"db_path": DB_PATH}})
    yield
    await state["db"].close()
    await state["redis"].aclose()


app = FastAPI(
    title="Policy Service",
    description="Configuration policy definition and rollout to device groups.",
    version="1.0.0",
    lifespan=lifespan,
)


async def _event_bus_healthy() -> bool:
    try:
        return bool(await state["redis"].ping())
    except Exception:
        return False


@app.get("/health")
async def health() -> dict[str, object]:
    db = state.get("db")
    if db is None:
        raise HTTPException(status_code=503, detail="database not ready")
    await db.execute("SELECT 1")
    # The event bus being down is reported but does not fail the health check:
    # policies can still be authored and read, and rollouts are what degrade.
    return {
        "status": "healthy",
        "service": SERVICE_NAME,
        "event_bus_connected": await _event_bus_healthy(),
    }


@app.post("/policies", response_model=Policy, status_code=201)
async def create_policy(request: PolicyCreate) -> Policy:
    db = state["db"]
    policy_id = str(uuid.uuid4())
    now = _now()
    await db.execute(
        """
        INSERT INTO policies (
            id, name, description, target_group, settings,
            version, published, created_at, updated_at, deleted
        ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, 0)
        """,
        (
            policy_id,
            request.name,
            request.description,
            request.target_group,
            json.dumps(request.settings),
            now,
            now,
        ),
    )
    await db.commit()
    return _to_policy(await _load(db, policy_id))


@app.get("/policies", response_model=list[Policy])
async def list_policies(
    target_group: str | None = Query(default=None),
    published: bool | None = Query(default=None),
) -> list[Policy]:
    db = state["db"]
    sql = "SELECT * FROM policies WHERE deleted = 0"
    params: list[object] = []
    if target_group:
        sql += " AND target_group = ?"
        params.append(target_group)
    if published is not None:
        sql += " AND published = ?"
        params.append(1 if published else 0)
    sql += " ORDER BY created_at DESC"

    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [_to_policy(row) for row in rows]


@app.get("/policies/{policy_id}", response_model=Policy)
async def get_policy(policy_id: str) -> Policy:
    return _to_policy(await _load(state["db"], policy_id))


@app.put("/policies/{policy_id}", response_model=Policy)
async def update_policy(policy_id: str, request: PolicyUpdate) -> Policy:
    """Edit a policy. Editing a published policy bumps its version and
    re-announces it, because devices already running the old version need to
    converge on the new one."""
    db = state["db"]
    current = await _load(db, policy_id)

    name = request.name if request.name is not None else current["name"]
    description = (
        request.description if request.description is not None else current["description"]
    )
    target_group = (
        request.target_group if request.target_group is not None else current["target_group"]
    )
    settings = (
        json.dumps(request.settings)
        if request.settings is not None
        else current["settings"]
    )
    new_version = current["version"] + 1
    now = _now()

    await db.execute(
        """
        UPDATE policies
           SET name = ?, description = ?, target_group = ?,
               settings = ?, version = ?, updated_at = ?
         WHERE id = ?
        """,
        (name, description, target_group, settings, new_version, now, policy_id),
    )
    await db.commit()

    if current["published"]:
        await _announce(POLICY_UPDATED, policy_id, name, target_group, new_version, settings)

    return _to_policy(await _load(db, policy_id))


@app.post("/policies/{policy_id}/publish", response_model=Rollout, status_code=202)
async def publish_policy(policy_id: str) -> Rollout:
    """Roll a policy out to its target group.

    Returns 202: the rollout has been accepted and announced, but devices
    converge on it asynchronously as they check in.
    """
    db = state["db"]
    row = await _load(db, policy_id)
    now = _now()

    event_id = await _announce(
        POLICY_PUBLISHED,
        policy_id,
        row["name"],
        row["target_group"],
        row["version"],
        row["settings"],
    )

    await db.execute(
        "UPDATE policies SET published = 1, updated_at = ? WHERE id = ?", (now, policy_id)
    )
    await db.execute(
        """
        INSERT INTO rollouts (policy_id, version, target_group, published_at, event_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (policy_id, row["version"], row["target_group"], now, event_id),
    )
    await db.commit()

    return Rollout(
        policy_id=policy_id,
        version=row["version"],
        target_group=row["target_group"],
        published_at=now,
        event_id=event_id,
    )


@app.delete("/policies/{policy_id}", status_code=204)
async def delete_policy(policy_id: str) -> Response:
    db = state["db"]
    row = await _load(db, policy_id)
    await db.execute("UPDATE policies SET deleted = 1 WHERE id = ?", (policy_id,))
    await db.commit()

    if row["published"]:
        await _announce(
            POLICY_DELETED,
            policy_id,
            row["name"],
            row["target_group"],
            row["version"],
            row["settings"],
        )
    return Response(status_code=204)


@app.get("/policies/for-group/{device_group}", response_model=list[Policy])
async def policies_for_group(device_group: str) -> list[Policy]:
    """What a device in this group is expected to be running.

    This is the read a device agent makes to find out what to apply.
    """
    db = state["db"]
    async with db.execute(
        """
        SELECT * FROM policies
         WHERE deleted = 0 AND published = 1 AND target_group = ?
         ORDER BY name
        """,
        (device_group,),
    ) as cur:
        rows = await cur.fetchall()
    return [_to_policy(row) for row in rows]


@app.get("/rollouts", response_model=list[Rollout])
async def list_rollouts(limit: int = Query(default=50, ge=1, le=500)) -> list[Rollout]:
    db = state["db"]
    async with db.execute(
        "SELECT * FROM rollouts ORDER BY published_at DESC LIMIT ?", (limit,)
    ) as cur:
        rows = await cur.fetchall()
    return [
        Rollout(
            policy_id=row["policy_id"],
            version=row["version"],
            target_group=row["target_group"],
            published_at=row["published_at"],
            event_id=row["event_id"],
        )
        for row in rows
    ]


@app.get("/stats", response_model=PolicyStats)
async def stats() -> PolicyStats:
    db = state["db"]
    async with db.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(published), 0) AS published
          FROM policies WHERE deleted = 0
        """
    ) as cur:
        row = await cur.fetchone()
    async with db.execute("SELECT COUNT(*) AS c FROM rollouts") as cur:
        rollouts = await cur.fetchone()

    total = row["total"]
    published = row["published"]
    return PolicyStats(
        total_policies=total,
        published_policies=published,
        draft_policies=total - published,
        total_rollouts=rollouts["c"],
        event_bus_connected=await _event_bus_healthy(),
    )


async def _announce(
    event_type: str,
    policy_id: str,
    name: str,
    target_group: str,
    version: int,
    settings_json: str,
) -> str:
    """Publish a rollout event, tolerating an unavailable event bus.

    If Redis is unreachable the policy write still stands. The rollout is
    recorded locally and the failure is logged rather than raised, so an event
    bus outage cannot take policy authoring down with it.
    """
    try:
        return await state["publisher"].publish(
            event_type,
            {
                "policy_id": policy_id,
                "name": name,
                "target_group": target_group,
                "version": version,
                "settings": json.loads(settings_json),
            },
        )
    except Exception:
        logger.exception(
            "failed to publish rollout event",
            extra={"context": {"policy_id": policy_id, "event_type": event_type}},
        )
        return "unpublished"
