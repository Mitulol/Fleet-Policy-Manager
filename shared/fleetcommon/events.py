"""Asynchronous event bus built on Redis streams.

Why streams and consumer groups rather than plain publish/subscribe:

Plain publish/subscribe fans every message out to every live subscriber and
keeps no history. That is fine while exactly one Compliance Service is running.
The moment that service is scaled horizontally -- which is the whole point of
the high-availability design here -- fan-out becomes a correctness bug: three
replicas each apply the same policy rollout, and any replica that happens to be
restarting when an event lands misses it permanently.

A consumer group fixes both problems. The stream is durable, so an event
published while a replica is down is still waiting when it comes back, and the
group guarantees each event is delivered to exactly one member. Work that a
replica claimed but died before acknowledging is reclaimed by its peers through
the idle-message reclaim below, so a mid-rollout failure costs latency rather
than data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis

from .config import EVENT_STREAM

logger = logging.getLogger(__name__)

# Event type constants, so publisher and consumer cannot drift apart.
POLICY_PUBLISHED = "policy.published"
POLICY_UPDATED = "policy.updated"
POLICY_DELETED = "policy.deleted"

# Cap the stream so a long-running demo cannot grow memory without bound.
MAX_STREAM_LENGTH = 10_000


def build_event(event_type: str, source: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "type": event_type,
        "source": source,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


class EventPublisher:
    """Writes domain events onto the shared stream."""

    def __init__(self, client: redis.Redis, source: str, stream: str = EVENT_STREAM) -> None:
        self._client = client
        self._source = source
        self._stream = stream

    async def publish(self, event_type: str, payload: dict[str, Any]) -> str:
        event = build_event(event_type, self._source, payload)
        await self._client.xadd(
            self._stream,
            {"data": json.dumps(event)},
            maxlen=MAX_STREAM_LENGTH,
            approximate=True,
        )
        logger.info(
            "published event",
            extra={"context": {"event_type": event_type, "event_id": event["event_id"]}},
        )
        return str(event["event_id"])


class EventConsumer:
    """Consumes the shared stream as one member of a named consumer group.

    Runs as a background task inside the service. Each iteration first reclaims
    events that another member took but never acknowledged -- the signature of a
    replica that was killed mid-handler -- then reads events new to the group.
    """

    def __init__(
        self,
        client: redis.Redis,
        group: str,
        consumer: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        stream: str = EVENT_STREAM,
        block_ms: int = 5000,
        batch_size: int = 50,
        reclaim_idle_ms: int = 30_000,
    ) -> None:
        self._client = client
        self._group = group
        self._consumer = consumer
        self._handler = handler
        self._stream = stream
        self._block_ms = block_ms
        self._batch_size = batch_size
        self._reclaim_idle_ms = reclaim_idle_ms
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def ensure_group(self) -> None:
        """Create the stream and group if this is the first service to start."""
        try:
            await self._client.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
            logger.info("created consumer group", extra={"context": {"group": self._group}})
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            # Another replica created it first, which is the normal case.

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await self.ensure_group()
        logger.info(
            "event consumer started",
            extra={"context": {"group": self._group, "consumer": self._consumer}},
        )
        while not self._stopping.is_set():
            try:
                await self._reclaim_abandoned()
                await self._read_new()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("event consumer iteration failed")
                await asyncio.sleep(1.0)

    async def _reclaim_abandoned(self) -> None:
        """Take over events a peer claimed but never acknowledged."""
        try:
            response = await self._client.xautoclaim(
                self._stream,
                self._group,
                self._consumer,
                min_idle_time=self._reclaim_idle_ms,
                start_id="0-0",
                count=self._batch_size,
            )
        except redis.ResponseError:
            return

        # Redis 7 returns (cursor, messages, deleted); Redis 6.2 omits the
        # deleted list. Read positionally so either server version works.
        if not response or len(response) < 2:
            return
        messages = response[1]

        if messages:
            logger.info(
                "reclaimed abandoned events",
                extra={"context": {"count": len(messages)}},
            )
            await self._dispatch(messages)

    async def _read_new(self) -> None:
        response = await self._client.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},
            count=self._batch_size,
            block=self._block_ms,
        )
        if not response:
            return
        for _stream_name, messages in response:
            await self._dispatch(messages)

    async def _dispatch(self, messages: list[tuple[str, dict[str, str]]]) -> None:
        for message_id, fields in messages:
            raw = fields.get("data")
            if not raw:
                # Malformed entry; acknowledge so it does not block the group.
                await self._client.xack(self._stream, self._group, message_id)
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                await self._client.xack(self._stream, self._group, message_id)
                continue

            try:
                await self._handler(event)
            except Exception:
                # Leave it unacknowledged so a peer reclaims and retries it.
                logger.exception(
                    "event handler failed",
                    extra={"context": {"event_id": event.get("event_id")}},
                )
                continue

            await self._client.xack(self._stream, self._group, message_id)


def make_redis(url: str) -> redis.Redis:
    return redis.from_url(url, decode_responses=True)
