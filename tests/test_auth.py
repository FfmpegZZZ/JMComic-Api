from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jmcomic import JTC
from jmcomic.jm_exception import JmcomicException

from jmcomic_api.concurrency.executors import BlockingPools
from jmcomic_api.concurrency.tasks import TaskManager
from jmcomic_api.services import jm_client
from jmcomic_api.services.auth import AuthRegistry, SessionStore
from jmcomic_api.services.jm_client import JmRuntime
from jmcomic_api.services.network import UpstreamSessionExpired
from jmcomic_api.settings import Settings


class AccountFactory:
    def __init__(self, root):
        self.root = root
        self.logins = Counter()
        self.toggles = Counter()
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.delay = 0
        self.fail = False
        self.expire = False
        self.runtimes = []
        self.templates = []
        self.policies = []

    def __call__(
        self, _path, username, password, *, cookies=None, user_id=None, template=None, policy=None
    ):
        self.templates.append(template)
        self.policies.append(policy)
        if cookies is None:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(self.delay)
                if self.fail:
                    raise JmcomicException("password-and-cookie-must-not-leak", {})
                self.logins[username] += 1
            finally:
                with self.lock:
                    self.active -= 1
        count = self.logins[username]
        profile = {
            "uid": username,
            "username": username,
            "coin": count,
            "s": "private-session-cookie",
            "password": password,
        }
        factory = self

        class Client:
            def get_album_detail(self, album_id):
                if factory.expire:
                    factory.expire = False
                    raise UpstreamSessionExpired()
                return SimpleNamespace(id=album_id, title=username, tags=[])

            def get_daily(self):
                if factory.expire:
                    factory.expire = False
                    raise UpstreamSessionExpired()
                return SimpleNamespace(res_data={"user": username, "signed": False})

            def toggle_favorite_album(self, album_id):
                if factory.expire:
                    factory.expire = False
                    raise UpstreamSessionExpired()
                factory.toggles[username] += 1
                if factory.fail:
                    raise OSError("private-upstream-error")
                return SimpleNamespace(res_data={"type": "add"})

            def daily_checkin(self):
                return SimpleNamespace(code=1, msg="already signed")

        runtime = JmRuntime(SimpleNamespace(dir_rule=SimpleNamespace(base_dir=self.root)), Client())
        self.runtimes.append(runtime)
        return runtime, profile if cookies is None else None, {"AVS": "private-session-cookie"}


@pytest.fixture
def account_app(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    (tmp_path / "images").mkdir()
    cfg_path = tmp_path / "option.yml"
    cfg_path.write_text("dir_rule:\n  base_dir: " + str(tmp_path / "images"))
    for name, value in {
        "AUTH_ENABLED": "true",
        "AUTH_KEY_FILE": str(key),
        "AUTH_STATE_DIR": str(tmp_path / "sessions"),
        "OPTION_FILE": str(cfg_path),
        "PDF_DIR": str(tmp_path / "pdf"),
        "PDF_SHARD_CACHE_DIR": str(tmp_path / "shards"),
    }.items():
        monkeypatch.setenv("JMAPI_" + name, value)
    from jmcomic_api import app as app_module
    from jmcomic_api.deps import settings

    factory = AccountFactory(tmp_path / "images")
    anonymous = JmRuntime(
        SimpleNamespace(dir_rule=SimpleNamespace(base_dir=tmp_path / "images")),
        SimpleNamespace(
            get_album_detail=lambda aid: SimpleNamespace(id=aid, title="anonymous", tags=[])
        ),
    )
    monkeypatch.setattr(app_module, "load_runtime", lambda *a, **k: anonymous)
    monkeypatch.setattr(jm_client, "login_runtime", factory)
    settings.cache_clear()

    @contextmanager
    def client():
        settings.cache_clear()
        with TestClient(app_module.create_app()) as c:
            yield c

    yield client, factory, tmp_path
    settings.cache_clear()


def login(client, username="alice", password="a-long-private-password"):
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token = response.json()["data"]["access_token"]
    return token, {"Authorization": "Bearer " + token}


def test_each_profile_request_logs_in_and_keeps_accounts_isolated(account_app):
    make_client, factory, root = account_app
    with make_client() as c:
        token, alice = login(c)
        _, bob = login(c, "bob")
        assert c.get("/auth/me", headers=alice).json()["data"]["profile"]["coin"] == 2
        assert c.get("/auth/me", headers=alice).json()["data"]["profile"]["coin"] == 3
        assert c.get("/auth/me", headers=bob).json()["data"]["profile"]["coin"] == 2
        assert factory.logins == {"alice": 3, "bob": 2}
        assert c.get("/album/123", headers=alice).json()["data"]["title"] == "alice"
        assert c.get("/album/123", headers=bob).json()["data"]["title"] == "bob"
        assert c.get("/album/123").json()["data"]["title"] == "anonymous"
        response = c.get("/auth/me", headers=alice)
        assert '"s"' not in response.text and '"password"' not in response.text
        record = next(r for r in c.app.state.auth.records.values() if r.data["uid"] == "alice")
        ciphertext = (root / "sessions" / f"{record.digest}.session").read_bytes()
        for secret in (token, "a-long-private-password", "private-session-cookie", "alice"):
            assert secret.encode() not in ciphertext
        assert (root / "sessions" / f"{record.digest}.session").stat().st_mode & 0o777 == 0o600


def test_restart_restores_password_and_expired_upstream_session_relogs_once(account_app):
    make_client, factory, _ = account_app
    with make_client() as c:
        _, headers = login(c)
    with make_client() as c:
        assert c.get("/auth/me", headers=headers).status_code == 200
        assert factory.logins["alice"] == 2
        factory.expire = True
        result = c.get("/me/daily", headers=headers)
        assert result.status_code == 200 and result.json()["data"]["user"] == "alice"
        assert factory.logins["alice"] == 3
        assert c.delete("/auth/session", headers=headers).status_code == 200
        assert c.get("/auth/me", headers=headers).status_code == 401
    with make_client() as c:
        assert c.get("/auth/me", headers=headers).status_code == 401


def test_login_failure_does_not_return_stale_profile_or_leak_exception(account_app):
    make_client, factory, _ = account_app
    with make_client() as c:
        _, headers = login(c)
        factory.fail = True
        result = c.get("/auth/me", headers=headers)
        assert result.status_code == 502
        assert "password-and-cookie" not in result.text and "profile" not in result.json()
        factory.fail = False
        assert c.get("/auth/me", headers=headers).status_code == 200


def test_body_validation_never_echoes_password_and_invalid_token_never_becomes_anonymous(
    account_app,
):
    make_client, _, _ = account_app
    with make_client() as c:
        result = c.post(
            "/auth/login",
            json={"username": "", "password": "private-body-value", "extra": "private-body-value"},
        )
        assert result.status_code == 422 and "private-body-value" not in result.text
        for value in ("Bearer invalid", "Basic test", "Bearer"):
            assert c.get("/album/123", headers={"Authorization": value}).status_code == 401
        assert c.get("/me/daily").status_code == 401


def test_mutation_receipts_survive_logout_and_relogin(account_app):
    make_client, factory, _ = account_app
    with make_client() as c:
        _, headers = login(c)
        headers["Idempotency-Key"] = "toggle-1"
        assert c.post("/me/favorites/123/toggle", headers=headers).status_code == 200
        assert c.post("/me/favorites/123/toggle", headers=headers).status_code == 200
        assert factory.toggles["alice"] == 1
        assert c.post("/me/favorites/124/toggle", headers=headers).status_code == 409
        assert c.delete("/auth/session", headers=headers).status_code == 200
        _, headers = login(c)
        headers["Idempotency-Key"] = "toggle-1"
        assert c.post("/me/favorites/123/toggle", headers=headers).status_code == 200
        assert factory.toggles["alice"] == 1
        headers["Idempotency-Key"] = "unknown-operation"
        factory.fail = True
        assert c.post("/me/favorites/123/toggle", headers=headers).status_code == 502
        factory.fail = False
        assert c.post("/me/favorites/123/toggle", headers=headers).status_code == 409
        assert factory.toggles["alice"] == 2


def test_wrong_key_and_tampered_records_are_rejected(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    store = SessionStore(tmp_path / "records", key)
    digest = hashlib.sha256(b"token").hexdigest()
    store.save(digest, {"password": "secret"})
    key.write_bytes(os.urandom(32))
    with pytest.raises(ValueError, match="Cannot decrypt"):
        list(SessionStore(tmp_path / "records", key).load())
    raw = (store.root / f"{digest}.session").read_bytes()
    (store.root / f"{digest}.session").write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(ValueError, match="Cannot decrypt"):
        list(store.load())


async def test_concurrent_profile_reads_each_login_but_serialize_by_account(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    cfg = Settings(auth_key_file=key, auth_state_dir=tmp_path / "state", _env_file=None)
    pools = BlockingPools(downloads=1, metadata=1, encoding=1)
    manager = TaskManager()
    auth = AuthRegistry(cfg, manager, pools, None)
    factory = AccountFactory(tmp_path)
    factory.delay = 0.02
    monkeypatch.setattr(jm_client, "login_runtime", factory)
    try:
        first = await auth.login("alice", "password", "local")
        second = await auth.login("alice", "password", "local")
        a, b = await auth.resolve(first["access_token"]), await auth.resolve(second["access_token"])
        await asyncio.gather(auth.refresh(a), auth.refresh(b), auth.refresh(a))
        assert factory.logins["alice"] == 5
        assert factory.peak == 1
    finally:
        await manager.shutdown(3)
        auth.close()
        pools.close()


def test_login_runtime_uses_direct_login_and_updates_both_clients(tmp_path, monkeypatch):
    option = tmp_path / "option.yml"
    option.write_text("auth: {username: server, password: server-secret}\n")

    class Client:
        def __init__(self):
            self.cookies = {}

        def __getitem__(self, key):
            return getattr(self, key)

        def __setitem__(self, key, value):
            setattr(self, key, value)

        def login(self, username, password):
            assert JTC.get_context_value("jmapi_no_retry") is True
            assert password == "personal-password"
            self.cookies = {"AVS": "personal-cookie"}
            return SimpleNamespace(res_data={"uid": "uid", "username": username})

    metadata, downloads = Client(), Client()
    fake = SimpleNamespace(auth=SimpleNamespace(username=None))

    def update_auth(**kwargs):
        downloads.cookies = kwargs["cookies"]

    fake.update_auth = update_auth
    fake.dir_rule = SimpleNamespace(base_dir=tmp_path)
    runtime = JmRuntime(fake, metadata)
    observed = []

    def construct(data, path, *, anonymous, policy=None):
        observed.append((data, anonymous))
        return runtime

    monkeypatch.setattr(jm_client, "runtime_from_data", construct)
    rt, profile, _ = jm_client.login_runtime(option, "personal", "personal-password")
    assert rt is runtime and profile["uid"] == "uid"
    assert metadata.cookies == downloads.cookies == {"AVS": "personal-cookie"}
    assert fake.auth.username == "personal"
    assert observed[0][0]["log"] is False and observed[0][1] is True


@pytest.fixture
async def isolated_registry(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    cfg = Settings(auth_key_file=key, auth_state_dir=tmp_path / "state", _env_file=None)
    pools = BlockingPools(downloads=1, metadata=1, encoding=1)
    manager = TaskManager()
    auth = AuthRegistry(cfg, manager, pools, None)
    factory = AccountFactory(tmp_path)
    monkeypatch.setattr(jm_client, "login_runtime", factory)
    try:
        yield auth, factory
    finally:
        await manager.shutdown(3)
        auth.close()
        pools.close()


async def test_concurrent_login_publication_respects_runtime_limit_and_lru(isolated_registry):
    auth, factory = isolated_registry
    auth.settings.auth_max_runtimes = 1
    persist = auth.persist

    async def slow_persist(record):
        await asyncio.sleep(0.01)
        await persist(record)

    auth.persist = slow_persist
    first, second = await asyncio.gather(
        auth.login("alice", "password", "local"), auth.login("bob", "password", "local")
    )
    assert len(auth.runtimes) == 1
    assert sum(record.runtime is not None for record in auth.records.values()) == 1
    for response in (first, second):
        record = await auth.resolve(response["access_token"])
        runtime = await auth.acquire_runtime(record)
        assert runtime.identity == record.digest and len(auth.runtimes) == 1
        runtime.release()
    assert factory.logins == {"alice": 1, "bob": 1}  # cookies restore without fresh login


async def test_failed_persistence_keeps_previous_identity_and_closes_candidate(
    isolated_registry, monkeypatch
):
    auth, factory = isolated_registry
    response = await auth.login("alice", "password", "local")
    record = await auth.resolve(response["access_token"])
    previous_runtime, previous_data = record.runtime, record.data.copy()

    def fail(*args):
        raise OSError("private-storage-failure")

    monkeypatch.setattr(auth.store, "save", fail)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await auth.refresh(record)
    assert error.value.status_code == 503
    assert record.runtime is previous_runtime and not previous_runtime._retired
    assert record.data == previous_data and factory.runtimes[-1]._retired


@pytest.mark.parametrize("cancel", [False, True])
async def test_late_login_worker_keeps_permit_and_retires_discarded_runtime(
    isolated_registry, cancel
):
    auth, factory = isolated_registry
    started, release = threading.Event(), threading.Event()

    def delayed():
        started.set()
        assert release.wait(2)
        return factory(None, "alice", "password")

    auth.settings.auth_login_timeout_seconds = 0.02
    task = asyncio.create_task(auth.upstream(delayed, login=True))
    while not started.is_set():
        await asyncio.sleep(0.001)
    if cancel:
        # Cancel the actual owned job, as application shutdown does.
        for owned in tuple(auth.manager.tasks):
            owned.cancel()
    await asyncio.sleep(0.04)
    assert auth.lane.active == 1 and not task.done()
    release.set()
    from jmcomic_api.concurrency.tasks import BusyError

    with pytest.raises(BusyError if cancel else TimeoutError):
        await task
    assert auth.lane.active == 0 and factory.runtimes[-1]._retired


async def test_expired_api_token_deletes_saved_password_and_runtime(isolated_registry):
    auth, _ = isolated_registry
    result = await auth.login("alice", "password", "local")
    record = await auth.resolve(result["access_token"])
    runtime = record.runtime
    record.data["expires_at"] = time.time() - 1
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await auth.resolve(result["access_token"])
    assert error.value.status_code == 401
    assert record.revoked and runtime._retired
    assert not list(auth.store.root.glob("*.session"))


def test_rejected_write_reauthenticates_once_before_executing(account_app):
    make_client, factory, _ = account_app
    with make_client() as client:
        _, headers = login(client)
        headers["Idempotency-Key"] = "rejected-write"
        factory.expire = True
        result = client.post("/me/favorites/123/toggle", headers=headers)
        assert result.status_code == 200
        assert factory.logins["alice"] == 2 and factory.toggles["alice"] == 1
        assert client.post("/me/favorites/123/toggle", headers=headers).status_code == 200
        assert factory.toggles["alice"] == 1


def test_failed_reload_uses_last_valid_account_template(account_app, monkeypatch):
    make_client, factory, root = account_app
    with make_client() as client:
        auth = client.app.state.auth
        valid_template = {"client": {"retry_times": 1}}
        # The application normally receives this snapshot from load_runtime.
        client.portal.call(auth.invalidate_runtimes, valid_template)
        (root / "option.yml").write_text("invalid: [yaml")
        from jmcomic_api import app as app_module

        def fail(*args, **kwargs):
            raise ValueError("invalid configuration")

        monkeypatch.setattr(app_module, "load_runtime", fail)
        client.portal.call(client.app.state.reload_runtime)
        _, headers = login(client)
        assert client.get("/auth/me", headers=headers).status_code == 200
        assert factory.templates[-1] is valid_template
        assert all(policy is auth.policy for policy in factory.policies)
