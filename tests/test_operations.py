from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from jmcomic_api.concurrency.tasks import Lane, TaskManager
from jmcomic_api.services.jm_client import JmRuntime
from jmcomic_api.services.queries import QueryCache
from jmcomic_api.services.storage import cleanup, confined, disk_admission, leases


def test_low_disk_admission_and_cleanup_leases(tmp_path, monkeypatch):
    import os
    import shutil

    root = tmp_path / "v2" / "1"
    root.mkdir(parents=True)
    active = root / "active.pdf"
    inactive = root / "inactive.pdf"
    legacy = tmp_path / "old.pdf"
    stale = root / ".jmapi-old.tmp"
    fresh = root / ".jmapi-new.tmp"
    for path in (active, inactive, legacy, stale, fresh):
        path.write_bytes(b"pdf")
    old = time.time() - 90000
    for path in (active, inactive, legacy, stale):
        os.utime(path, (old, old))
    with leases.hold(active):
        cleanup([tmp_path], ttl=1)
        assert active.exists()
        assert not inactive.exists()
        assert stale.exists()  # skip all temporary files in an active build directory
    cleanup([tmp_path])
    assert not stale.exists()
    assert fresh.exists()
    assert legacy.exists()
    assert active.exists()  # retention disabled by default
    monkeypatch.setattr(shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    with pytest.raises(HTTPException) as error:
        disk_admission(tmp_path, 1)
    assert error.value.status_code == 503
    assert error.value.headers["Retry-After"] == "30"


def test_path_confinement_blocks_symlinks(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException):
        confined(root, root / "link" / "escape.pdf")


async def test_query_cache_singleflight_success_only_ttl_and_reload():
    manager = TaskManager()
    cache = QueryCache(manager, Lane("metadata", 1, 32, 1), capacity=2)
    rt = JmRuntime(None, None)
    calls = 0

    async def factory(job):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.001)
        return 42

    values = await asyncio.gather(
        *[cache.run(("search", "q"), factory, runtime=rt, ttl=0.01) for _ in range(8)]
    )
    assert values == [42] * 8
    assert calls == 1
    await cache.run(("search", "q"), factory, runtime=rt, ttl=0.01)
    assert calls == 1
    await asyncio.sleep(0.02)
    await cache.run(("search", "q"), factory, runtime=rt, ttl=0.01)
    assert calls == 2
    cache.clear()
    await cache.run(("search", "q"), factory, runtime=rt, ttl=0.01)
    assert calls == 3

    async def failed(job):
        raise ValueError("failure")

    for _ in range(2):
        with pytest.raises(ValueError):
            await cache.run(("fail",), failed, runtime=rt, ttl=10)
    assert not any("fail" in str(key) for key in cache.entries)
    await cache.run(("a",), factory, runtime=rt, ttl=10)
    await cache.run(("b",), factory, runtime=rt, ttl=10)
    assert len(cache.entries) == 2


def test_runtime_retirement_waits_for_users():
    session1, session2 = MagicMock(), MagicMock()
    client = SimpleNamespace(postman=SimpleNamespace(session=session1))
    download = SimpleNamespace(postman=SimpleNamespace(session=session2))
    opt = SimpleNamespace()
    opt.__dict__["__cache_build_jm_client__"] = download
    rt = JmRuntime(opt, client)
    rt.retain()
    rt.retire()
    session1.close.assert_not_called()
    rt.release()
    session1.close.assert_called_once()
    session2.close.assert_called_once()


def test_unknown_routes_have_bounded_metric_label(client):
    tc, _ = client
    for index in range(3):
        assert tc.get(f"/random-path-{index}").status_code == 404
    metrics = tc.get("/metrics").text
    assert 'route="__unmatched__"' in metrics
    assert 'route="/random-path-' not in metrics


def test_readiness_shutdown_and_reload_failure_fallback(client, monkeypatch):
    from jmcomic_api import app as app_module

    tc, rt = client

    def bad_load(_):
        raise ValueError("bad config")

    monkeypatch.setattr(app_module, "load_runtime", bad_load)
    tc.portal.call(tc.app.state.reload_runtime)
    assert tc.app.state.runtime is rt
    tc.app.state.manager.closing = True
    assert tc.get("/health/ready").status_code == 503


@pytest.mark.parametrize(
    "path",
    [
        "/get_pdf/invalid",
        "/get_pdf_path/invalid",
        "/pdf/info/invalid",
        "/pdf/shard/invalid/1",
        "/album/invalid",
    ],
)
def test_all_album_routes_return_unified_422_for_invalid_ids(client, path):
    tc, _ = client
    response = tc.get(path)
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert "detail" not in body


async def test_asgi_processing_and_full_send_timings_and_disconnect():
    from jmcomic_api import metrics
    from jmcomic_api.metrics import ObservabilityMiddleware

    route = "/__timing_test__"
    processing = metrics.REQUEST_DURATION.labels("GET", route)
    sending = metrics.RESPONSE_DURATION.labels("GET", route)
    before_processing = processing._sum.get()
    before_sending = sending._sum.get()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0.03)
        await send({"type": "http.response.body", "body": b"data"})

    async def send(message):
        pass

    scope = dict(
        type="http", method="GET", path=route, headers=[], route=SimpleNamespace(path=route)
    )
    middleware = ObservabilityMiddleware(app)
    await middleware(scope, None, send)
    assert sending._sum.get() - before_sending >= 0.03
    assert processing._sum.get() - before_processing < 0.02
    before_disconnect = metrics.RESPONSE_OUTCOMES.labels("disconnect")._value.get()

    async def disconnected(message):
        raise OSError("connection closed")

    with pytest.raises(OSError):
        await middleware(scope, None, disconnected)
    assert metrics.RESPONSE_OUTCOMES.labels("disconnect")._value.get() == before_disconnect + 1
