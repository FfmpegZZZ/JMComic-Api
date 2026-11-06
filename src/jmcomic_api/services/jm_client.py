"""Upstream adapter: owned synchronous workers and cooperative download control."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jmcomic import (
    DownloadControl,
    JmModuleConfig,
    JmOption,
    JmSyncRuntime,
    download_album,
    jm_task_context,
)
from jmcomic.jm_exception import (
    DownloadCancelledException,
    PartialDownloadFailedException,
    RequestRetryAllFailException,
)

from jmcomic_api import metrics
from jmcomic_api.concurrency.executors import BlockingPools, run_blocking
from jmcomic_api.concurrency.tasks import BusyError, Job, drain
from jmcomic_api.logging_config import get_logger
from jmcomic_api.services.network import NetworkPolicy, UpstreamSessionExpired

logger = get_logger(__name__)
RETRYABLE_DOWNLOAD_EXCEPTIONS = (PartialDownloadFailedException, RequestRetryAllFailException)


@dataclass
class JmRuntime:
    opt: Any
    client: Any
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    sync: Any = None  # Application-owned chapter/image pools, shared across reloads.
    executors: BlockingPools | None = None
    identity: str = "anonymous"
    auth_fetched_at: float | None = field(default=None, repr=False)
    template: dict | None = field(default=None, repr=False)
    _users: int = 0
    _retired: bool = False
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def retain(self):
        with self._lock:
            self._users += 1

    def release(self):
        with self._lock:
            self._users -= 1
            close = self._retired and self._users == 0
        if close:
            self.close()

    def retire(self):
        with self._lock:
            self._retired = True
            close = self._users == 0
        if close:
            self.close()

    def close(self):
        # Download and metadata clients have separate sessions/cookies.
        clients = (self.client, getattr(self.opt, "__dict__", {}).get("__cache_build_jm_client__"))
        for client in clients:
            postman = getattr(client, "postman", None)
            session = getattr(postman, "session", None)
            if session is not None:
                with contextlib.suppress(Exception):
                    session.close()
                postman.session = None  # release thread-local Curl handles as well


def install_album_dirname_advice() -> None:
    from jmcomic_api.services.pdf_paths import sanitize_filename

    JmModuleConfig.AFIELD_ADVICE["jmbook"] = lambda album: (
        f"[{album.id}]{sanitize_filename(album.title)}"
    )


def load_runtime(option_file: str | Path, *, anonymous: bool = False, policy=None) -> JmRuntime:
    with open(option_file, encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    return runtime_from_data(data, option_file, anonymous=anonymous, policy=policy)


def runtime_from_data(
    data: dict, option_file: str | Path, *, anonymous=False, policy=None
) -> JmRuntime:
    data = copy.deepcopy(data)
    if anonymous:
        data["auth"] = {}
        # Managed identities must never inherit credentials or login hooks.
        data["plugins"] = data.get("plugins") or {}
        data["plugins"]["after_init"] = []
        meta = data.setdefault("client", {}).setdefault("postman", {}).setdefault("meta_data", {})
        meta.pop("cookies", None)
        if isinstance(meta.get("headers"), dict):
            meta["headers"] = {
                k: v
                for k, v in meta["headers"].items()
                if k.lower() not in ("cookie", "authorization")
            }
    client_config = data.setdefault("client", {})
    client_config.setdefault("retry_times", 1)
    postman = client_config.setdefault("postman", {})
    postman.setdefault("type", "curl_cffi_session")
    metadata = postman.setdefault("meta_data", {})
    metadata.setdefault("timeout", (5, 25))
    if isinstance(metadata["timeout"], list):
        metadata["timeout"] = tuple(metadata["timeout"])
    data.setdefault("filepath", str(option_file))
    template = copy.deepcopy(data)
    opt = JmOption.construct(data)
    try:
        # Pre-create both clients in the loader thread; request handlers never
        # initialise sessions (which may perform an upstream /setting call).
        policy = policy or NetworkPolicy()  # download + metadata share host cooldowns
        client = opt.new_jm_client(domain_retry_strategy=policy)
        downloads = opt.build_jm_client(domain_retry_strategy=policy)
        # A login plugin may already have populated the option's client cache.
        downloads.domain_retry_strategy = policy
    except BaseException:
        cached = opt.__dict__.get("__cache_build_jm_client__")
        for obj in (locals().get("client"), cached):
            session = getattr(getattr(obj, "postman", None), "session", None)
            if session is not None:
                session.close()
        raise
    return JmRuntime(opt=opt, client=client, template=template)


def login_runtime(
    option_file, username, password, *, cookies=None, user_id=None, template=None, policy=None
):
    """Create an isolated API identity. Passwords never enter JmOption/plugin logs."""
    if template is None:
        with open(option_file, encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
    else:
        data = copy.deepcopy(template)
    data["log"] = False
    data.setdefault("client", {})["impl"] = "api"
    runtime = runtime_from_data(data, option_file, anonymous=True, policy=policy)
    try:
        if cookies is None:
            with jm_task_context(jmapi_no_retry=True):
                response = runtime.client.login(username, password)
            profile = response.res_data
            user_id = profile.get("uid")
            if user_id is None:
                raise ValueError("Missing JM user identity")
            cookies = dict(runtime.client["cookies"])
        else:
            profile = None
            runtime.client["cookies"] = cookies
        runtime.opt.auth.username = username
        runtime.opt.update_auth(cookies=cookies, user_id=str(user_id))
        runtime.client._username = username
        runtime.client._user_id = str(user_id)
        return runtime, profile, cookies
    except BaseException:
        runtime.retire()
        raise


async def _bounded(function, timeout: float, *, job: Job | None = None, executor=None):
    control = DownloadControl()
    deadline = time.monotonic() + timeout

    def blocking():
        with jm_task_context(control=control, jmapi_deadline=deadline):
            return function()

    worker = asyncio.create_task(run_blocking(executor, blocking))
    try:
        return await asyncio.wait_for(asyncio.shield(worker), timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        control.cancel(
            "metadata deadline exceeded" if isinstance(error, TimeoutError) else "shutdown"
        )
        if job is not None:
            job.fail(error if isinstance(error, TimeoutError) else BusyError("shutting down"))
        await drain(worker)
        raise


async def search(client, query: str, page: int, *, timeout: float = 30, job=None, executor=None):
    return await _bounded(
        lambda: client.search_site(search_query=query, page=page),
        timeout,
        job=job,
        executor=executor,
    )


async def get_album_detail(client, album_id: str, *, timeout: float = 30, job=None, executor=None):
    return await _bounded(
        lambda: client.get_album_detail(album_id), timeout, job=job, executor=executor
    )


async def categories_filter(
    client, *, page, time, category, order_by, timeout=30, job=None, executor=None
):
    return await _bounded(
        lambda: client.categories_filter(
            page=page,
            time=time,
            category=category,
            order_by=order_by,
        ),
        timeout,
        job=job,
        executor=executor,
    )


async def download_with_retry(
    album_id: str,
    opt: Any,
    *,
    timeout: float = 600,
    max_attempts: int = 3,
    initial_wait: float = 2,
    max_wait: float = 30,
    control: DownloadControl | None = None,
    sync: JmSyncRuntime | None = None,
    job: Job | None = None,
    executor=None,
):
    control = control or DownloadControl()
    deadline = time.monotonic() + timeout

    def blocking():
        with jm_task_context(runtime=sync, control=control, jmapi_deadline=deadline):
            return download_album(album_id, option=opt, control=control)

    worker = None
    try:
        for attempt in range(max_attempts):
            if control.is_cancelled:
                raise DownloadCancelledException(control.reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("download total deadline exceeded")
            worker = asyncio.create_task(run_blocking(executor, blocking))
            try:
                return await asyncio.wait_for(asyncio.shield(worker), remaining)
            except RETRYABLE_DOWNLOAD_EXCEPTIONS as error:
                if isinstance(error, PartialDownloadFailedException):
                    downloader = error.context.get("downloader")
                    failures = getattr(downloader, "download_failed_photo", []) + getattr(
                        downloader, "download_failed_image", []
                    )
                    if any(isinstance(failure, UpstreamSessionExpired) for _, failure in failures):
                        raise UpstreamSessionExpired("JM authentication required") from None
                if control.is_cancelled or attempt + 1 == max_attempts:
                    raise
                wait = min(max_wait, initial_wait * (2**attempt) + random.uniform(0, initial_wait))
                remaining = deadline - time.monotonic()
                if wait >= remaining:
                    raise TimeoutError("download retry deadline exceeded") from None
                logger.info("download_retry_scheduled", attempt=attempt + 1, next_wait_seconds=wait)
                await asyncio.sleep(wait)
    except (TimeoutError, asyncio.CancelledError) as error:
        control.cancel(
            "download deadline exceeded" if isinstance(error, TimeoutError) else "shutdown"
        )
        metrics.CANCELLATIONS.labels("download").inc()
        if job is not None:
            job.fail(error if isinstance(error, TimeoutError) else BusyError("shutting down"))
        # Keep the owner's lane/lock until the synchronous worker acknowledges
        # cancellation. Retrying the same resource cannot overlap this worker.
        metrics.TASKS.labels("download", "cancelling").inc()
        try:
            if worker is not None:
                await drain(worker)
        finally:
            metrics.TASKS.labels("download", "cancelling").dec()
        raise


download = download_with_retry
