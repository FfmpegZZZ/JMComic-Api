"""HTTP checks for the rebuilt optimized comparison container only.

Run after compare.py and verify_live.py, with no concurrent benchmark workload.
Uses only fixed offline fixture IDs and never connects to the JM upstream.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from verify_live import sparse_digest, streamed_base64_digest


def fetch(base, path, *, headers=None, method="GET"):
    request = urllib.request.Request(base + path, headers=headers or {}, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error  # urllib reports a valid 304 as HTTPError
    with response:
        return response.status, response.headers, response.read()


def metric(base, name, *, labels=""):
    status, _, body = fetch(base, "/metrics")
    assert status == 200
    prefix = name + labels + " "
    return next(
        (float(line.split()[-1]) for line in body.decode().splitlines() if line.startswith(prefix)),
        0.0,
    )


def active(base):
    return metric(base, "jmapi_tasks", labels='{lane="base64_stream",state="active"}')


def wait_active(base, expected, *, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        value = active(base)
        if value == expected:
            return
        assert time.monotonic() < deadline, f"Expected {expected} active streams, observed {value}"
        time.sleep(0.02)


def stalled_peer(base):
    target = urlsplit(base)
    assert target.scheme == "http", "Dedicated offline container must use HTTP"
    peer = socket.socket()
    try:
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        peer.settimeout(5)
        peer.connect((target.hostname, target.port or 80))
        peer.sendall(
            f"GET /get_pdf/11?passwd=false HTTP/1.1\r\nHost: {target.netloc}\r\n\r\n".encode()
        )
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = peer.recv(1024)
            assert chunk and len(header) < 65536, "Missing response headers"
            header.extend(chunk)
        assert b" 200 " in header.split(b"\r\n", 1)[0]
        return peer
    except BaseException:
        peer.close()
        raise


def conditional_pdf(base):
    path = "/get_pdf/100?pdf=true&passwd=false&quality=70"
    status, headers, content = fetch(base, path)
    assert status == 200 and content.startswith(b"%PDF")
    etag, modified = headers["ETag"], headers["Last-Modified"]
    for condition in ({"If-None-Match": etag}, {"If-Modified-Since": modified}):
        status, _, body = fetch(base, path, headers=condition)
        assert status == 304 and body == b""
    status, _, body = fetch(base, path, headers={"Range": "bytes=0-127"})
    assert status == 206 and body == content[:128]
    return dict(
        passed=True, etag=etag, full_bytes=len(content), conditional_status=304, range_status=206
    )


def slow_peers(base):
    wait_active(base, 0)
    peers = []
    try:
        for _ in range(3):
            peers.append(stalled_peer(base))
        wait_active(base, 3)
        time.sleep(0.1)
        start = time.perf_counter()
        metadata, size, digest = streamed_base64_digest(base + "/get_pdf/10?passwd=false")
        elapsed = time.perf_counter() - start
        assert metadata["success"] is True and size == 10 * 1024**2
        assert digest == sparse_digest(size)
        assert active(base) == 3, "Fast response depended on stalled peers being released"
        return dict(passed=True, stalled_peers=3, fast_response_seconds=elapsed, decoded_bytes=size)
    finally:
        for peer in peers:
            peer.close()
        wait_active(base, 0)


def send_timeout(base, *, expected_timeout):
    wait_active(base, 0)
    before = metric(base, "jmapi_response_send_timeouts_total")
    peer = stalled_peer(base)
    start = time.monotonic()
    try:
        wait_active(base, 1)
        deadline = start + expected_timeout + 10
        while metric(base, "jmapi_response_send_timeouts_total") == before:
            assert time.monotonic() < deadline, "Stalled response did not hit send timeout"
            time.sleep(0.1)
        elapsed = time.monotonic() - start
        assert elapsed >= expected_timeout - 1, "Observed a different timeout than configured"
        wait_active(base, 0)
        status, _, _ = fetch(base, "/health/live")
        assert status == 200
        return dict(
            passed=True,
            timeout_seconds=expected_timeout,
            observed_seconds=elapsed,
            permits_released=True,
            healthy_after=True,
        )
    finally:
        with contextlib.suppress(OSError):
            peer.close()
        wait_active(base, 0)


def bounded_streams(base):
    wait_active(base, 0)
    peers = []
    try:
        for _ in range(4):
            peers.append(stalled_peer(base))
        wait_active(base, 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(streamed_base64_digest, base + "/get_pdf/10?passwd=false")
            try:
                deadline = time.monotonic() + 3
                while (
                    metric(base, "jmapi_tasks", labels='{lane="base64_stream",state="pending"}')
                    != 1
                ):
                    assert time.monotonic() < deadline, "Fifth stream did not enter bounded waiting"
                    time.sleep(0.02)
                assert active(base) == 4 and not future.done()
                status, _, _ = fetch(base, "/health/live")
                assert status == 200
                peers.pop().close()
                _, size, digest = future.result(timeout=5)
                assert size == 10 * 1024**2 and digest == sparse_digest(size)
                assert active(base) == 3
            finally:
                for peer in peers:
                    peer.close()
        return dict(passed=True, active_limit=4, queued_streams=1, resumed_after_slot_released=True)
    finally:
        for peer in peers:
            peer.close()
        wait_active(base, 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--optimized", default="http://127.0.0.1:18701")
    parser.add_argument("--send-timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results/network-fixed"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, work in (
        ("conditional_pdf", lambda: conditional_pdf(args.optimized)),
        ("slow_peers", lambda: slow_peers(args.optimized)),
        ("bounded_streams", lambda: bounded_streams(args.optimized)),
        ("send_timeout", lambda: send_timeout(args.optimized, expected_timeout=args.send_timeout)),
    ):
        print(f"optimized: checking {name}", flush=True)
        result[name] = work()
        (args.output / "network-verification.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result[name], indent=2), flush=True)


if __name__ == "__main__":
    main()
