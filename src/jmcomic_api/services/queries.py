"""Successful-query TTL/LRU; singleflight is provided by the shared manager."""

from __future__ import annotations

import time
from collections import OrderedDict

from jmcomic_api import metrics
from jmcomic_api.concurrency.tasks import BusyError


class QueryCache:
    def __init__(self, manager, lane, capacity=1024):
        self.manager, self.lane, self.capacity = manager, lane, capacity
        self.entries = OrderedDict()
        self.epoch = 0

    def clear(self):
        self.epoch += 1
        self.entries.clear()

    async def run(self, key, factory, *, runtime, ttl=0):
        if self.manager.closing:
            raise BusyError("application shutting down")
        epoch = self.epoch
        key = (epoch, runtime.generation, *key)
        cached = self.entries.get(key)
        if ttl and cached and cached[0] > time.monotonic():
            self.entries.move_to_end(key)
            metrics.record_cache("query", True)
            return cached[1]
        self.entries.pop(key, None)
        metrics.record_cache("query", False)

        async def owned(job):
            result = await factory(job)
            if result is not None and ttl and epoch == self.epoch:
                self.entries[key] = (time.monotonic() + ttl, result)
                self.entries.move_to_end(key)
                while len(self.entries) > self.capacity:
                    self.entries.popitem(last=False)
            return result

        return await self.manager.run(("query", key), self.lane, owned, runtime=runtime)
