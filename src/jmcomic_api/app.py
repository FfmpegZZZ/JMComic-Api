"""FastAPI app factory + lifespan.

The lifespan:
1. configures structlog
2. installs the album-folder naming advice on jmcomic
3. loads ``option.yml`` and creates the jmcomic client
4. registers a SIGHUP handler that reloads ``option.yml`` and rebuilds the client
   (replaces the watchdog observer — SIGHUP is reliable, watchdog double-fired
    on editor atomic writes)
5. on shutdown:
   - cancels every tracked in-flight task (so SIGTERM doesn't get downgraded
     to SIGKILL by the supervisor while a download is mid-flight)
   - removes the SIGHUP handler
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from jmcomic import JmSyncRuntime

from jmcomic_api import __version__, metrics
from jmcomic_api.concurrency.executors import BlockingPools
from jmcomic_api.concurrency.keyed_lock import KeyedAsyncLock
from jmcomic_api.concurrency.tasks import Lane, TaskManager
from jmcomic_api.deps import settings
from jmcomic_api.errors import register_handlers
from jmcomic_api.logging_config import configure as configure_logging
from jmcomic_api.logging_config import get_logger
from jmcomic_api.routers import auth as auth_router
from jmcomic_api.routers import catalog as catalog_router
from jmcomic_api.routers import health as health_router
from jmcomic_api.routers import pdf as pdf_router
from jmcomic_api.services.album import AlbumService, ReliabilityConfig
from jmcomic_api.services.auth import AuthRegistry, request_runtime
from jmcomic_api.services.jm_client import (
    install_album_dirname_advice,
    load_runtime,
)
from jmcomic_api.services.network import NetworkPolicy
from jmcomic_api.services.queries import QueryCache


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
        policy = NetworkPolicy()

        def load_configured_runtime():
            if cfg.auth_enabled:
                return load_runtime(cfg.option_file, anonymous=True, policy=policy)
            return load_runtime(cfg.option_file)

        app.state.runtime = await asyncio.to_thread(load_configured_runtime)
        Path(app.state.runtime.opt.dir_rule.base_dir).mkdir(parents=True, exist_ok=True)
        pools = JmSyncRuntime(photo_workers=cfg.chapter_threads, image_workers=cfg.image_threads)
        app.state.runtime.sync = pools
        blocking = BlockingPools(
            downloads=cfg.max_concurrent_downloads,
            metadata=cfg.max_concurrent_metadata,
            encoding=cfg.max_concurrent_base64,
        )
        app.state.blocking = blocking
        app.state.runtime.executors = blocking
        manager = TaskManager(wait_timeout=cfg.lock_acquire_timeout_seconds)
        app.state.manager = manager
        try:
            app.state.auth = (
                await asyncio.to_thread(
                    AuthRegistry, cfg, manager, blocking, pools, app.state.runtime.template, policy
                )
                if cfg.auth_enabled
                else None
            )
        except BaseException:
            app.state.runtime.retire()
            await asyncio.to_thread(pools.close)
            await asyncio.to_thread(blocking.close)
            raise
        app.state.metadata_lane = Lane(
            "metadata", cfg.max_concurrent_metadata, cfg.queue_capacity, cfg.queue_timeout_seconds
        )
        app.state.queries = QueryCache(manager, app.state.metadata_lane)
        app.state.base64_lane = Lane(
            "base64",
            cfg.max_concurrent_base64,
            max(cfg.queue_capacity, cfg.max_concurrent_base64_streams - cfg.max_concurrent_base64),
            cfg.queue_timeout_seconds,
        )
        app.state.base64_stream_lane = Lane(
            "base64_stream",
            cfg.max_concurrent_base64_streams,
            cfg.queue_capacity,
            cfg.queue_timeout_seconds,
        )

        # Reliability stack: global download semaphore + keyed lock + tunable
        # retry/timeout config. Constructing them here (not at import time)
        # keeps tests deterministic — each test process gets its own.
        app.state.download_semaphore = asyncio.Semaphore(cfg.max_concurrent_downloads)
        app.state.album_lock = KeyedAsyncLock()
        app.state.last_download_unix = None  # surfaced by /health/ready
        # Tracked tasks for graceful shutdown.
        app.state.tasks = manager.tasks

        def _mark_download_success() -> None:
            app.state.last_download_unix = time.time()

        app.state.album_service = AlbumService(
            lambda: request_runtime.get() or app.state.runtime,
            lock=app.state.album_lock,
            download_semaphore=app.state.download_semaphore,
            reliability=ReliabilityConfig(
                download_timeout_seconds=cfg.download_timeout_seconds,
                pdf_build_timeout_seconds=cfg.pdf_build_timeout_seconds,
                download_retry_attempts=cfg.download_retry_attempts,
                download_retry_initial_wait=cfg.download_retry_initial_wait,
                download_retry_max_wait=cfg.download_retry_max_wait,
            ),
            on_download_success=_mark_download_success,
            manager=manager,
            download_lane=Lane(
                "download",
                cfg.max_concurrent_downloads,
                cfg.queue_capacity,
                cfg.queue_timeout_seconds,
            ),
            pdf_lane=Lane(
                "pdf", cfg.max_concurrent_pdf_builds, cfg.queue_capacity, cfg.queue_timeout_seconds
            ),
            lock_timeout=cfg.lock_acquire_timeout_seconds,
            settings=cfg,
        )
        logger.info(
            "startup_complete",
            version=__version__,
            host=cfg.host,
            port=cfg.port,
            pdf_dir=str(cfg.pdf_path),
        )

        loop = asyncio.get_running_loop()

        reload_lock = asyncio.Lock()

        async def _reload():
            async with reload_lock:
                try:
                    loading = asyncio.create_task(asyncio.to_thread(load_configured_runtime))
                    try:
                        replacement = await asyncio.shield(loading)
                    except asyncio.CancelledError:
                        from jmcomic_api.concurrency.tasks import drain

                        await drain(loading)
                        if not loading.cancelled() and loading.exception() is None:
                            loading.result().retire()
                        raise
                    replacement.sync = pools
                    replacement.executors = blocking
                    if manager.closing:
                        replacement.retire()
                        return
                    old = app.state.runtime
                    app.state.runtime = replacement
                    old.retire()
                    app.state.queries.clear()
                    if app.state.auth is not None:
                        await app.state.auth.invalidate_runtimes(replacement.template)
                    logger.info("option_reloaded", path=str(cfg.option_path))
                except Exception as e:
                    logger.error("option_reload_failed", error=str(e))

        def _reload_runtime():
            if not manager.closing:
                manager.track(_reload(), name="reload")

        app.state.reload_runtime = _reload

        async def maintenance():
            from jmcomic_api.services.storage import cleanup

            while True:
                roots = [
                    cfg.pdf_path,
                    cfg.shard_cache_path,
                    Path(app.state.runtime.opt.dir_rule.base_dir).resolve(),
                ]
                try:
                    if app.state.auth is not None:
                        await app.state.auth.maintenance()
                    await asyncio.to_thread(
                        cleanup,
                        roots,
                        ttl=cfg.pdf_cache_ttl_seconds,
                        max_bytes=cfg.pdf_cache_max_mb * 1024**2,
                    )
                except OSError as error:
                    logger.warning("storage_cleanup_failed", error=str(error))
                await asyncio.sleep(cfg.cleanup_interval_seconds)

        maintenance_task = asyncio.create_task(maintenance(), name="storage-maintenance")

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
            maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance_task
            survivors = await manager.shutdown(cfg.shutdown_grace_seconds)
            if app.state.auth is not None:
                app.state.auth.close()
            app.state.runtime.retire()

            # A cooperative upstream thread may outlive the grace period. Its
            # runtime leases and pools stay alive until it really exits.
            async def close_pools():
                from jmcomic_api.concurrency.tasks import drain

                for task in tuple(manager.tasks):
                    await drain(task)
                if app.state.auth is not None:
                    app.state.auth.close()
                await drain(asyncio.create_task(asyncio.to_thread(pools.close)))
                await drain(asyncio.create_task(asyncio.to_thread(blocking.close)))

            pool_cleanup = asyncio.create_task(close_pools(), name="pool-cleanup")
            if not survivors:
                await pool_cleanup
            logger.info("lifespan_shutdown_drained", still_running=survivors)
            if signal_installed:
                with contextlib.suppress(NotImplementedError, ValueError):
                    loop.remove_signal_handler(signal.SIGHUP)
            logger.info("lifespan_shutdown_complete")

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
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["*"],
        )

    app.add_middleware(
        metrics.ObservabilityMiddleware, slow_threshold=cfg.slow_request_threshold_seconds
    )
    metrics.mount(app)

    register_handlers(app)
    app.include_router(health_router.router)
    app.include_router(pdf_router.router)
    app.include_router(catalog_router.router)
    app.include_router(auth_router.router)

    @app.get("/docs/external", include_in_schema=False)
    async def _legacy_docs_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("https://jm-api.apifox.cn", status_code=302)

    return app


app = create_app()
