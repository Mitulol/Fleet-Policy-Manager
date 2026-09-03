"""Storage for the Device Registry Service.

The registry owns device identity and nothing else. Its SQLite file is private
to this service -- no other service opens it, and no other service is given a
connection string to it. Anything another service needs about a device it must
ask for over HTTP or learn from an event.
"""

from __future__ import annotations

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id                TEXT PRIMARY KEY,
    hostname          TEXT NOT NULL,
    os                TEXT NOT NULL,
    os_version        TEXT NOT NULL,
    device_group      TEXT NOT NULL,
    agent_version     TEXT NOT NULL,
    enrolled_at       TEXT NOT NULL,
    last_heartbeat_at TEXT,
    heartbeat_count   INTEGER NOT NULL DEFAULT 0,
    retired           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_devices_group ON devices(device_group);
CREATE INDEX IF NOT EXISTS idx_devices_heartbeat ON devices(last_heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_devices_retired ON devices(retired);
"""


async def connect(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    # Write-ahead logging lets the heartbeat write path and the dashboard's
    # read path proceed concurrently instead of serialising on a global lock.
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.executescript(SCHEMA)
    await conn.commit()
    return conn
