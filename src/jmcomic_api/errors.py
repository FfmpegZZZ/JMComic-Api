"""Map every error path to the legacy ``{success, message}`` envelope.

Three layers, all routed through ``_envelope``:
1. ``HTTPException`` raised inside routes (e.g. ``404`` for invalid shard index).
2. ``RequestValidationError`` from Pydantic / Query validation (422).
3. ``JmcomicException`` family from the upstream library.

A bare ``Exception`` handler is intentionally NOT registered — uvicorn's default
500 page and traceback logging is more useful than swallowing them silently.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jmcomic.jm_exception import (
    JmcomicException,
    MissingAlbumPhotoException,
    PartialDownloadFailedException,
    RequestRetryAllFailException,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from jmcomic_api.concurrency.keyed_lock import LockTimeout
from jmcomic_api.logging_config import get_logger

logger = get_logger(__name__)


def _envelope(message: str, status: int, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"success": False, "message": message}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _http_message(detail: Any) -> str:
    """``HTTPException.detail`` may be a string or any JSON-serialisable value."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and "message" in detail:
        return str(detail["message"])
    return str(detail)


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException):
        # Catches both starlette + fastapi HTTPException (fastapi's subclasses
        # starlette's). Rewrite ``{"detail": ...}`` → ``{"success": False, "message": ...}``.
        message = _http_message(exc.detail)
        # 404/422 are normal — info; everything else gets a warning.
        log = logger.info if exc.status_code < 500 else logger.warning
        log("http_error", status=exc.status_code, message=message)
        return _envelope(message, exc.status_code)

    @app.exception_handler(FastAPIHTTPException)
    async def _http_fastapi(request: Request, exc: FastAPIHTTPException):
        return await _http(request, exc)  # type: ignore[arg-type]

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError):
        # Compact summary: list the offending fields, not the full pydantic dump.
        fields = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            msg = err.get("msg", "invalid")
            fields.append(f"{loc}: {msg}")
        message = "validation failed: " + "; ".join(fields) if fields else "validation failed"
        logger.info("validation_error", fields=fields)
        return _envelope(message, 422, errors=exc.errors())

    @app.exception_handler(MissingAlbumPhotoException)
    async def _missing(_request: Request, exc: MissingAlbumPhotoException):
        logger.info("album_missing", error=str(exc))
        return _envelope("Album not found", 404)

    @app.exception_handler(RequestRetryAllFailException)
    async def _retry_fail(_request: Request, exc: RequestRetryAllFailException):
        logger.warning("upstream_unreachable", error=str(exc))
        return _envelope("Upstream JM site unreachable", 502)

    @app.exception_handler(PartialDownloadFailedException)
    async def _partial(_request: Request, exc: PartialDownloadFailedException):
        logger.warning("partial_download_failed", error=str(exc))
        resp = _envelope("Some images failed to download; try again later", 503)
        resp.headers["Retry-After"] = "30"
        return resp

    @app.exception_handler(JmcomicException)
    async def _jm(_request: Request, exc: JmcomicException):
        logger.error("jmcomic_error", error=str(exc), type=type(exc).__name__)
        return _envelope(f"Jmcomic error: {exc}", 500)

    @app.exception_handler(FileNotFoundError)
    async def _missing_file(_request: Request, exc: FileNotFoundError):
        logger.error("local_file_missing", error=str(exc))
        return _envelope("Required local file missing", 500)

    @app.exception_handler(TimeoutError)
    async def _timeout(_request: Request, exc: TimeoutError):
        # asyncio.TimeoutError IS TimeoutError on 3.12+. Covers wait_for budget
        # exhaustion in jm_client + album service.
        logger.warning("request_timeout", error=str(exc))
        resp = _envelope("Operation timed out — try again later", 504)
        resp.headers["Retry-After"] = "30"
        return resp

    @app.exception_handler(LockTimeout)
    async def _lock_timeout(_request: Request, exc: LockTimeout):
        # Same album was being processed by another in-flight request and we
        # waited too long. 503 + Retry-After signals "try again, the system
        # isn't broken — it's busy".
        logger.warning("lock_timeout", error=str(exc))
        resp = _envelope("Resource busy — try again later", 503)
        resp.headers["Retry-After"] = "30"
        return resp
