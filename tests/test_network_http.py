"""Exercise the real curl session against a local fault-injection HTTP server."""

from __future__ import annotations

import contextlib
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from curl_cffi.requests import Session
from jmcomic import JmHtmlClient, jm_task_context
from jmcomic.common import CurlCffiSessionPostman
from jmcomic.jm_exception import RequestRetryAllFailException

from jmcomic_api.services.network import NetworkPolicy

# Capture before the autouse upstream blocker is installed. The fixture restores
# it only while URLs are controlled by the local server below.
SESSION_REQUEST = Session.request


@pytest.fixture
def local_http(monkeypatch):
    calls, ports = Counter(), []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            calls[self.path] += 1
            ports.append(self.client_address[1])
            if self.path == "/slow":
                time.sleep(0.1)
            limited = (self.path == "/rate" and calls[self.path] == 1) or self.path == "/wait"
            # The deliberately slow response may finish after the client closes.
            with contextlib.suppress(OSError):
                self.send_response(429 if limited else 200)
                self.send_header("Content-Length", "2")
                if limited:
                    self.send_header("Retry-After", "10" if self.path == "/wait" else "0.03")
                self.end_headers()
                self.wfile.write(b"ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    monkeypatch.setattr(Session, "request", SESSION_REQUEST)
    postman = CurlCffiSessionPostman({"timeout": (0.1, 0.5), "trust_env": False})
    client = JmHtmlClient(
        postman=postman,
        domain_list=["unused.invalid"],
        retry_times=1,
        domain_retry_strategy=NetworkPolicy(),
    )
    try:
        yield f"http://127.0.0.1:{server.server_port}", client, calls, ports
    finally:
        postman.session.close()
        server.shutdown()
        server.server_close()
        thread.join(1)


def test_real_session_reuses_connection_and_honors_429(local_http):
    base, client, calls, ports = local_http
    started = time.monotonic()
    first = client.get(base + "/rate")
    assert first.status_code == 200 and first.content == b"ok"
    assert time.monotonic() - started >= 0.03
    assert client.get(base + "/ok").content == b"ok"
    assert calls["/rate"] == 2 and calls["/ok"] == 1
    assert len(set(ports)) == 1


def test_real_transport_timeout_and_cooldown_respect_operation_deadline(local_http):
    base, client, calls, _ = local_http
    client.retry_times = 0
    with pytest.raises(RequestRetryAllFailException):
        client.get(base + "/slow", timeout=0.02)
    assert calls["/slow"] == 1
    client.retry_times = 2
    with (
        jm_task_context(jmapi_deadline=time.monotonic() + 0.04),
        pytest.raises(TimeoutError),
    ):
        client.get(base + "/wait")
    assert calls["/wait"] == 1
