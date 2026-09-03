"""Storage for the Policy Service.

Private SQLite file, same ownership rule as the registry: only this service
reads or writes it.
"""

from __future__ import annotations

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    target_group  TEXT NOT NULL,
    settings      TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    published     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    deleted       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_policies_group ON policies(target_group);
CREATE INDEX IF NOT EXISTS idx_policies_deleted ON policies(deleted);

-- An append-only record of every rollout, so the dashboard can show what was
-- pushed and when without reconstructing it from the event stream.
CREATE TABLE IF NOT EXISTS rollouts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    target_group  TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    event_id      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rollouts_policy ON rollouts(policy_id);
"""


async def connect(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.executescript(SCHEMA)
    await conn.commit()
    return conn
