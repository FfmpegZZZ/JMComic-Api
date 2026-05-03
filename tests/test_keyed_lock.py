"""Tests for the asyncio KeyedAsyncLock that replaced KeyedTaskQueue.

Verifies:
- same key → serial
- different keys → parallel
- exception path doesn't leak the lock entry
- many concurrent keys + tasks complete deterministically
"""

from __future__ import annotations

import asyncio

import pytest

from jmcomic_api.concurrency.keyed_lock import KeyedAsyncLock


@pytest.mark.asyncio
async def test_same_key_runs_serially():
    lock = KeyedAsyncLock()
    order: list[int] = []

    async def task(n: int):
        async with lock.acquire("k"):
            await asyncio.sleep(0.005)
            order.append(n)

    await asyncio.gather(*(task(i) for i in range(10)))
    assert order == list(range(10))


@pytest.mark.asyncio
async def test_different_keys_run_in_parallel():
    lock = KeyedAsyncLock()
    barrier_count = 4
    arrived = asyncio.Event()
    counter = 0
    cond = asyncio.Lock()

    async def task(key: str):
        nonlocal counter
        async with lock.acquire(key):
            async with cond:
                counter += 1
                if counter == barrier_count:
                    arrived.set()
            # If keys serialised this would deadlock — they shouldn't.
            await asyncio.wait_for(arrived.wait(), timeout=1.0)

    await asyncio.gather(*(task(f"k{i}") for i in range(barrier_count)))


@pytest.mark.asyncio
async def test_lock_entry_cleaned_up_after_release():
    lock = KeyedAsyncLock()

    async def acquire(key: str):
        async with lock.acquire(key):
            pass

    await acquire("once")
    # No active waiters: dict should be empty.
    assert "once" not in lock._locks
    assert "once" not in lock._refs


@pytest.mark.asyncio
async def test_exception_does_not_leak_entry():
    lock = KeyedAsyncLock()

    with pytest.raises(ValueError, match="boom"):
        async with lock.acquire("err"):
            raise ValueError("boom")

    assert "err" not in lock._locks
    # Subsequent acquire on same key still works.
    async with lock.acquire("err"):
        pass


@pytest.mark.asyncio
async def test_high_concurrency_stress():
    lock = KeyedAsyncLock()
    counts: dict[str, int] = {f"k{i}": 0 for i in range(8)}

    async def task(k: str):
        async with lock.acquire(k):
            counts[k] += 1

    await asyncio.gather(*(task(k) for k in counts for _ in range(15)))
    assert all(v == 15 for v in counts.values())
    # All entries cleaned up.
    assert lock._locks == {}
    assert lock._refs == {}
