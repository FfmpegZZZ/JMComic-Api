"""Prometheus metrics. Single source of truth for all instrumented events.

All names use the ``jmapi_`` prefix and follow Prometheus naming conventions
(``*_total`` for counters, ``*_seconds`` for time histograms).

Wiring:
- :func:`http_middleware` is registered in ``app.py`` to count + time every
  request.
- Service code calls :func:`record_download_outcome`, :func:`record_pdf_build`
  etc. directly.
- :func:`mount` adds the ``/metrics`` exposition endpoint to a FastAPI app.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

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


# ---- helpers ----------------------------------------------------------------


def record_download_outcome(outcome: str) -> None:
    DOWNLOAD_TOTAL.labels(outcome=outcome).inc()


def record_pdf_build_duration(kind: str, seconds: float) -> None:
    PDF_BUILD_DURATION.labels(kind=kind).observe(seconds)


def record_cache(kind: str, hit: bool) -> None:
    (CACHE_HIT_TOTAL if hit else CACHE_MISS_TOTAL).labels(kind=kind).inc()


# ---- ASGI plumbing ---------------------------------------------------------


def _route_template(request: Request) -> str:
    """Use the route's path *template* (``/get_pdf/{album_id}``) instead of the
    concrete path so cardinality stays bounded.
    """
    route = request.scope.get("route")
    if route and getattr(route, "path", None):
        return route.path
    return request.url.path


async def http_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Count + time every HTTP request. Skip ``/metrics`` itself to avoid recursion."""
    if request.url.path == "/metrics":
        return await call_next(request)

    start = time.monotonic()
    response = await call_next(request)
    elapsed = time.monotonic() - start

    route = _route_template(request)
    status_class = f"{response.status_code // 100}xx"
    REQUEST_TOTAL.labels(method=request.method, route=route, status_class=status_class).inc()
    REQUEST_DURATION.labels(method=request.method, route=route).observe(elapsed)
    return response


def mount(app: FastAPI) -> None:
    """Register the ``/metrics`` route. Plain text exposition format."""

    @app.get("/metrics", include_in_schema=False)
    async def _metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
