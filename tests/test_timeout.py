"""Tests for ``asyncio.wait_for`` budgets in the service layer + 504 mapping."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_search_timeout_propagates(monkeypatch):
    """``jm_client.search`` honours the timeout argument."""
    from jmcomic_api.services import jm_client

    monkeypatch.setattr(
        jm_client,
        "_bounded",
        AsyncMock(side_effect=TimeoutError("simulated upstream hang")),
    )

    fake_client = MagicMock()
    with pytest.raises(TimeoutError):
        await jm_client.search(fake_client, "anything", 1, timeout=0.1)


@pytest.mark.asyncio
async def test_router_timeout_returns_504(client):
    """A TimeoutError raised inside the route is mapped to HTTP 504 with
    ``Retry-After`` and the unified envelope."""
    tc, _ = client
    tc.app.state.album_service.get_or_build = AsyncMock(
        side_effect=TimeoutError("budget exhausted")
    )

    resp = tc.get("/get_pdf/123")
    assert resp.status_code == 504
    assert resp.headers.get("retry-after") == "30"
    body = resp.json()
    assert body["success"] is False
    assert "detail" not in body  # unified envelope


@pytest.mark.asyncio
async def test_router_lock_timeout_returns_503(client):
    """``LockTimeout`` is mapped to 503 with Retry-After (busy, not failed)."""
    from jmcomic_api.concurrency.keyed_lock import LockTimeout

    tc, _ = client
    tc.app.state.album_service.get_or_build = AsyncMock(
        side_effect=LockTimeout("could not acquire lock 'album:1' within 0.1s")
    )

    resp = tc.get("/get_pdf/1")
    assert resp.status_code == 503
    assert resp.headers.get("retry-after") == "30"
    body = resp.json()
    assert body["success"] is False


@pytest.mark.asyncio
async def test_keyed_lock_timeout_actually_fires():
    """End-to-end: KeyedAsyncLock.acquire(timeout=...) raises LockTimeout when held."""
    import asyncio

    from jmcomic_api.concurrency.keyed_lock import KeyedAsyncLock, LockTimeout

    lock = KeyedAsyncLock()
    holder_started = asyncio.Event()
    can_release = asyncio.Event()

    async def holder():
        async with lock.acquire("k"):
            holder_started.set()
            await can_release.wait()

    async def waiter():
        await holder_started.wait()
        async with lock.acquire("k", timeout=0.1):
            pass

    holder_task = asyncio.create_task(holder())
    try:
        with pytest.raises(LockTimeout, match="could not acquire"):
            await waiter()
    finally:
        can_release.set()
        await holder_task
