from __future__ import annotations

import asyncio
import threading

from jmcomic import JTC, jm_task_context

from jmcomic_api.concurrency.executors import BlockingPools, run_blocking


async def test_slow_metadata_cannot_starve_download_or_encoding_and_context_is_preserved():
    pools = BlockingPools(downloads=1, metadata=1, encoding=1)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(3)

    held = asyncio.create_task(run_blocking(pools.metadata, slow))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        with jm_task_context(marker="request"):
            for executor, prefix in (
                (pools.download, "jmapi-download"),
                (pools.encoding, "jmapi-base64"),
            ):
                name, value = await asyncio.wait_for(
                    run_blocking(
                        executor,
                        lambda: (threading.current_thread().name, JTC.get_context_value("marker")),
                    ),
                    1,
                )
                assert name.startswith(prefix) and value == "request"
    finally:
        release.set()
        await held
        pools.close()
