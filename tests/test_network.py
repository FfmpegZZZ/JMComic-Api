from __future__ import annotations

import concurrent.futures
import threading
import time
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from jmcomic import DownloadControl, JmHtmlClient, jm_task_context
from jmcomic.common import Postman
from jmcomic.jm_exception import (
    DownloadCancelledException,
    MissingAlbumPhotoException,
    RequestRetryAllFailException,
)

from jmcomic_api.services.network import (
    NetworkPolicy,
    UpstreamSessionExpired,
    request_timeout,
    retry_after,
)


class Transport(Postman):
    def __init__(self, replies):
        super().__init__({"timeout": (5, 25)})
        self.replies = list(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs, time.monotonic()))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply()
        return reply


def response(status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, close=MagicMock())


def make_client(policy, replies, *, retries=1, domains=None):
    return JmHtmlClient(
        postman=Transport(replies),
        domain_list=domains or ["a.invalid", "b.invalid", "c.invalid"],
        retry_times=retries,
        domain_retry_strategy=policy,
    )


def test_retry_budget_is_shared_across_domains_and_image_urls():
    policy = NetworkPolicy()
    for url in ("/album/1", "https://images.invalid/1.jpg"):
        client = make_client(policy, [OSError("offline")] * 3, retries=2)
        with pytest.raises(RequestRetryAllFailException) as error:
            client.get(url)
        assert len(client.postman.calls) == len(error.value.errors) == 3
        if url.startswith("https"):
            assert {call[0] for call in client.postman.calls} == {url}


def test_transient_failure_switches_domain_and_reuses_successful_domain():
    bad, good = response(503), response()
    client = make_client(NetworkPolicy(), [bad, good, good])
    assert client.get("/album/1") is good
    assert client.get("/album/2") is good
    assert [call[0] for call in client.postman.calls] == [
        "https://a.invalid/album/1",
        "https://b.invalid/album/1",
        "https://b.invalid/album/2",
    ]
    bad.close.assert_called_once()
    good.close.assert_not_called()


def test_deadline_caps_http_timeout_and_stops_new_attempts():
    def slow():
        time.sleep(0.03)
        raise OSError("late transport failure")

    client = make_client(NetworkPolicy(), [slow] * 3, retries=2)
    with (
        jm_task_context(jmapi_deadline=time.monotonic() + 0.02),
        pytest.raises(TimeoutError, match="deadline"),
    ):
        client.get("/album/1")
    assert len(client.postman.calls) == 1
    assert 0 < sum(client.postman.calls[0][1]["timeout"]) <= 0.02


def test_permanent_missing_is_not_retried():
    client = make_client(NetworkPolicy(), [MissingAlbumPhotoException("missing", {})], retries=5)
    with pytest.raises(MissingAlbumPhotoException):
        client.get("/album/1")
    assert len(client.postman.calls) == 1


def test_429_cooldown_is_shared_between_clients_but_not_hosts():
    policy = NetworkPolicy()
    limited = make_client(
        policy, [response(429, {"Retry-After": "0.05"})], retries=0, domains=["a.invalid"]
    )
    waiting = make_client(policy, [response()], domains=["a.invalid"])
    other = make_client(policy, [response()], domains=["other.invalid"])
    with pytest.raises(RequestRetryAllFailException):
        limited.get("/album/1")
    other.get("/album/1")
    waiting.get("/album/1")
    first = limited.postman.calls[0][2]
    assert waiting.postman.calls[0][2] - first >= 0.045
    assert other.postman.calls[0][2] < waiting.postman.calls[0][2]


def test_cancel_and_deadline_interrupt_cooldown_without_network():
    policy = NetworkPolicy()
    policy._defer("a.invalid", 10)
    client = make_client(policy, [response()], domains=["a.invalid"])
    with jm_task_context(jmapi_deadline=time.monotonic() + 0.01), pytest.raises(TimeoutError):
        client.get("/album/1")
    control, started = DownloadControl(), threading.Event()

    def request():
        with jm_task_context(control=control):
            started.set()
            client.get("/album/1")

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(request)
        assert started.wait(1)
        control.cancel("shutdown")
        with pytest.raises(DownloadCancelledException):
            future.result(timeout=1)
    assert not client.postman.calls


def test_retry_after_dates_invalid_values_and_timeout_normalization():
    assert retry_after("2") == 2
    assert retry_after(formatdate(1005, usegmt=True), now=1000) == 5
    assert retry_after("invalid") == retry_after("nan") == 1
    assert retry_after("-1") == 0
    assert request_timeout(7, 3) == 3
    assert request_timeout(None, 3) == 3
    assert request_timeout([5, 25], 10) == (5, 5)


@pytest.mark.parametrize("json_rejection", [False, True])
def test_authentication_rejection_never_retries_and_closes_response(json_rejection):
    rejected = response(200 if json_rejection else 401)
    rejected.json = lambda: {"code": "401"}
    client = make_client(NetworkPolicy(), [rejected], retries=5)
    with pytest.raises(UpstreamSessionExpired):
        client.get("/account")
    assert len(client.postman.calls) == 1
    rejected.close.assert_called_once()


def test_write_transport_context_disables_all_retries():
    client = make_client(NetworkPolicy(), [OSError("connection lost")], retries=5)
    with jm_task_context(jmapi_no_retry=True), pytest.raises(RequestRetryAllFailException):
        client.get("/write")
    assert len(client.postman.calls) == 1
