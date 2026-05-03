"""Adapter around jmcomic. Centralizes all upstream calls so:
- they can be mocked in one place
- sync calls run inside ``asyncio.to_thread`` (so the event loop never blocks)
- ``asyncio.wait_for`` enforces request timeouts
- ``tenacity`` handles transient failures (Partial / RetryAll) at the app layer
- a future jmcomic API change touches only this file
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jmcomic import (
    JmAlbumDetail,
    JmApiClient,
    JmCategoryPage,
    JmModuleConfig,
    JmSearchPage,
    create_option_by_file,
    download_album,
)
from jmcomic.jm_exception import (
    PartialDownloadFailedException,
    RequestRetryAllFailException,
)
from tenacity import (
    RetryCallState,
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from jmcomic_api.logging_config import get_logger

logger = get_logger(__name__)


def _log_before_sleep(state: RetryCallState) -> None:
    """structlog-friendly replacement for tenacity.before_sleep_log."""
    exc = state.outcome.exception() if state.outcome else None
    logger.info(
        "download_retry_scheduled",
        attempt=state.attempt_number,
        next_wait_seconds=getattr(state.next_action, "sleep", None),
        exception=type(exc).__name__ if exc else None,
        message=str(exc) if exc else None,
    )


# Exceptions that signal *transient* failure — worth retrying. Anything else
# (Missing/Json/Regular) is permanent at the level of a single album lookup.
RETRYABLE_DOWNLOAD_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PartialDownloadFailedException,
    RequestRetryAllFailException,
)


@dataclass
class JmRuntime:
    """Holds the live jmcomic option + client. Rebuilt by ``reload()``."""

    opt: Any
    client: JmApiClient


def install_album_dirname_advice() -> None:
    """Force downloaded album folders to be named ``[id]title`` regardless of
    the user's ``dir_rule`` shortcut. Must run before any download.
    """
    JmModuleConfig.AFIELD_ADVICE["jmbook"] = lambda album: f"[{album.id}]{album.title}"


def load_runtime(option_file: str | Path) -> JmRuntime:
    opt = create_option_by_file(str(option_file))
    return JmRuntime(opt=opt, client=opt.new_jm_client())


# ---- thin async wrappers (each runs in default thread pool) ----------------


async def _bounded(coro, timeout: float):
    """Common wrapper: ``asyncio.wait_for`` + structured error logging."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        logger.warning("upstream_timeout", timeout_seconds=timeout)
        raise


async def search(
    client: JmApiClient, query: str, page: int, *, timeout: float = 30
) -> JmSearchPage:
    return await _bounded(
        asyncio.to_thread(client.search_site, search_query=query, page=page), timeout
    )


async def get_album_detail(
    client: JmApiClient, album_id: str, *, timeout: float = 30
) -> JmAlbumDetail | None:
    """May return ``None`` if the upstream lookup yields no album."""
    return await _bounded(asyncio.to_thread(client.get_album_detail, album_id), timeout)


async def categories_filter(
    client: JmApiClient,
    *,
    page: int,
    time: str,
    category: str,
    order_by: str,
    timeout: float = 30,
) -> JmCategoryPage:
    return await _bounded(
        asyncio.to_thread(
            client.categories_filter,
            page=page,
            time=time,
            category=category,
            order_by=order_by,
        ),
        timeout,
    )


# ---- download with retry ---------------------------------------------------


def _build_retrying_download(max_attempts: int, initial_wait: float, max_wait: float):
    """Build a ``tenacity``-wrapped synchronous download_album call. Closure
    style so callers can plug different retry budgets per environment.
    """

    @retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=initial_wait, max=max_wait),
        retry=retry_if_exception_type(RETRYABLE_DOWNLOAD_EXCEPTIONS),
        before_sleep=_log_before_sleep,
        reraise=True,
    )
    def _do(album_id: str, opt: Any):
        return download_album(album_id, option=opt)

    return _do


async def download_with_retry(
    album_id: str,
    opt: Any,
    *,
    timeout: float = 600,
    max_attempts: int = 3,
    initial_wait: float = 2,
    max_wait: float = 30,
) -> tuple[Any, Iterable[Any]]:
    """Download an album with retries + per-call timeout.

    On retry-exhausted, the underlying retryable exception is re-raised
    (``reraise=True``) so downstream handlers map it to 502/503 as appropriate.
    """
    blocking = _build_retrying_download(max_attempts, initial_wait, max_wait)
    try:
        return await _bounded(asyncio.to_thread(blocking, album_id, opt), timeout)
    except RetryError as e:
        # tenacity wraps the underlying when reraise=False; with reraise=True we
        # shouldn't get here — but be defensive.
        if e.last_attempt and e.last_attempt.failed:
            inner = e.last_attempt.exception()
            if inner is not None:
                raise inner from e
        raise


# Legacy alias kept so older callers (and tests) still work.
download = download_with_retry
