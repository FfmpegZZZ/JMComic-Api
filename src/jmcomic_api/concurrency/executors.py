"""Application-owned blocking pools; preserve task context across threads."""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import Executor, ThreadPoolExecutor
from functools import partial


async def run_blocking(executor: Executor | None, function, /, *args, **kwargs):
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    call = partial(contextvars.copy_context().run, partial(function, *args, **kwargs))
    return await asyncio.get_running_loop().run_in_executor(executor, call)


class BlockingPools:
    def __init__(self, *, downloads: int, metadata: int, encoding: int):
        self.download = ThreadPoolExecutor(downloads, thread_name_prefix="jmapi-download")
        self.metadata = ThreadPoolExecutor(metadata, thread_name_prefix="jmapi-metadata")
        self.encoding = ThreadPoolExecutor(encoding, thread_name_prefix="jmapi-base64")
        self.auth = ThreadPoolExecutor(2, thread_name_prefix="jmapi-auth")

    def close(self):
        # Call only after owned jobs and response workers have exited.
        for pool in (self.download, self.metadata, self.encoding, self.auth):
            pool.shutdown(wait=True)
