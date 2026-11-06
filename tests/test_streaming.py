from __future__ import annotations

import asyncio
import base64
import json

import pytest
from starlette.requests import ClientDisconnect

from jmcomic_api.concurrency.tasks import Lane
from jmcomic_api.services.responses import CHUNK_BYTES, base64_response
from jmcomic_api.services.storage import leases


@pytest.mark.parametrize("length", [0, 1, 2, 3, CHUNK_BYTES - 1, CHUNK_BYTES, CHUNK_BYTES + 1])
async def test_base64_exact_length_and_unicode(tmp_path, length):
    path = tmp_path / "中文.pdf"
    data = bytes(i % 251 for i in range(length))
    path.write_bytes(data)
    lane = Lane("base64", 1, 1, 1)
    response = await base64_response(path, dict(success=True, name=path.name), lane)
    output = bytearray()

    async def send(message):
        if message["type"] == "http.response.body":
            output.extend(message.get("body", b""))

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send)
    body = json.loads(output)
    assert body["name"] == path.name
    assert base64.b64decode(body["data"]) == data
    assert int(response.headers["content-length"]) == len(output)
    assert lane.active == 0
    assert leases.remove(path)


async def test_disconnect_and_read_failure_release_admission_and_file(tmp_path):
    path = tmp_path / "file.pdf"
    path.write_bytes(b"a" * (CHUNK_BYTES + 2))
    lane = Lane("base64", 1, 0, 1)
    response = await base64_response(path, dict(success=True), lane)
    assert not leases.remove(path)

    async def disconnected(message):
        raise OSError("peer closed")

    with pytest.raises((OSError, ClientDisconnect)):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, disconnected)
    assert lane.active == 0
    response = await base64_response(path, dict(success=True), lane)
    path.write_bytes(b"x")  # truncate after preflight, before read

    async def send(message):
        pass

    with pytest.raises((OSError, ClientDisconnect)):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send)
    assert lane.active == 0
    assert leases.remove(path)


async def test_stream_backpressure_keeps_one_chunk_in_memory(tmp_path):
    path = tmp_path / "large.pdf"
    with path.open("wb") as stream:
        stream.truncate(10 * 1024**2)
    lane = Lane("base64", 1, 0, 1)
    response = await base64_response(path, {}, lane)
    largest = 0

    async def send(message):
        nonlocal largest
        largest = max(largest, len(message.get("body", b"")))
        await asyncio.sleep(0)

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send)
    assert largest <= CHUNK_BYTES * 4 // 3


async def test_slow_client_releases_encoder_permit_for_another_stream(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"abc" * CHUNK_BYTES)
    streams, encoding = Lane("base64_stream", 2, 0, 1), Lane("base64", 1, 0, 1)
    stalled, release = asyncio.Event(), asyncio.Event()

    async def slow(message):
        if len(message.get("body", b"")) > 100:
            stalled.set()
            await release.wait()

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    first = await base64_response(path, {}, streams, encoding_lane=encoding)
    sending = asyncio.create_task(first(scope, None, slow))
    try:
        await asyncio.wait_for(stalled.wait(), 1)
        assert streams.active == 1 and encoding.active == 0
        second = await base64_response(path, {}, streams, encoding_lane=encoding)
        output = bytearray()

        async def fast(message):
            output.extend(message.get("body", b""))

        await asyncio.wait_for(second(scope, None, fast), 1)
        assert base64.b64decode(json.loads(output)["data"]) == path.read_bytes()
        assert streams.active == 1 and encoding.active == 0
    finally:
        release.set()
        await sending
    assert streams.active == 0 and leases.remove(path)


async def test_send_timeout_releases_stream_and_file(tmp_path):
    path = tmp_path / "file.pdf"
    path.write_bytes(b"abc")
    lane = Lane("base64_stream", 1, 0, 1)
    response = await base64_response(path, {}, lane, send_timeout=0.01)

    async def stalled(message):
        await asyncio.Event().wait()

    with pytest.raises((TimeoutError, ClientDisconnect)):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, stalled)
    assert lane.active == 0 and leases.remove(path)


async def test_cancel_during_encoding_keeps_both_permits_until_reader_exits(tmp_path, monkeypatch):
    import threading

    from jmcomic_api.services import responses

    path = tmp_path / "source.pdf"
    path.write_bytes(b"abc" * 1024)
    started, release = threading.Event(), threading.Event()
    original = responses.base64.b64encode

    def delayed(data):
        started.set()
        release.wait(2)
        return original(data)

    monkeypatch.setattr(responses.base64, "b64encode", delayed)
    streams, encoding = Lane("base64_stream", 1, 0, 1), Lane("base64", 1, 0, 1)
    response = await base64_response(path, {}, streams, encoding_lane=encoding)

    async def send(message):
        pass

    task = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send)
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert streams.active == encoding.active == 1
        assert not task.done() and not leases.remove(path)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert streams.active == encoding.active == 0 and leases.remove(path)


async def test_stream_limit_rejects_before_opening_more_files(tmp_path):
    from jmcomic_api.concurrency.tasks import BusyError

    path = tmp_path / "file.pdf"
    path.write_bytes(b"abc")
    lane = Lane("base64_stream", 1, 0, 1)
    first = await base64_response(path, {}, lane)
    with pytest.raises(BusyError, match="queue full"):
        await base64_response(path, {}, lane)

    async def send(message):
        pass

    await first({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send)
    assert lane.active == 0 and leases.remove(path)


async def test_raw_pdf_conditional_requests_and_range(tmp_path):
    from starlette.responses import FileResponse

    from jmcomic_api.services.responses import LeasedFileResponse

    path = tmp_path / "file.pdf"
    path.write_bytes(b"0123456789")
    etag = FileResponse(path, stat_result=path.stat()).headers["etag"]
    modified = FileResponse(path, stat_result=path.stat()).headers["last-modified"]
    for headers, expected_status, expected_body in (
        ([(b"if-none-match", etag.encode())], 304, b""),
        ([(b"if-none-match", ("W/" + etag).encode())], 304, b""),
        ([(b"if-modified-since", modified.encode())], 304, b""),
        (
            [(b"if-none-match", b'"other"'), (b"if-modified-since", modified.encode())],
            200,
            b"0123456789",
        ),
        ([(b"range", b"bytes=2-5")], 206, b"2345"),
    ):
        messages = []

        async def send(message, messages=messages):
            messages.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "headers": headers,
            "asgi": {"spec_version": "2.4"},
        }
        await LeasedFileResponse(path, path.name)(scope, None, send)
        assert messages[0]["status"] == expected_status
        assert b"".join(message.get("body", b"") for message in messages) == expected_body
    assert leases.remove(path)
