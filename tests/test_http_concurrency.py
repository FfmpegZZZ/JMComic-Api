"""Real socket backpressure through Uvicorn and the offline fixture app."""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def test_three_slow_peers_do_not_block_a_fourth_base64_response(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for album_id, size in ((10, 10), (11, 100)):
        with (tmp_path / f"transport-{album_id}.pdf").open("wb") as stream:
            stream.write(b"%PDF-1.4\n")
            stream.truncate(size * 1024**2)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update(
        BENCH_ROOT=str(tmp_path),
        BENCH_VARIANT="slow-peers",
        PYTHONPATH=os.pathsep.join((str(root / "scripts/benchmark"), str(root / "src"))),
        JMAPI_MAX_CONCURRENT_BASE64="1",
        JMAPI_MAX_CONCURRENT_BASE64_STREAMS="4",
        JMAPI_QUEUE_CAPACITY="0",
        JMAPI_RESPONSE_SEND_TIMEOUT_SECONDS="5",
    )
    clients = []
    with (tmp_path / "server.log").open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "fixture_app:app",
                "--fd",
                str(listener.fileno()),
                "--no-access-log",
                "--timeout-graceful-shutdown",
                "1",
            ],
            env=env,
            cwd=root,
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=log,
        )
        listener.close()

        def metrics():
            with urllib.request.urlopen(base + "/metrics", timeout=2) as response:
                content = response.read().decode()
            prefix = 'jmapi_tasks{lane="base64_stream",state="active"}'
            return next(
                (
                    float(line.split()[-1])
                    for line in content.splitlines()
                    if line.startswith(prefix)
                ),
                0,
            )

        try:
            deadline = time.monotonic() + 10
            while True:
                assert proc.poll() is None, (tmp_path / "server.log").read_text()
                try:
                    with urllib.request.urlopen(base + "/health/live", timeout=0.2) as response:
                        assert response.status == 200
                    break
                except (OSError, urllib.error.URLError):
                    assert time.monotonic() < deadline, "offline server failed to start"
                    time.sleep(0.05)
            for _ in range(3):
                peer = socket.socket()
                clients.append(peer)
                peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                peer.settimeout(3)
                peer.connect(("127.0.0.1", port))
                peer.sendall(b"GET /get_pdf/11?passwd=false HTTP/1.1\r\nHost: localhost\r\n\r\n")
                assert b"200" in peer.recv(1024)
                # Leave the rest unread so the server encounters real backpressure.
            time.sleep(0.1)
            assert metrics() == 3
            received = 0
            with urllib.request.urlopen(base + "/get_pdf/10?passwd=false", timeout=4) as response:
                assert response.status == 200
                while chunk := response.read(192 * 1024):
                    received += len(chunk)
                assert received == int(response.headers["Content-Length"])
                assert received > 10 * 1024**2
            assert metrics() == 3  # completion did not depend on slow-peer expiry
            for peer in clients:
                peer.close()
            deadline = time.monotonic() + 3
            while metrics():
                assert time.monotonic() < deadline, "disconnected streams kept their permits"
                time.sleep(0.02)
        finally:
            for peer in clients:
                with contextlib.suppress(OSError):
                    peer.close()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
