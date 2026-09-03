"""Fleet dashboard.

A thin read-only view of the platform. It has no database and no domain logic
of its own -- every number on the page comes from the API gateway, through the
same authenticated, rate-limited front door that a device or an operator's
script would use.

The gateway credential stays on this server. The browser calls this service,
this service calls the gateway, so the admin key is never shipped to the page.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

SERVICE_NAME = "fleet-dashboard"
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8000")
API_KEY = os.getenv("GATEWAY_API_KEY", "fleet-admin-key")
REFRESH_SECONDS = int(os.getenv("DASHBOARD_REFRESH_SECONDS", "2"))

templates = Jinja2Templates(directory="templates")
state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["http"] = httpx.AsyncClient(
        base_url=GATEWAY_URL,
        timeout=8.0,
        headers={"X-API-Key": API_KEY},
    )
    yield
    await state["http"].aclose()


app = FastAPI(title="Fleet Dashboard", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": SERVICE_NAME}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"refresh_seconds": REFRESH_SECONDS},
    )


@app.get("/api/summary")
async def summary() -> JSONResponse:
    """Fleet state for the page.

    If the gateway itself is unreachable the page is told so explicitly rather
    than being left to time out, so a platform outage renders as a visible
    banner instead of a frozen dashboard.
    """
    try:
        response = await state["http"].get("/api/fleet/summary")
        response.raise_for_status()
        return JSONResponse(content=response.json())
    except Exception as exc:
        return JSONResponse(
            status_code=200,
            content={
                "gateway_unreachable": True,
                "error": type(exc).__name__,
                "degraded": ["registry", "policy", "compliance"],
            },
        )


@app.get("/api/noncompliant")
async def noncompliant() -> JSONResponse:
    try:
        response = await state["http"].get(
            "/api/compliance/noncompliant", params={"limit": 25}
        )
        response.raise_for_status()
        return JSONResponse(content=response.json())
    except Exception:
        return JSONResponse(content=[])


@app.get("/api/policies")
async def policies() -> JSONResponse:
    try:
        response = await state["http"].get("/api/policy/policies")
        response.raise_for_status()
        return JSONResponse(content=response.json())
    except Exception:
        return JSONResponse(content=[])
