"""Prepared, leased file responses with bounded Base64 memory."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response, StreamingResponse

from jmcomic_api import metrics
from jmcomic_api.concurrency.executors import run_blocking
from jmcomic_api.concurrency.tasks import drain
from jmcomic_api.logging_config import get_logger
from jmcomic_api.services.storage import leases

CHUNK_BYTES = 192 * 1024  # divisible by 3: concatenating chunks preserves Base64
logger = get_logger(__name__)


def timed_sender(send, timeout):
    async def sending(message):
        try:
            async with asyncio.timeout(timeout):
                await send(message)
        except TimeoutError:
            metrics.SEND_TIMEOUTS.inc()
            logger.warning("response_send_timed_out", timeout_seconds=timeout)
            raise

    return sending


def not_modified(headers, etag, modified):
    if "if-none-match" in headers:
        return any(
            tag.strip() == "*" or tag.strip().removeprefix("W/") == etag
            for tag in headers["if-none-match"].split(",")
        )
    if "if-modified-since" in headers:
        try:
            date = parsedate_to_datetime(headers["if-modified-since"])
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return int(modified) <= date.timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return False


class LeasedFileResponse(FileResponse):
    def __init__(self, path: Path, filename: str, *, send_timeout=30):
        self._send_timeout = send_timeout
        self._lease = leases.hold(path)
        self._lease.__enter__()
        try:
            super().__init__(
                path, filename=filename, media_type="application/pdf", stat_result=path.stat()
            )
        except BaseException:
            self._lease.__exit__(None, None, None)
            raise

    async def __call__(self, scope, receive, send):
        try:
            sending = timed_sender(send, self._send_timeout)
            if scope.get("method", "GET") in ("GET", "HEAD") and not_modified(
                Headers(scope=scope), self.headers["etag"], self.stat_result.st_mtime
            ):
                response = Response(
                    status_code=304,
                    headers={key: self.headers[key] for key in ("etag", "last-modified")},
                )
                await response(scope, receive, sending)
            else:
                await super().__call__(scope, receive, sending)
        finally:
            self._lease.__exit__(None, None, None)


class PreparedBase64Response(StreamingResponse):
    def __init__(self, iterator, *, length, release, send_timeout):
        self._release = release
        self._send_timeout = send_timeout
        super().__init__(
            iterator, media_type="application/json", headers={"Content-Length": str(length)}
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, timed_sender(send, self._send_timeout))
        finally:
            try:
                await self.body_iterator.aclose()
            finally:
                await self._release()


async def base64_response(
    path: Path, fields: dict, lane, *, encoding_lane=None, executor=None, send_timeout=30
):
    admission = lane.enter()
    await admission.__aenter__()
    lease = leases.hold(path)
    lease.__enter__()
    stream = None
    try:
        stream = path.open("rb")
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("PDF source is not a regular file")
        prefix = (
            json.dumps(fields, ensure_ascii=False, separators=(",", ":"))[:-1]
            + ("," if fields else "")
            + '"data":"'
        ).encode()
        suffix = b'"}'
        length = len(prefix) + 4 * ((info.st_size + 2) // 3) + len(suffix)
    except BaseException:
        if stream:
            stream.close()
        lease.__exit__(None, None, None)
        await admission.__aexit__(None, None, None)
        raise

    def read_encoded(count):
        chunk = stream.read(count)
        if len(chunk) != count:
            raise OSError("PDF source truncated during response")
        return base64.b64encode(chunk)

    async def chunks():
        async def encoded_chunk(size):
            worker = asyncio.create_task(run_blocking(executor, read_encoded, size))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                await drain(worker)
                raise

        try:
            yield prefix
            remaining = info.st_size
            while remaining:
                size = min(CHUNK_BYTES, remaining)
                if encoding_lane is None:
                    encoded = await encoded_chunk(size)
                else:
                    async with encoding_lane.enter():
                        encoded = await encoded_chunk(size)
                # Backpressure never holds an encoder permit; only one encoded
                # chunk is retained per admitted stream, including slow peers.
                yield encoded
                remaining -= size
            yield suffix
        except Exception:
            logger.exception("base64_response_read_failed", path=str(path))
            raise  # Headers were sent: terminate the response, never emit valid-looking partial JSON.

    async def release():
        stream.close()
        lease.__exit__(None, None, None)
        await admission.__aexit__(None, None, None)

    return PreparedBase64Response(
        chunks(), length=length, release=release, send_timeout=send_timeout
    )
