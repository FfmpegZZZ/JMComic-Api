"""Killable PDF workers. Only the parent may publish a validated artifact."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from jmcomic_api import metrics
from jmcomic_api.concurrency.tasks import drain
from jmcomic_api.logging_config import get_logger
from jmcomic_api.services.pdf_builder import cache_decryptable

logger = get_logger(__name__)


async def stop_process(proc):
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


async def build_pdf_process(
    paths: list[Path], output: Path, *, password=None, jpeg_quality=None, timeout=300, **options
):
    output.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix=".jmapi-build-", suffix=".tmp", dir=output.parent
    ) as scratch_name:
        scratch = Path(scratch_name)
        spec = scratch / "spec.json"
        staging = scratch / "output.pdf"
        errors = scratch / "stderr"
        spec.write_text(
            json.dumps(
                dict(
                    paths=[str(p) for p in paths],
                    output=str(staging),
                    password=password,
                    jpeg_quality=jpeg_quality,
                    **options,
                )
            ),
            encoding="utf-8",
        )
        proc = None
        try:
            with errors.open("wb") as stderr:
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "jmcomic_api.services.pdf_worker",
                        str(spec),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=stderr,
                    )
                )
                try:
                    proc = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    await drain(spawning)
                    if not spawning.cancelled() and spawning.exception() is None:
                        proc = spawning.result()
                    raise
            remaining = timeout - (time.monotonic() - start)
            await asyncio.wait_for(proc.wait(), max(0.000001, remaining))
            if proc.returncode != 0:
                metrics.BUILD_FAILURES.labels("process").inc()
                with errors.open("rb") as stream:
                    stream.seek(max(0, stream.seek(0, 2) - 8192))
                    detail = stream.read().decode("utf-8", errors="replace")
                logger.error("pdf_worker_failed", returncode=proc.returncode, detail=detail)
                raise RuntimeError(f"PDF worker exited with status {proc.returncode}")
            validation = asyncio.create_task(
                asyncio.to_thread(
                    cache_decryptable,
                    staging,
                    password,
                    expected_pages=len(paths),
                )
            )
            try:
                valid = await asyncio.wait_for(
                    asyncio.shield(validation), max(0.000001, timeout - (time.monotonic() - start))
                )
            except (asyncio.CancelledError, TimeoutError):
                await drain(validation)
                raise
            if not valid:
                metrics.BUILD_FAILURES.labels("validation").inc()
                raise RuntimeError("PDF worker produced an invalid artifact")
            os.replace(staging, output)
        except TimeoutError:
            metrics.BUILD_FAILURES.labels("timeout").inc()
            raise
        finally:
            if proc is not None:
                # Cancellation cannot interrupt reaping; cleanup happens only
                # after the child and any parent validation thread have exited.
                await drain(asyncio.create_task(stop_process(proc)))
