"""Encrypted local identities, fresh profile logins and owned runtime replacement."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import secrets
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from jmcomic.jm_exception import JmcomicException, MissingAlbumPhotoException

from jmcomic_api.concurrency.keyed_lock import KeyedAsyncLock
from jmcomic_api.concurrency.tasks import BusyError, Lane, drain
from jmcomic_api.services import jm_client
from jmcomic_api.services.network import NetworkPolicy, UpstreamSessionExpired

request_runtime: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "request_runtime", default=None
)
request_session: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "request_session", default=None
)

PROFILE_FIELDS = (
    "uid",
    "username",
    "email",
    "emailverified",
    "photo",
    "fname",
    "gender",
    "coin",
    "album_favorites",
    "level_name",
    "level",
    "nextLevelExp",
    "exp",
    "expPercent",
    "badges",
    "album_favorites_max",
)


def public_profile(data: dict) -> dict:
    return {key: data.get(key) for key in PROFILE_FIELDS}


def unauthorized(message="Invalid or expired session"):
    return HTTPException(401, message, headers={"WWW-Authenticate": "Bearer"})


class SessionStore:
    """Only token digests occur in paths; all record contents are authenticated ciphertext."""

    def __init__(self, root: Path, key_file: Path | None):
        if key_file is None:
            raise ValueError("JMAPI_AUTH_KEY_FILE is required when authentication is enabled")
        key = key_file.read_bytes()
        if len(key) != 32:
            raise ValueError("The authentication key must contain exactly 32 random bytes")
        self.cipher = AESGCM(key)
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=self.root):
            pass

    def save(self, digest: str, data: dict, suffix="session"):
        nonce = os.urandom(12)
        encrypted = nonce + self.cipher.encrypt(
            nonce, json.dumps(data, ensure_ascii=False).encode(), f"{suffix}:{digest}".encode()
        )
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / f"{digest}.{suffix}")
        finally:
            Path(name).unlink(missing_ok=True)

    def delete(self, digest):
        (self.root / f"{digest}.session").unlink(missing_ok=True)

    def read(self, digest, suffix="session"):
        path = self.root / f"{digest}.{suffix}"
        if not path.exists():
            return None
        if path.stat().st_size > 4 * 1024**2:
            raise ValueError("Invalid encrypted account record")
        raw = path.read_bytes()
        return json.loads(self.cipher.decrypt(raw[:12], raw[12:], f"{suffix}:{digest}".encode()))

    def load(self):
        for path in self.root.glob("*.session"):
            digest = path.stem
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                continue
            if path.stat().st_size > 4 * 1024**2:
                raise ValueError("Invalid encrypted session record")
            try:
                data = self.read(digest)
            except Exception:
                raise ValueError(
                    "Cannot decrypt session state; verify the authentication key"
                ) from None
            yield digest, data


@dataclass(repr=False)
class Identity:
    digest: str
    data: dict = field(repr=False)
    runtime: Any = field(default=None, repr=False)
    revoked: bool = False
    config_epoch: int = 0


class AuthRegistry:
    def __init__(self, settings, manager, pools, sync, template=None, policy=None):
        self.settings, self.manager, self.pools, self.sync = settings, manager, pools, sync
        self.template = template
        self.policy = policy or NetworkPolicy()
        self.publish_lock = asyncio.Lock()
        self.store = SessionStore(settings.auth_state_dir, settings.auth_key_file)
        self.records: dict[str, Identity] = {}
        self.runtimes: OrderedDict[str, Identity] = OrderedDict()
        self.locks = KeyedAsyncLock()
        self.lane = Lane("login", 2, settings.queue_capacity, settings.queue_timeout_seconds)
        self.operation_lane = Lane(
            "identity", 2, settings.queue_capacity, settings.queue_timeout_seconds
        )
        self.write_lane = Lane(
            "account", 2, settings.queue_capacity, settings.queue_timeout_seconds
        )
        self.failures: OrderedDict[str, list[float]] = OrderedDict()
        self.inflight_logins = 0
        self.config_epoch = 0
        now = time.time()
        for digest, data in self.store.load():
            if self.expired(data, now):
                self.store.delete(digest)
            else:
                if len(self.records) >= settings.auth_max_sessions:
                    raise ValueError("Stored session count exceeds JMAPI_AUTH_MAX_SESSIONS")
                self.records[digest] = Identity(digest, data)

    def expired(self, data, now=None):
        now = time.time() if now is None else now
        return (
            now >= data["expires_at"]
            or now - data["last_seen"] >= self.settings.auth_session_idle_seconds
        )

    async def persist(self, record):
        try:
            await asyncio.to_thread(self.store.save, record.digest, record.data)
        except OSError:
            raise HTTPException(503, "Cannot persist account session") from None

    async def revoke(self, record):
        # Delete durably before acknowledging logout, never resurrect on request completion.
        async with self.locks.acquire(f"session:{record.digest}"):
            await asyncio.to_thread(self.store.delete, record.digest)
            record.revoked = True
            self.records.pop(record.digest, None)
            self.runtimes.pop(record.digest, None)
            if record.runtime is not None:
                record.runtime.retire()
                record.runtime = None
            for task, owner in tuple(self.manager.owners.items()):
                if owner == record.digest:
                    task.cancel()

    def wire(self, runtime, record):
        runtime.identity = record.digest
        runtime.auth_fetched_at = record.data["fetched_at"]
        runtime.sync, runtime.executors = self.sync, self.pools

    def make_room(self, current=None):
        if len(self.runtimes) < self.settings.auth_max_runtimes:
            return
        for digest, record in tuple(self.runtimes.items()):
            if record is not current and record.runtime._users == 0:
                self.runtimes.pop(digest)
                record.runtime.retire()
                record.runtime = None
                return
        raise BusyError("All account runtimes are busy")

    async def upstream(self, function, *, login=False):
        async def owned(job):
            produced = []

            def execute():
                result = function()
                produced.append(result)
                return result

            try:
                return await jm_client._bounded(
                    execute,
                    self.settings.auth_login_timeout_seconds,
                    executor=self.pools.auth,
                )
            except UpstreamSessionExpired:
                if login:
                    raise unauthorized("JM rejected the account credentials") from None
                raise
            except JmcomicException:
                # JM exceptions may contain response bodies, account data and tokens.
                raise HTTPException(502, "JM account request failed") from None
            except TimeoutError:
                raise
            except (OSError, ValueError, KeyError, TypeError):
                raise HTTPException(502, "JM account request failed") from None
            finally:
                # A timed-out/cancelled worker can have finished constructing a
                # runtime after its result was rejected. It still needs an owner.
                import sys

                if sys.exc_info()[0] is not None:
                    for result in produced:
                        if isinstance(result, tuple) and isinstance(result[0], jm_client.JmRuntime):
                            result[0].retire()

        return await self.manager.run(
            ("account-login" if login else "account-read", uuid.uuid4().hex),
            self.lane if login else self.write_lane,
            owned,
            total_timeout=self.settings.auth_login_timeout_seconds,
        )

    async def supervise(self, coro):
        if self.manager.closing:
            coro.close()
            raise BusyError("Application shutting down")

        async def owned():
            started = False
            try:
                async with self.operation_lane.enter():
                    started = True
                    return await coro
            finally:
                if not started:
                    coro.close()

        task = self.manager.track(owned(), name="account-operation")
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Account changes finish under their locks, including persistence.
            await drain(task)
            raise

    async def login(self, username, password, source):
        await self.maintenance()
        # Bounded source bookkeeping; do not trust user-supplied forwarding headers.
        now = time.monotonic()
        attempts = [t for t in self.failures.pop(source, []) if now - t < 60]
        self.failures[source] = attempts
        while len(self.failures) > 4096:
            self.failures.popitem(last=False)
        if len(attempts) >= 10:
            raise HTTPException(429, "Too many login attempts", headers={"Retry-After": "60"})
        if len(self.records) + self.inflight_logins >= self.settings.auth_max_sessions:
            raise HTTPException(503, "Session capacity reached")
        self.inflight_logins += 1
        attempts.append(now)
        candidate = None
        try:
            async with self.locks.acquire(f"login:{username.casefold()}"):
                epoch, template = self.config_epoch, self.template
                candidate, profile, cookies = await self.upstream(
                    lambda: jm_client.login_runtime(
                        self.settings.option_file,
                        username,
                        password,
                        template=template,
                        policy=self.policy,
                    ),
                    login=True,
                )
                token = secrets.token_urlsafe(32)
                digest = hashlib.sha256(token.encode()).hexdigest()
                now_wall = time.time()
                record = Identity(
                    digest,
                    dict(
                        username=username,
                        password=password,
                        uid=str(profile["uid"]),
                        cookies=cookies,
                        profile=public_profile(profile),
                        fetched_at=now_wall,
                        expires_at=now_wall + self.settings.auth_session_ttl_seconds,
                        last_seen=now_wall,
                    ),
                )
                async with self.publish_lock:
                    self.make_room()
                    await self.persist(record)
                    self.wire(candidate, record)
                    record.runtime = candidate
                    self.records[digest] = record
                    self.runtimes[digest] = record
                    record.config_epoch = epoch
                    candidate = None
                return dict(
                    access_token=token,
                    token_type="bearer",
                    expires_at=record.data["expires_at"],
                    profile=record.data["profile"],
                    fetched_at=record.data["fetched_at"],
                )
        finally:
            self.inflight_logins -= 1
            if candidate is not None:
                candidate.retire()

    async def resolve(self, token):
        if len(token) > 256:
            raise unauthorized()
        digest = hashlib.sha256(token.encode()).hexdigest()
        record = self.records.get(digest)
        if record is None or record.revoked:
            raise unauthorized()
        if self.expired(record.data):
            await self.revoke(record)
            raise unauthorized()
        async with self.locks.acquire(f"session:{digest}"):
            if record.revoked:
                raise unauthorized()
            record.data["last_seen"] = time.time()
            await self.persist(record)
            if digest in self.runtimes:
                self.runtimes.move_to_end(digest)
        return record

    async def refresh(self, record, *, observed=None):
        # Force a login for every /auth/me call; recovery of stale concurrent reads
        # can reuse a replacement already published by another recovery request.
        async with (
            self.locks.acquire(f"uid:{record.data['uid']}"),
            self.locks.acquire(f"login:{record.data['username'].casefold()}"),
            self.locks.acquire(f"session:{record.digest}"),
        ):
            if record.revoked or self.expired(record.data):
                raise unauthorized()
            if observed is not None and record.data["fetched_at"] != observed.auth_fetched_at:
                return record.data["profile"]
            candidate = None
            try:
                epoch, template = self.config_epoch, self.template
                candidate, profile, cookies = await self.upstream(
                    lambda: jm_client.login_runtime(
                        self.settings.option_file,
                        record.data["username"],
                        record.data["password"],
                        template=template,
                        policy=self.policy,
                    ),
                    login=True,
                )
                if str(profile["uid"]) != record.data["uid"]:
                    raise unauthorized("JM account identity changed; please log in again")
                replacement = dict(
                    record.data,
                    cookies=cookies,
                    profile=public_profile(profile),
                    fetched_at=time.time(),
                    last_seen=time.time(),
                )
                async with self.publish_lock:
                    if record.digest not in self.runtimes:
                        self.make_room(record)
                    previous = record.data
                    record.data = replacement
                    try:
                        await self.persist(record)
                    except BaseException:
                        record.data = previous
                        raise
                    self.wire(candidate, record)
                    old = record.runtime
                    record.runtime = candidate
                    candidate = None
                    self.runtimes[record.digest] = record
                    record.config_epoch = epoch
                    if old is not None:
                        old.retire()
                return record.data["profile"]
            finally:
                if candidate is not None:
                    candidate.retire()

    async def maintenance(self):
        for record in tuple(self.records.values()):
            if self.expired(record.data):
                await self.revoke(record)

    async def acquire_runtime(self, record):
        # Pin under the same lock used by logout, reload and LRU eviction.
        async with self.locks.acquire(f"session:{record.digest}"):
            if record.revoked or self.expired(record.data):
                raise unauthorized()
            if record.runtime is None or record.config_epoch != self.config_epoch:
                epoch, template = self.config_epoch, self.template
                runtime, _, _ = await self.upstream(
                    lambda: jm_client.login_runtime(
                        self.settings.option_file,
                        record.data["username"],
                        record.data["password"],
                        cookies=record.data["cookies"],
                        user_id=record.data["uid"],
                        template=template,
                        policy=self.policy,
                    )
                )
                try:
                    async with self.publish_lock:
                        if record.digest not in self.runtimes:
                            self.make_room(record)
                        self.wire(runtime, record)
                        old = record.runtime
                        record.runtime = runtime
                        self.runtimes[record.digest] = record
                        record.config_epoch = epoch
                        if old is not None:
                            old.retire()
                except BaseException:
                    runtime.retire()
                    raise
            runtime = record.runtime
            runtime.retain()
            return runtime

    async def invalidate_runtimes(self, template=None):
        self.template = template
        self.config_epoch += 1

    def close(self):
        for record in self.records.values():
            if record.runtime is not None:
                record.runtime.retire()
                record.runtime = None
        self.runtimes.clear()


def personal_session():
    record = request_session.get()
    if record is None:
        raise unauthorized("Personal login required")
    return record


class IdentityRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            if self.path == "/auth/login":
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            headers = request.headers.getlist("authorization")
            token = None
            if headers:
                parts = headers[0].split()
                if len(headers) != 1 or len(parts) != 2 or parts[0].lower() != "bearer":
                    raise unauthorized()
                token = parts[1]
            registry: Any = getattr(request.app.state, "auth", None)
            if token and registry is None:
                raise unauthorized()
            record = await registry.resolve(token) if token else None
            for attempt in range(2):
                runtime = (
                    await registry.acquire_runtime(record) if record else request.app.state.runtime
                )
                if record is None:
                    runtime.retain()
                rt_marker = request_runtime.set(runtime)
                session_marker = request_session.set(record)
                expired = False
                try:
                    response = await original(request)
                    if record is not None or self.path.startswith(("/auth/", "/me/")):
                        response.headers["Cache-Control"] = "no-store"
                    return response
                except UpstreamSessionExpired:
                    expired = True
                except JmcomicException as error:
                    if record is None:
                        raise
                    if isinstance(error, MissingAlbumPhotoException):
                        raise HTTPException(404, "Album not found") from None
                    raise HTTPException(502, "JM upstream request failed") from None
                finally:
                    request_runtime.reset(rt_marker)
                    request_session.reset(session_marker)
                    runtime.release()
                if expired:
                    if record is None or attempt or self.path == "/auth/me":
                        raise unauthorized("JM session expired; please log in again")
                    await registry.supervise(registry.refresh(record, observed=runtime))
            raise unauthorized()

        return handle
