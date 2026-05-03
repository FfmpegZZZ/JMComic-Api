"""Per-key asyncio lock with reference-counted GC.

Holds at most one ``asyncio.Lock`` per active key. The lock entry is removed
once the last waiter releases — no idle bookkeeping, no leak across
long-running processes.

Replaces the threading-based ``KeyedTaskQueue`` from the Flask era. Same key →
serial; different keys → parallel.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class KeyedAsyncLock:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}
        self._meta = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, key: str) -> AsyncIterator[None]:
        async with self._meta:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._refs[key] = self._refs.get(key, 0) + 1

        try:
            async with lock:
                yield
        finally:
            async with self._meta:
                self._refs[key] -= 1
                if self._refs[key] == 0:
                    del self._refs[key]
                    del self._locks[key]
