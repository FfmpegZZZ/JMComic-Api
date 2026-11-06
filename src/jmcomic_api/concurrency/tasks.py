"""Single-process job ownership, bounded admission, and cancellation isolation.

A result may time out before its worker exits. The job, permit and singleflight
entry remain owned by the supervisor until the *actual* worker has exited.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from jmcomic_api import metrics
from jmcomic_api.concurrency.keyed_lock import LockTimeout


class BusyError(LockTimeout):
    pass


class Lane:
    def __init__(self, name: str, limit: int, capacity: int, timeout: float):
        self.name, self.limit, self.capacity, self.timeout = name, limit, capacity, timeout
        self.active = self.pending = 0
        self._sem = asyncio.Semaphore(limit)

    @contextlib.asynccontextmanager
    async def enter(self, *, deadline: float | None = None):
        # There is no await between the capacity check and registration.
        if self.active + self.pending >= self.limit + self.capacity:
            raise BusyError(f"{self.name} queue full")
        wait_timeout = self.timeout
        if deadline is not None:
            wait_timeout = min(wait_timeout, deadline - time.monotonic())
            if wait_timeout <= 0:
                raise TimeoutError("operation total deadline exceeded in queue")
        self.pending += 1
        metrics.TASKS.labels(self.name, "pending").inc()
        start = time.monotonic()
        try:
            try:
                await asyncio.wait_for(self._sem.acquire(), wait_timeout)
            except TimeoutError as e:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("operation total deadline exceeded in queue") from e
                raise BusyError(f"{self.name} queue timed out") from e
        finally:
            self.pending -= 1
            metrics.TASKS.labels(self.name, "pending").dec()
            metrics.QUEUE_DURATION.labels(self.name).observe(time.monotonic() - start)
        self.active += 1
        metrics.TASKS.labels(self.name, "active").inc()
        try:
            yield
        finally:
            self.active -= 1
            metrics.TASKS.labels(self.name, "active").dec()
            self._sem.release()


@dataclass
class Job:
    future: asyncio.Future
    created: float

    def fail(self, error: BaseException) -> None:
        if not self.future.done():
            self.future.set_exception(error)


class TaskManager:
    def __init__(self, *, wait_timeout: float = 600):
        self.wait_timeout = wait_timeout
        self.tasks: set[asyncio.Task] = set()
        self.owners: dict[asyncio.Task, str] = {}
        self.jobs: dict[Any, Job] = {}
        self.closing = False

    def track(self, coro: Coroutine, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        # Always retrieve exceptions, including jobs whose every waiter left.
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return task

    async def run(
        self,
        key: Any,
        lane: Lane | None,
        factory: Callable[[Job], Awaitable],
        *,
        runtime=None,
        total_timeout=None,
    ):
        if self.closing:
            raise BusyError("application shutting down")
        job = self.jobs.get(key)
        if job is None:
            future = asyncio.get_running_loop().create_future()
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            job = Job(future, time.monotonic())
            self.jobs[key] = job
            if runtime is not None:
                runtime.retain()

            async def owned():
                try:
                    if lane is None:
                        result = await factory(job)
                    else:
                        async with lane.enter(
                            deadline=job.created + total_timeout
                            if total_timeout is not None
                            else None
                        ):
                            result = await factory(job)
                    if not future.done():
                        future.set_result(result)
                except asyncio.CancelledError:
                    job.fail(BusyError("application shutting down"))
                    raise
                except Exception as e:
                    job.fail(e)
                finally:
                    self.jobs.pop(key, None)
                    if runtime is not None:
                        runtime.release()

            task = self.track(owned(), name=f"job:{key}")
            if runtime is not None:
                self.owners[task] = getattr(runtime, "identity", "anonymous")
                task.add_done_callback(lambda done: self.owners.pop(done, None))
        try:
            return await asyncio.wait_for(asyncio.shield(job.future), self.wait_timeout)
        except TimeoutError:
            if job.future.done():
                return job.future.result()  # preserve operation timeout vs waiter timeout
            raise BusyError("same-resource wait timed out") from None

    async def shutdown(self, grace: float) -> int:
        self.closing = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if not tasks:
            return 0
        _, pending = await asyncio.wait(tasks, timeout=grace)
        return len(pending)


async def drain(task: asyncio.Task) -> None:
    """Keep ownership through cancellation, even if shutdown cancels twice."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.result()
