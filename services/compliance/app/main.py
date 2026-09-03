"""Compliance Service.

Ingests device status reports, decides which devices are out of compliance, and
tracks how far each policy rollout has progressed.

This is the horizontally scaled service. Several identical replicas sit behind
a load balancer sharing one Postgres store, so any replica can serve any
report. Each replica stamps the reports it handles with its own instance id,
which is what makes load distribution -- and recovery after a replica is
killed -- directly observable rather than merely asserted.

It learns what the fleet is supposed to be running by consuming policy events
off the bus, never by calling the Policy Service. If Policy is down, previously
announced expectations are still in Postgres and compliance evaluation
continues uninterrupted.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException, Query

from fleetcommon.config import env_float, env_int, env_str
from fleetcommon.events import (
    POLICY_DELETED,
    POLICY_PUBLISHED,
    POLICY_UPDATED,
    EventConsumer,
    make_redis,
)
from fleetcommon.logging import configure_logging

from .db import create_pool
from .models import (
    ComplianceReport,
    ComplianceStats,
    DeviceCompliance,
    InstanceActivity,
    PolicyExpectation,
    ReportAccepted,
    RolloutProgress,
)

SERVICE_NAME = "compliance-service"
# Falls back to the container hostname, which docker compose makes unique per
# replica, so instance ids are distinct without any manual configuration.
INSTANCE_ID = env_str("INSTANCE_ID", socket.gethostname())
POSTGRES_DSN = env_str(
    "COMPLIANCE_DSN", "postgresql://fleet:fleet@compliance-db:5432/compliance"
)
REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")
CONSUMER_GROUP = env_str("COMPLIANCE_CONSUMER_GROUP", "compliance-workers")
# A device with no report inside this window is stale rather than compliant.
STALE_AFTER_SECONDS = env_int("COMPLIANCE_STALE_SECONDS", 90)
REPORT_RETENTION = env_int("COMPLIANCE_REPORT_RETENTION", 50_000)
INSTANCE_FLUSH_SECONDS = env_float("INSTANCE_FLUSH_SECONDS", 3.0)

logger = configure_logging(SERVICE_NAME, instance=INSTANCE_ID)
state: dict[str, Any] = {}

# Per-replica throughput counters, held in memory and flushed on an interval.
#
# These used to be incremented inside the request transaction. Because there is
# exactly one instance_activity row per replica, every concurrent request on a
# replica took a lock on that same row and the whole ingest path serialised
# behind it -- load testing put the ceiling near 48 requests per second per
# replica no matter how much hardware was available. Counting in memory and
# flushing the accumulated delta periodically takes that contention off the hot
# path entirely. The cost is that a replica killed between flushes loses up to
# one interval of its own counters, which is an acceptable trade for a metric.
_counters = {"reports": 0, "events": 0}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _flush_counters_forever() -> None:
    """Write accumulated per-replica counters, and refresh liveness.

    The row is touched every interval even when no traffic arrived, so a
    replica that is alive but idle is distinguishable from one that has died:
    a dead replica stops updating last_seen altogether.
    """
    while True:
        await asyncio.sleep(INSTANCE_FLUSH_SECONDS)
        reports, events = _counters["reports"], _counters["events"]
        _counters["reports"] -= reports
        _counters["events"] -= events
        try:
            await state["pool"].execute(
                """
                INSERT INTO instance_activity (instance, reports_handled, events_handled, last_seen)
                VALUES ($1, $2, $3, now())
                ON CONFLICT (instance) DO UPDATE SET
                    reports_handled = instance_activity.reports_handled + EXCLUDED.reports_handled,
                    events_handled  = instance_activity.events_handled + EXCLUDED.events_handled,
                    last_seen       = now()
                """,
                INSTANCE_ID,
                reports,
                events,
            )
        except Exception:
            # Put the counts back so a transient database blip does not lose
            # them, and try again on the next tick.
            _counters["reports"] += reports
            _counters["events"] += events
            logger.exception("failed to flush instance counters")


async def _handle_event(event: dict[str, Any]) -> None:
    """Apply a policy event to this service's own view of the fleet.

    Exactly one replica runs this per event, because all replicas join the same
    consumer group. The write is an upsert regardless, so a redelivery after a
    failed acknowledgement is harmless.
    """
    event_type = event.get("type")
    payload = event.get("payload") or {}
    policy_id = payload.get("policy_id")
    if not policy_id:
        return

    pool: asyncpg.Pool = state["pool"]

    if event_type in (POLICY_PUBLISHED, POLICY_UPDATED):
        await pool.execute(
            """
            INSERT INTO policy_expectations
                (policy_id, name, target_group, version, settings, active, received_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, TRUE, now())
            ON CONFLICT (policy_id) DO UPDATE SET
                name         = EXCLUDED.name,
                target_group = EXCLUDED.target_group,
                -- Never move an expectation backwards; a redelivered older
                -- event must not undo a newer rollout.
                version      = GREATEST(policy_expectations.version, EXCLUDED.version),
                settings     = EXCLUDED.settings,
                active       = TRUE,
                received_at  = now()
            """,
            policy_id,
            payload.get("name", "unnamed"),
            payload.get("target_group", "default"),
            int(payload.get("version", 1)),
            json.dumps(payload.get("settings", {})),
        )
        logger.info(
            "applied policy expectation",
            extra={
                "context": {
                    "policy_id": policy_id,
                    "version": payload.get("version"),
                    "target_group": payload.get("target_group"),
                    "event_type": event_type,
                }
            },
        )
    elif event_type == POLICY_DELETED:
        await pool.execute(
            "UPDATE policy_expectations SET active = FALSE WHERE policy_id = $1",
            policy_id,
        )
        logger.info("retired policy expectation", extra={"context": {"policy_id": policy_id}})
    else:
        return

    _counters["events"] += 1


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["pool"] = await create_pool(POSTGRES_DSN)
    state["redis"] = make_redis(REDIS_URL)

    consumer = EventConsumer(
        client=state["redis"],
        group=CONSUMER_GROUP,
        consumer=INSTANCE_ID,
        handler=_handle_event,
    )
    await consumer.ensure_group()
    consumer.start()
    state["consumer"] = consumer

    await state["pool"].execute(
        """
        INSERT INTO instance_activity (instance, first_seen, last_seen)
        VALUES ($1, now(), now())
        ON CONFLICT (instance) DO UPDATE SET last_seen = now()
        """,
        INSTANCE_ID,
    )
    state["flusher"] = asyncio.create_task(_flush_counters_forever())
    logger.info("compliance replica ready")
    yield

    state["flusher"].cancel()
    await state["consumer"].stop()
    await state["redis"].aclose()
    await state["pool"].close()


app = FastAPI(
    title="Compliance Service",
    description="Device compliance evaluation and policy rollout tracking.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def stamp_instance(request, call_next):
    """Tag every response with the replica that produced it.

    This is what lets a caller outside the cluster see which replica the load
    balancer picked, without reading any container logs.
    """
    response = await call_next(request)
    response.headers["X-Served-By"] = INSTANCE_ID
    return response


@app.get("/health")
async def health() -> dict[str, object]:
    """Health of this specific replica.

    The load balancer polls this. A replica that cannot reach Postgres reports
    unhealthy and is taken out of rotation rather than serving errors.
    """
    pool = state.get("pool")
    if pool is None:
        raise HTTPException(status_code=503, detail="database pool not ready")
    try:
        await pool.fetchval("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}") from exc
    return {"status": "healthy", "service": SERVICE_NAME, "instance": INSTANCE_ID}


@app.get("/instance")
async def whoami() -> dict[str, str]:
    """Which replica answered. Used to demonstrate load balancer spread."""
    return {"instance": INSTANCE_ID}


@app.post("/reports", response_model=ReportAccepted, status_code=202)
async def ingest_report(report: ComplianceReport) -> ReportAccepted:
    """The hot path. Every device in the fleet posts here on an interval.

    Compliance is derived here rather than trusted from the device: a failed
    local check is a finding, and so is running an older version of a policy
    than the fleet currently expects -- which this service knows only because
    it consumed the rollout event.
    """
    pool: asyncpg.Pool = state["pool"]
    now = _now()

    expectations = await pool.fetch(
        """
        SELECT policy_id, version FROM policy_expectations
         WHERE active AND target_group = $1
        """,
        report.device_group,
    )

    findings: list[str] = [
        f"check_failed:{name}" for name, passed in sorted(report.checks.items()) if not passed
    ]
    for row in expectations:
        applied = report.applied_policies.get(row["policy_id"], 0)
        if applied < row["version"]:
            findings.append(f"policy_drift:{row['policy_id']}")

    compliant = not findings

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO device_compliance (
                    device_id, device_group, applied_policies, findings,
                    compliant, last_report_at, last_served_by, report_count
                ) VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, 1)
                ON CONFLICT (device_id) DO UPDATE SET
                    device_group     = EXCLUDED.device_group,
                    applied_policies = EXCLUDED.applied_policies,
                    findings         = EXCLUDED.findings,
                    compliant        = EXCLUDED.compliant,
                    last_report_at   = EXCLUDED.last_report_at,
                    last_served_by   = EXCLUDED.last_served_by,
                    report_count     = device_compliance.report_count + 1
                """,
                report.device_id,
                report.device_group,
                json.dumps(report.applied_policies),
                json.dumps(findings),
                compliant,
                now,
                INSTANCE_ID,
            )
            await conn.execute(
                """
                INSERT INTO compliance_reports
                    (device_id, device_group, compliant, findings, served_by, reported_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                """,
                report.device_id,
                report.device_group,
                compliant,
                json.dumps(findings),
                INSTANCE_ID,
                now,
            )

    # Counted in memory rather than in the transaction above, so concurrent
    # reports do not queue behind a lock on this replica's counter row.
    _counters["reports"] += 1

    return ReportAccepted(
        device_id=report.device_id,
        compliant=compliant,
        findings=findings,
        served_by=INSTANCE_ID,
        evaluated_at=now.isoformat(),
    )


@app.get("/devices/{device_id}/compliance", response_model=DeviceCompliance)
async def device_compliance(device_id: str) -> DeviceCompliance:
    row = await state["pool"].fetchrow(
        "SELECT * FROM device_compliance WHERE device_id = $1", device_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no compliance data for device")
    return _to_device_compliance(row)


@app.get("/noncompliant", response_model=list[DeviceCompliance])
async def non_compliant(
    limit: int = Query(default=100, ge=1, le=1000),
    device_group: str | None = Query(default=None),
) -> list[DeviceCompliance]:
    sql = "SELECT * FROM device_compliance WHERE compliant = FALSE"
    params: list[object] = []
    if device_group:
        sql += " AND device_group = $1"
        params.append(device_group)
    sql += f" ORDER BY last_report_at DESC LIMIT ${len(params) + 1}"
    params.append(limit)

    rows = await state["pool"].fetch(sql, *params)
    return [_to_device_compliance(row) for row in rows]


@app.get("/expectations", response_model=list[PolicyExpectation])
async def expectations() -> list[PolicyExpectation]:
    """What this service believes the fleet should be running.

    Populated entirely from the event stream. Useful for showing that the
    Compliance Service stays correct while the Policy Service is stopped.
    """
    rows = await state["pool"].fetch(
        "SELECT * FROM policy_expectations ORDER BY received_at DESC"
    )
    return [
        PolicyExpectation(
            policy_id=row["policy_id"],
            name=row["name"],
            target_group=row["target_group"],
            version=row["version"],
            active=row["active"],
            received_at=row["received_at"].isoformat(),
            settings=json.loads(row["settings"]),
        )
        for row in rows
    ]


@app.get("/stats", response_model=ComplianceStats)
async def stats() -> ComplianceStats:
    pool: asyncpg.Pool = state["pool"]
    cutoff = _now() - timedelta(seconds=STALE_AFTER_SECONDS)

    totals = await pool.fetchrow(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE compliant AND last_report_at >= $1) AS compliant,
               COUNT(*) FILTER (WHERE NOT compliant AND last_report_at >= $1) AS non_compliant,
               COUNT(*) FILTER (WHERE last_report_at < $1) AS stale
          FROM device_compliance
        """,
        cutoff,
    )

    rollout_rows = await pool.fetch(
        """
        SELECT e.policy_id,
               e.name,
               e.target_group,
               e.version,
               COUNT(d.device_id) AS targeted,
               COUNT(*) FILTER (
                   WHERE d.device_id IS NOT NULL
                     AND COALESCE((d.applied_policies ->> e.policy_id)::int, 0) >= e.version
               ) AS converged
          FROM policy_expectations e
          LEFT JOIN device_compliance d ON d.device_group = e.target_group
         WHERE e.active
         GROUP BY e.policy_id, e.name, e.target_group, e.version
         ORDER BY e.name
        """
    )

    instance_rows = await pool.fetch(
        "SELECT * FROM instance_activity ORDER BY reports_handled DESC"
    )
    now = _now()

    return ComplianceStats(
        total_devices_reporting=totals["total"],
        compliant=totals["compliant"],
        non_compliant=totals["non_compliant"],
        stale=totals["stale"],
        rollouts=[
            RolloutProgress(
                policy_id=row["policy_id"],
                name=row["name"],
                target_group=row["target_group"],
                version=row["version"],
                targeted_devices=row["targeted"],
                converged_devices=row["converged"],
                percent_complete=(
                    round(100.0 * row["converged"] / row["targeted"], 1)
                    if row["targeted"]
                    else 0.0
                ),
            )
            for row in rollout_rows
        ],
        instances=[
            InstanceActivity(
                instance=row["instance"],
                reports_handled=row["reports_handled"],
                events_handled=row["events_handled"],
                last_seen=row["last_seen"].isoformat(),
                seconds_since_last_seen=round((now - row["last_seen"]).total_seconds(), 1),
            )
            for row in instance_rows
        ],
        tracked_policies=len(rollout_rows),
        served_by=INSTANCE_ID,
    )


@app.post("/maintenance/prune", status_code=200)
async def prune_reports() -> dict[str, int]:
    """Trim the append-only report log so a long demo run stays bounded."""
    deleted = await state["pool"].fetchval(
        """
        WITH doomed AS (
            SELECT id FROM compliance_reports
             ORDER BY id DESC OFFSET $1
        )
        DELETE FROM compliance_reports
         WHERE id IN (SELECT id FROM doomed)
        RETURNING 1
        """,
        REPORT_RETENTION,
    )
    return {"deleted": deleted or 0}


def _to_device_compliance(row: asyncpg.Record) -> DeviceCompliance:
    return DeviceCompliance(
        device_id=row["device_id"],
        device_group=row["device_group"],
        compliant=row["compliant"],
        findings=json.loads(row["findings"]),
        applied_policies=json.loads(row["applied_policies"]),
        last_report_at=row["last_report_at"].isoformat(),
        last_served_by=row["last_served_by"],
        report_count=row["report_count"],
    )
