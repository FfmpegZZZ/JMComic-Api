"""Per-key asyncio lock with reference-counted GC.

Holds at most one ``asyncio.Lock`` per active key. The lock entry is removed
once the last waiter releases — no idle bookkeeping, no leak across
long-running processes.

Replaces the threading-based ``KeyedTaskQueue`` from the Flask era. Same key →
serial; different keys → parallel.

Reliability:
- ``acquire(key, timeout=...)`` will raise :class:`LockTimeout` instead of
  waiting forever if the holder is stuck.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class LockTimeout(Exception):
    """Raised when ``KeyedAsyncLock.acquire`` cannot get the lock in time."""


class KeyedAsyncLock:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}
        self._meta = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, key: str, *, timeout: float | None = None) -> AsyncIterator[None]:
        async with self._meta:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._refs[key] = self._refs.get(key, 0) + 1

        acquired = False
        try:
            if timeout is None:
                await lock.acquire()
            else:
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=timeout)
                except TimeoutError as e:
                    raise LockTimeout(f"could not acquire lock {key!r} within {timeout}s") from e
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            async with self._meta:
                self._refs[key] -= 1
                if self._refs[key] == 0:
                    del self._refs[key]
                    del self._locks[key]
