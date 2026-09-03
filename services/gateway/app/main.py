"""API Gateway.

The single front door to the platform. Every caller -- device agent, operator,
dashboard -- comes through here, and nothing behind it is exposed directly.
It handles four things the backend services deliberately do not:

  routing        -- one public URL space mapped onto three private services
  authentication -- API keys resolved to a role
  authorisation  -- device credentials scoped to what a device legitimately does
  rate limiting  -- a shared token bucket, so limits hold across gateway processes

Compliance traffic is routed at a load balancer rather than a single container,
so the gateway is unaware of how many Compliance replicas exist and needs no
redeploy when that number changes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from fleetcommon.config import env_float, env_int, env_list, env_str
from fleetcommon.events import make_redis
from fleetcommon.logging import configure_logging

from .auth import KeyStore, authorize
from .ratelimit import RateLimiter

SERVICE_NAME = "api-gateway"

REGISTRY_URL = env_str("REGISTRY_URL", "http://registry:8001")
POLICY_URL = env_str("POLICY_URL", "http://policy:8002")
COMPLIANCE_URL = env_str("COMPLIANCE_URL", "http://compliance-lb:8080")
REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")

ADMIN_KEYS = env_list("GATEWAY_ADMIN_KEYS", "fleet-admin-key")
DEVICE_KEYS = env_list("GATEWAY_DEVICE_KEYS", "fleet-device-key")

DEVICE_RATE_CAPACITY = env_int("RATE_LIMIT_DEVICE_BURST", 240)
DEVICE_RATE_PER_SECOND = env_float("RATE_LIMIT_DEVICE_PER_SECOND", 120.0)
ADMIN_RATE_CAPACITY = env_int("RATE_LIMIT_ADMIN_BURST", 120)
ADMIN_RATE_PER_SECOND = env_float("RATE_LIMIT_ADMIN_PER_SECOND", 60.0)

UPSTREAM_TIMEOUT = env_float("UPSTREAM_TIMEOUT_SECONDS", 10.0)

# Public path prefix to the private service that owns it.
ROUTES: dict[str, str] = {
    "registry": REGISTRY_URL,
    "policy": POLICY_URL,
    "compliance": COMPLIANCE_URL,
}

logger = configure_logging(SERVICE_NAME)
state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["http"] = httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,
        # The fleet simulator drives hundreds of concurrent devices through
        # this one client, so the default connection ceiling is raised.
        limits=httpx.Limits(max_connections=500, max_keepalive_connections=200),
    )
    state["redis"] = make_redis(REDIS_URL)
    state["keys"] = KeyStore(ADMIN_KEYS, DEVICE_KEYS)
    state["device_limiter"] = RateLimiter(
        state["redis"], DEVICE_RATE_CAPACITY, DEVICE_RATE_PER_SECOND
    )
    state["admin_limiter"] = RateLimiter(
        state["redis"], ADMIN_RATE_CAPACITY, ADMIN_RATE_PER_SECOND
    )
    logger.info(
        "gateway ready",
        extra={"context": {"routes": list(ROUTES), "api_keys": len(state["keys"])}},
    )
    yield
    await state["http"].aclose()
    await state["redis"].aclose()


app = FastAPI(
    title="Fleet Policy Manager API Gateway",
    description="Routing, authentication and rate limiting for the fleet services.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> JSONResponse:
    """Aggregate health.

    Reports each service separately and stays 200 while any are reachable, so
    a single failed service shows up as a degraded component rather than
    presenting the whole platform as down.
    """
    async def probe(name: str, base_url: str) -> tuple[str, dict[str, Any]]:
        try:
            response = await state["http"].get(f"{base_url}/health", timeout=3.0)
            return name, {
                "reachable": response.status_code == 200,
                "status_code": response.status_code,
            }
        except Exception as exc:
            return name, {"reachable": False, "error": type(exc).__name__}

    results = dict(await asyncio.gather(*(probe(n, u) for n, u in ROUTES.items())))
    healthy = sum(1 for r in results.values() if r["reachable"])

    if healthy == len(ROUTES):
        status, code = "healthy", 200
    elif healthy > 0:
        status, code = "degraded", 200
    else:
        status, code = "unavailable", 503

    return JSONResponse(
        status_code=code,
        content={"status": status, "service": SERVICE_NAME, "services": results},
    )


def _authenticate(api_key: str | None, method: str, path: str):
    principal = state["keys"].resolve(api_key)
    if principal is None:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    if not authorize(principal, method, path):
        raise HTTPException(
            status_code=403,
            detail="this credential is not permitted to call that route",
        )
    return principal


async def _enforce_rate_limit(principal) -> None:
    limiter = state["admin_limiter"] if principal.is_admin else state["device_limiter"]
    result = await limiter.check(principal.key_id)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={
                "Retry-After": str(result.retry_after),
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": "0",
            },
        )


@app.get("/api/fleet/summary")
async def fleet_summary(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> JSONResponse:
    """One aggregated view of the fleet for the dashboard.

    Declared before the catch-all proxy route below so it is matched first;
    otherwise "fleet" would be treated as an unknown backend service. Fans out
    to all three services concurrently and tolerates any of them being down: a
    failed service becomes an unavailable section of the response instead of a
    failed page load.
    """
    principal = _authenticate(x_api_key, "GET", "/api/fleet/summary")
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin credential required")
    await _enforce_rate_limit(principal)

    async def fetch(name: str, url: str) -> tuple[str, dict[str, Any]]:
        try:
            response = await state["http"].get(url, timeout=5.0)
            response.raise_for_status()
            return name, {"available": True, "data": response.json()}
        except Exception as exc:
            logger.warning(
                "summary section unavailable",
                extra={"context": {"section": name, "error": type(exc).__name__}},
            )
            return name, {"available": False, "error": type(exc).__name__, "data": None}

    sections = dict(
        await asyncio.gather(
            fetch("registry", f"{REGISTRY_URL}/stats"),
            fetch("policy", f"{POLICY_URL}/stats"),
            fetch("compliance", f"{COMPLIANCE_URL}/stats"),
        )
    )

    return JSONResponse(
        content={
            "generated_at": time.time(),
            "degraded": [n for n, s in sections.items() if not s["available"]],
            **sections,
        }
    )


@app.api_route(
    "/api/{service}/{upstream_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy(
    service: str,
    upstream_path: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    base_url = ROUTES.get(service)
    if base_url is None:
        raise HTTPException(status_code=404, detail=f"unknown service '{service}'")

    principal = _authenticate(x_api_key, request.method, request.url.path)
    await _enforce_rate_limit(principal)

    # A correlation id is minted here and passed down, so one device's request
    # can be followed across every service it touches.
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    body = await request.body()
    started = time.perf_counter()

    try:
        upstream = await state["http"].request(
            method=request.method,
            url=f"{base_url}/{upstream_path}",
            params=dict(request.query_params),
            content=body,
            headers={
                "content-type": request.headers.get("content-type", "application/json"),
                "X-Request-ID": request_id,
                "X-Forwarded-Role": principal.role,
            },
        )
    except httpx.TimeoutException:
        logger.warning(
            "upstream timed out",
            extra={"context": {"service": service, "path": upstream_path, "request_id": request_id}},
        )
        raise HTTPException(status_code=504, detail=f"{service} service timed out") from None
    except httpx.RequestError as exc:
        logger.warning(
            "upstream unreachable",
            extra={"context": {"service": service, "error": str(exc), "request_id": request_id}},
        )
        raise HTTPException(
            status_code=503, detail=f"{service} service unavailable"
        ) from None

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    headers = {"X-Request-ID": request_id, "X-Upstream-Latency-Ms": str(elapsed_ms)}
    # Surfaces which Compliance replica served the call, which is how the load
    # balancer's behaviour is observed from outside the cluster.
    if "X-Served-By" in upstream.headers:
        headers["X-Served-By"] = upstream.headers["X-Served-By"]

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type", "application/json"),
    )
