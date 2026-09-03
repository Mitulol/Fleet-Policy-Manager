"""Storage for the Compliance Service.

This service is the one that scales horizontally, which drives the choice of
store. Registry and Policy each keep a private SQLite file because exactly one
process ever writes them. Compliance runs as N replicas behind a load balancer,
so a file-per-container would give each replica its own divergent view of the
fleet -- three replicas, three different answers to "how many devices are out
of compliance". It therefore gets Postgres.

This is still a store private to one service. Registry and Policy hold no
connection string to it and cannot read it; it is shared between replicas of a
single service, not between services.
"""

from __future__ import annotations

import asyncpg

# Replicas start at the same moment under docker compose, so schema creation
# races. An advisory lock makes the first one through do the work and the rest
# wait, instead of colliding inside the system catalogue.
SCHEMA_LOCK_ID = 913_477_001

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_expectations (
    policy_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    target_group TEXT NOT NULL,
    version      INTEGER NOT NULL,
    settings     JSONB NOT NULL DEFAULT '{}'::jsonb,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_expect_group ON policy_expectations(target_group);

CREATE TABLE IF NOT EXISTS device_compliance (
    device_id        TEXT PRIMARY KEY,
    device_group     TEXT NOT NULL,
    applied_policies JSONB NOT NULL DEFAULT '{}'::jsonb,
    findings         JSONB NOT NULL DEFAULT '[]'::jsonb,
    compliant        BOOLEAN NOT NULL DEFAULT FALSE,
    last_report_at   TIMESTAMPTZ NOT NULL,
    last_served_by   TEXT NOT NULL,
    report_count     BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_devcomp_group ON device_compliance(device_group);
CREATE INDEX IF NOT EXISTS idx_devcomp_compliant ON device_compliance(compliant);
CREATE INDEX IF NOT EXISTS idx_devcomp_reported ON device_compliance(last_report_at);

CREATE TABLE IF NOT EXISTS compliance_reports (
    id           BIGSERIAL PRIMARY KEY,
    device_id    TEXT NOT NULL,
    device_group TEXT NOT NULL,
    compliant    BOOLEAN NOT NULL,
    findings     JSONB NOT NULL DEFAULT '[]'::jsonb,
    served_by    TEXT NOT NULL,
    reported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reports_time ON compliance_reports(reported_at DESC);

-- One row per replica. This is what proves the load balancer is actually
-- spreading work, and what shows traffic shifting after a replica is killed.
CREATE TABLE IF NOT EXISTS instance_activity (
    instance        TEXT PRIMARY KEY,
    reports_handled BIGINT NOT NULL DEFAULT 0,
    events_handled  BIGINT NOT NULL DEFAULT 0,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def _configure(conn: asyncpg.Connection) -> None:
    """Per-connection settings for the ingest workload.

    Compliance reports are high-frequency, self-repeating telemetry: every
    device re-reports its posture within seconds, so the newest report always
    supersedes the last. Waiting for a disk flush on each one costs roughly
    12ms per commit and makes the flush rate -- not the hardware -- the ceiling
    on fleet ingest.

    Relaxing the commit sync lets the database group commits together. The
    exposure is losing a fraction of a second of the most recent reports if the
    database server itself crashes; it is not a corruption risk, and the fleet
    re-reports that window on its next interval anyway. Policy definitions and
    device enrollment, where a lost write would actually matter, are held by
    other services and are not affected by this setting.
    """
    await conn.execute("SET synchronous_commit = off")


async def create_pool(dsn: str, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, setup=_configure
    )
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_ID)
        try:
            await conn.execute(SCHEMA)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", SCHEMA_LOCK_ID)
    return pool
