from __future__ import annotations

import asyncio
import threading

import pytest
from PIL import Image

from jmcomic_api.concurrency.tasks import BusyError, Lane, TaskManager
from jmcomic_api.services import jm_client
from jmcomic_api.services.build_process import build_pdf_process


async def test_singleflight_waiter_cancellation_does_not_cancel_worker():
    manager = TaskManager()
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def work(job):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 42

    a = asyncio.create_task(manager.run("same", Lane("pdf", 1, 2, 1), work))
    await started.wait()
    b = asyncio.create_task(manager.run("same", None, work))
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    release.set()
    assert await b == 42
    assert calls == 1
    assert not manager.jobs


async def test_timed_out_download_holds_permit_and_identity_until_thread_exits(monkeypatch):
    manager = TaskManager()
    lane = Lane("download", 1, 0, 1)
    release, acknowledged = threading.Event(), threading.Event()
    seen = []

    def slow(album_id, option, *, control):
        seen.append(control)
        while not control.is_cancelled:
            acknowledged.wait(0.002)
        acknowledged.set()
        release.wait(2)
        return None

    monkeypatch.setattr(jm_client, "download_album", slow)

    async def work(job):
        return await jm_client.download_with_retry("1", None, timeout=0.03, job=job)

    try:
        with pytest.raises(TimeoutError):
            await manager.run("same", lane, work)
        assert lane.active == 1
        assert acknowledged.wait(1)
        with pytest.raises(TimeoutError):
            await manager.run("same", lane, work)
        with pytest.raises(BusyError):
            await manager.run("other", lane, work)
        assert len(seen) == 1
    finally:
        release.set()
        await asyncio.gather(*manager.tasks)
    assert lane.active == 0
    assert not manager.jobs


async def test_queue_timeout_and_shutdown():
    manager = TaskManager()
    lane = Lane("metadata", 1, 1, 0.02)
    started = asyncio.Event()

    async def work(job):
        started.set()
        await asyncio.Event().wait()

    first = asyncio.create_task(manager.run("a", lane, work))
    await started.wait()
    with pytest.raises(BusyError, match="timed out"):
        await manager.run("b", lane, work)
    assert await manager.shutdown(0.2) == 0
    with pytest.raises(BusyError):
        await first
    with pytest.raises(BusyError, match="shutting down"):
        await manager.run("c", lane, work)


async def test_pdf_worker_crash_and_timeout_never_publish(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"bad image")
    output = tmp_path / "result.pdf"
    with pytest.raises(RuntimeError, match="worker exited"):
        await build_pdf_process([bad], output)
    assert not output.exists()
    assert not list(tmp_path.glob(".jmapi-*"))
    Image.new("RGB", (20, 30)).save(bad)
    with pytest.raises(TimeoutError):
        await build_pdf_process([bad], output, timeout=0.001)
    assert not output.exists()
    assert not list(tmp_path.glob(".jmapi-*"))
    await build_pdf_process([bad], output)
    assert output.read_bytes().startswith(b"%PDF")


async def test_shutdown_reaps_an_active_pdf_process(tmp_path, monkeypatch):
    from jmcomic_api.services import build_process

    manager = TaskManager()
    lane = Lane("pdf", 1, 32, 1)
    spawned = asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec

    async def slow_child(*args, **kwargs):
        # Deterministic long child; supervise/reap through the production path.
        import sys

        proc = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(proc)
        spawned.set()
        return proc

    monkeypatch.setattr(build_process.asyncio, "create_subprocess_exec", slow_child)

    async def build(job):
        await build_pdf_process([], tmp_path / "output.pdf")

    waiter = asyncio.create_task(manager.run("build", lane, build))
    await spawned.wait()
    assert await manager.shutdown(5) == 0
    with pytest.raises(BusyError):
        await waiter
    assert processes[0].returncode is not None
    assert lane.active == 0
    assert not list(tmp_path.glob(".jmapi-*"))


async def test_total_download_deadline_includes_queue_wait():
    manager = TaskManager()
    lane = Lane("download", 1, 2, 1)
    started, release = asyncio.Event(), asyncio.Event()

    async def holder(job):
        started.set()
        await release.wait()

    first = asyncio.create_task(manager.run("holder", lane, holder))
    await started.wait()
    calls = 0

    async def queued(job):
        nonlocal calls
        calls += 1

    try:
        with pytest.raises(TimeoutError, match="total deadline"):
            await manager.run("queued", lane, queued, total_timeout=0.02)
        assert calls == 0
        assert lane.active == 1
        assert lane.pending == 0
    finally:
        release.set()
        await first
