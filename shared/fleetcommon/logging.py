"""Structured JSON logging.

Every service logs one JSON object per line tagged with the service name and,
for the horizontally scaled Compliance Service, the instance id. That instance
tag is what makes it possible to see load spreading across replicas -- and to
see it re-spread after one is killed.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, instance: str | None = None) -> None:
        super().__init__()
        self.service = service
        self.instance = instance

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "message": record.getMessage(),
        }
        if self.instance:
            payload["instance"] = self.instance

        # Anything attached via logger.info("...", extra={"context": {...}}).
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(service: str, instance: str | None = None, level: str = "INFO") -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service, instance=instance))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Uvicorn's own access log duplicates what the services already record.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.error").handlers.clear()
    # httpx logs one line per outbound request at INFO. The gateway and
    # dashboard make a lot of those; their own logs already say what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return logging.getLogger(service)
