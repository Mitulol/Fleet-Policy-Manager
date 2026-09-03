"""Distributed rate limiting.

A token bucket held in Redis and refilled continuously, so limits are shared
across every gateway process rather than being per-process. The check, refill
and decrement run inside a Lua script so the whole operation is atomic -- doing
it as separate reads and writes would let concurrent requests both observe the
same last token and both take it.

Buckets are keyed per API key, so one noisy device cannot exhaust the fleet's
allowance, and admins get a separate, smaller ceiling appropriate to a human or
a dashboard.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import redis.asyncio as redis

logger = logging.getLogger(__name__)

TOKEN_BUCKET_SCRIPT = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts     = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 3600)

return {allowed, math.floor(tokens)}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    limit: int
    retry_after: int


class RateLimiter:
    def __init__(self, client: redis.Redis, capacity: int, refill_per_second: float) -> None:
        self._client = client
        self._capacity = capacity
        self._rate = refill_per_second
        self._script = client.register_script(TOKEN_BUCKET_SCRIPT)

    async def check(self, identity: str) -> RateLimitResult:
        """Consume one token for this identity.

        Fails open. If Redis is unreachable the gateway keeps serving traffic
        unlimited rather than rejecting the entire fleet -- an outage in the
        limiter should degrade enforcement, not availability.
        """
        try:
            allowed, remaining = await self._script(
                keys=[f"ratelimit:{identity}"],
                args=[self._capacity, self._rate, time.time()],
            )
        except Exception:
            logger.warning("rate limiter unavailable, failing open")
            return RateLimitResult(
                allowed=True, remaining=self._capacity, limit=self._capacity, retry_after=0
            )

        return RateLimitResult(
            allowed=bool(allowed),
            remaining=int(remaining),
            limit=self._capacity,
            retry_after=0 if allowed else max(1, int(1 / self._rate) if self._rate else 1),
        )
