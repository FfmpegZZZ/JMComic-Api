"""FastAPI app factory + lifespan.

The lifespan:
1. configures structlog
2. installs the album-folder naming advice on jmcomic
3. loads ``option.yml`` and creates the jmcomic client
4. registers a SIGHUP handler that reloads ``option.yml`` and rebuilds the client
   (replaces the watchdog observer — SIGHUP is reliable, watchdog double-fired
    on editor atomic writes)
5. on shutdown, removes the SIGHUP handler
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from jmcomic_api import __version__
from jmcomic_api.deps import settings
from jmcomic_api.errors import register_handlers
from jmcomic_api.logging_config import configure as configure_logging
from jmcomic_api.logging_config import get_logger
from jmcomic_api.routers import catalog as catalog_router
from jmcomic_api.routers import health as health_router
from jmcomic_api.routers import pdf as pdf_router
from jmcomic_api.services.album import AlbumService
from jmcomic_api.services.jm_client import (
    install_album_dirname_advice,
    load_runtime,
)


def create_app() -> FastAPI:
    cfg = settings()
    configure_logging(cfg.log_format, cfg.log_level)
    logger = get_logger("jmcomic_api.app")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        install_album_dirname_advice()
        cfg.pdf_path.mkdir(parents=True, exist_ok=True)
        cfg.shard_cache_path.mkdir(parents=True, exist_ok=True)
        app.state.settings = cfg
        app.state.runtime = load_runtime(cfg.option_file)
        app.state.album_service = AlbumService(lambda: app.state.runtime)
        logger.info(
            "startup_complete",
            version=__version__,
            host=cfg.host,
            port=cfg.port,
            pdf_dir=str(cfg.pdf_path),
        )

        loop = asyncio.get_running_loop()

        def _reload_runtime() -> None:
            try:
                app.state.runtime = load_runtime(cfg.option_file)
                logger.info("option_reloaded", path=str(cfg.option_path))
            except Exception as e:
                logger.error("option_reload_failed", error=str(e))

        # SIGHUP handlers only work on the main thread of the main interpreter.
        # Skip silently in test harnesses that run the loop in a worker thread.
        signal_installed = False
        try:
            loop.add_signal_handler(signal.SIGHUP, _reload_runtime)
            signal_installed = True
        except (NotImplementedError, RuntimeError, ValueError) as e:
            logger.info("sighup_handler_skipped", reason=str(e))
        try:
            yield
        finally:
            if signal_installed:
                with contextlib.suppress(NotImplementedError, ValueError):
                    loop.remove_signal_handler(signal.SIGHUP)

    app = FastAPI(
        title="JMComic API",
        version=__version__,
        description="HTTP wrapper around the jmcomic library.",
        lifespan=lifespan,
    )

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_methods=["GET"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        start = time.monotonic()
        response: Response = await call_next(request)
        elapsed = time.monotonic() - start
        response.headers["x-request-id"] = request_id
        if elapsed >= cfg.slow_request_threshold_seconds:
            logger.warning(
                "slow_request",
                elapsed_seconds=round(elapsed, 3),
                status_code=response.status_code,
            )
        return response

    register_handlers(app)
    app.include_router(health_router.router)
    app.include_router(pdf_router.router)
    app.include_router(catalog_router.router)

    @app.get("/docs/external", include_in_schema=False)
    async def _legacy_docs_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("https://jm-api.apifox.cn", status_code=302)

    return app


app = create_app()
