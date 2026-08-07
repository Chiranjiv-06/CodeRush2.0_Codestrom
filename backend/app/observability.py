"""Prometheus metrics + structured logging."""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Callable

from fastapi import Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

REGISTRY = CollectorRegistry(auto_describe=True)

http_requests_total = Counter(
    "m2x_http_requests_total", "HTTP requests", ["method", "path", "status"], registry=REGISTRY
)
http_request_duration = Histogram(
    "m2x_http_request_duration_seconds",
    "HTTP latency",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)
jobs_total = Counter("m2x_jobs_total", "Jobs by terminal status", ["status"], registry=REGISTRY)
jobs_active = Gauge("m2x_jobs_active", "Jobs currently running or queued", registry=REGISTRY)
job_duration = Histogram(
    "m2x_job_duration_seconds",
    "Job wall time",
    ["runtime"],
    buckets=(0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300),
    registry=REGISTRY,
)
payments_total = Counter(
    "m2x_payments_total", "x402 payment outcomes", ["status", "network"], registry=REGISTRY
)
payment_volume_micros = Counter(
    "m2x_payment_volume_micros_total", "Settled volume in micro-USDC", registry=REGISTRY
)
refunds_total = Counter("m2x_refunds_total", "Refunds issued", ["reason"], registry=REGISTRY)
disputes_total = Counter("m2x_disputes_total", "Disputes by status", ["status"], registry=REGISTRY)
integrity_checks = Counter(
    "m2x_integrity_checks_total", "SHA-256 verifications", ["result"], registry=REGISTRY
)
workers_spawned = Counter(
    "m2x_workers_spawned_total", "Sandbox workers spawned", ["backend"], registry=REGISTRY
)
workers_active = Gauge("m2x_workers_active", "Live sandbox workers", registry=REGISTRY)
workers_reaped = Counter("m2x_workers_reaped_total", "Workers cleaned up", registry=REGISTRY)
bazaar_discoveries = Counter(
    "m2x_bazaar_discoveries_total", "Bazaar discovery calls", ["source", "result"], registry=REGISTRY
)
zerion_requests = Counter(
    "m2x_zerion_requests_total", "Zerion provider requests",
    ["capability", "transport", "status"], registry=REGISTRY,
)
zerion_latency = Histogram(
    "m2x_zerion_latency_seconds",
    "Zerion provider round-trip latency",
    ["capability", "transport"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
    registry=REGISTRY,
)
zerion_payments = Counter(
    "m2x_zerion_payments_total", "Zerion-side payment outcomes",
    ["rail", "status"], registry=REGISTRY,
)
zerion_spend_micros = Counter(
    "m2x_zerion_spend_micros_total", "Micro-USDC spent on the Zerion rail", registry=REGISTRY
)
agent_runs = Counter("m2x_agent_runs_total", "Agent plan runs", ["status"], registry=REGISTRY)
agent_steps = Counter("m2x_agent_steps_total", "Agent graph node executions", ["node"], registry=REGISTRY)
scheduler_ticks = Counter("m2x_scheduler_ticks_total", "Scheduler loop iterations", registry=REGISTRY)
reputation_gauge = Gauge(
    "m2x_provider_reputation", "Provider reputation score", ["provider"], registry=REGISTRY
)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for field in ("request_id", "user_id", "job_id"):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s"))
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    for noisy in ("uvicorn.access", "httpx", "watchfiles"):
        logging.getLogger(noisy).setLevel("WARNING")


logger = logging.getLogger("m2x")


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #
def _normalize(path: str) -> str:
    """Collapse ids so metric cardinality stays bounded."""
    parts = []
    for seg in path.split("/"):
        if "_" in seg and len(seg) > 12:
            parts.append(f":{seg.split('_')[0]}")
        else:
            parts.append(seg)
    return "/".join(parts)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            logger.exception("unhandled error", extra={"request_id": request_id})
            raise
        finally:
            elapsed = time.perf_counter() - started
            path = _normalize(request.url.path)
            if settings.metrics_enabled and not path.startswith("/metrics"):
                http_request_duration.labels(request.method, path).observe(elapsed)
                http_requests_total.labels(
                    request.method, path, str(locals().get("status_code", 500))
                ).inc()
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed * 1000:.2f}"
        return response
