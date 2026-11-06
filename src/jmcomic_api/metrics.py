"""Prometheus metrics. Single source of truth for all instrumented events.

All names use the ``jmapi_`` prefix and follow Prometheus naming conventions
(``*_total`` for counters, ``*_seconds`` for time histograms).

Wiring:
- :class:`ObservabilityMiddleware` is registered in ``app.py`` to count + time every
  request.
- Service code calls :func:`record_download_outcome`, :func:`record_pdf_build`
  etc. directly.
- :func:`mount` adds the ``/metrics`` exposition endpoint to a FastAPI app.
"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import FastAPI
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

from jmcomic_api.logging_config import get_logger

# ---- request-level -----------------------------------------------------------

REQUEST_TOTAL = Counter(
    "jmapi_request_total",
    "HTTP requests by route + status class.",
    ["method", "route", "status_class"],
)
REQUEST_DURATION = Histogram(
    "jmapi_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)

# ---- service-level -----------------------------------------------------------

DOWNLOAD_TOTAL = Counter(
    "jmapi_download_total",
    "Album downloads by outcome.",
    ["outcome"],  # success | partial_fail | retry_exhausted | timeout
)
PDF_BUILD_DURATION = Histogram(
    "jmapi_pdf_build_duration_seconds",
    "PDF build latency.",
    ["kind"],  # full | shard
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
CACHE_HIT_TOTAL = Counter(
    "jmapi_cache_hit_total",
    "Cache hits by kind (album webp / pdf / shard).",
    ["kind"],
)
CACHE_MISS_TOTAL = Counter(
    "jmapi_cache_miss_total",
    "Cache misses by kind.",
    ["kind"],
)

TASKS = Gauge("jmapi_tasks", "Owned jobs by lane and state.", ["lane", "state"])
QUEUE_DURATION = Histogram("jmapi_queue_wait_seconds", "Admission wait.", ["lane"])
CANCELLATIONS = Counter("jmapi_cancellations_total", "Cooperative cancellations.", ["kind"])
BUILD_FAILURES = Counter("jmapi_build_failures_total", "Failed PDF builds.", ["reason"])
DISK_REJECTIONS = Counter("jmapi_disk_rejections_total", "Disk admission failures.")
UPSTREAM_REQUESTS = Counter("jmapi_upstream_requests_total", "HTTP attempts.", ["outcome"])
UPSTREAM_DURATION = Histogram("jmapi_upstream_request_seconds", "HTTP attempt duration.")
SEND_TIMEOUTS = Counter(
    "jmapi_response_send_timeouts_total", "Response sends exceeding idle timeout."
)


# ---- helpers ----------------------------------------------------------------


def record_download_outcome(outcome: str) -> None:
    DOWNLOAD_TOTAL.labels(outcome=outcome).inc()


def record_pdf_build_duration(kind: str, seconds: float) -> None:
    PDF_BUILD_DURATION.labels(kind=kind).observe(seconds)


def record_cache(kind: str, hit: bool) -> None:
    (CACHE_HIT_TOTAL if hit else CACHE_MISS_TOTAL).labels(kind=kind).inc()


# ---- ASGI plumbing ---------------------------------------------------------


RESPONSE_DURATION = Histogram(
    "jmapi_response_duration_seconds",
    "Full response send latency.",
    ["method", "route"],
    buckets=(0.05, 0.1, 0.2, 0.5, 1, 5, 30, 120, 600),
)
RESPONSE_OUTCOMES = Counter("jmapi_response_outcomes_total", "Response completion.", ["outcome"])


class ObservabilityMiddleware:
    def __init__(self, app, slow_threshold=5):
        self.app, self.slow_threshold = app, slow_threshold
        self.logger = get_logger(__name__)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/metrics":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        request_id = headers.get(b"x-request-id", uuid.uuid4().hex[:12].encode())
        tokens = structlog.contextvars.bind_contextvars(
            request_id=request_id.decode("latin-1"),
            method=scope["method"],
            path=scope["path"],
        )
        start = time.monotonic()
        status_code = 500
        processing = None
        outcome = "error"
        finished = False

        async def observed_receive():
            nonlocal outcome
            message = await receive()
            if message["type"] == "http.disconnect":
                outcome = "disconnect"
            return message

        async def observed_send(message):
            nonlocal status_code, processing, outcome, finished
            if message["type"] == "http.response.start":
                status_code = message["status"]
                processing = time.monotonic() - start
                message = dict(message)
                message["headers"] = [*message.get("headers", []), (b"x-request-id", request_id)]
            try:
                await send(message)
            except OSError:
                outcome = "disconnect"
                raise
            if message["type"] in (
                "http.response.body",
                "http.response.pathsend",
            ) and not message.get("more_body", False):
                finished = True
                outcome = "complete"

        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            elapsed = time.monotonic() - start
            route = scope.get("route")
            template = getattr(route, "path", "__unmatched__")
            REQUEST_TOTAL.labels(scope["method"], template, f"{status_code // 100}xx").inc()
            REQUEST_DURATION.labels(scope["method"], template).observe(
                processing if processing is not None else elapsed
            )
            RESPONSE_DURATION.labels(scope["method"], template).observe(elapsed)
            RESPONSE_OUTCOMES.labels(
                outcome if finished or outcome == "disconnect" else "error"
            ).inc()
            if elapsed >= self.slow_threshold:
                self.logger.warning(
                    "slow_request", elapsed_seconds=round(elapsed, 3), status_code=status_code
                )
            structlog.contextvars.reset_contextvars(**tokens)


def mount(app: FastAPI) -> None:
    """Register the ``/metrics`` route. Plain text exposition format."""

    @app.get("/metrics", include_in_schema=False)
    async def _metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
