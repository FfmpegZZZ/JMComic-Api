"""Bound HTTP retries across domains by the owning operation's deadline.

Uses jmcomic's documented domain_retry_strategy hook. No global client patch or
transport replacement; configured cookies, proxy and session remain owned by jmcomic.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from jmcomic import JTC, jm_task_context
from jmcomic.jm_exception import MissingAlbumPhotoException, ResponseUnexpectedException

from jmcomic_api import metrics


class UpstreamSessionExpired(Exception):
    """A definite authentication rejection; never contains upstream response data."""


def retry_after(value: str | None, *, now: float | None = None) -> float:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)  # type: ignore[arg-type]
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = date.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return 1.0
    return max(0.0, seconds) if math.isfinite(seconds) else 1.0


def remaining() -> float:
    JTC.raise_if_cancelled()
    deadline = JTC.get_context_value("jmapi_deadline")
    budget = math.inf if deadline is None else deadline - time.monotonic()
    if budget < 0.001:
        # curl timeouts are integer milliseconds; rounding to zero disables
        # them. Do not dispatch a new request with a sub-millisecond budget.
        raise TimeoutError("upstream operation deadline exceeded")
    return budget


def request_timeout(configured, budget: float):
    if isinstance(configured, (tuple, list)):
        connect, read = configured
        if math.isinf(budget) or connect + read <= budget:
            return (connect, read)
        # curl_cffi treats tuple timeout as connect + read total. Leave both
        # positive, while never exceeding the remaining operation budget.
        connect = max(0.001, min(connect, budget / 2))
        return (connect, budget - connect)
    if configured is None or configured == 0:
        return None if math.isinf(budget) else budget
    return min(configured, budget)


class NetworkPolicy:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cooldowns: dict[str, float] = {}

    def _defer(self, host: str, seconds: float):
        with self._lock:
            now = time.monotonic()
            self._cooldowns = {key: end for key, end in self._cooldowns.items() if end > now}
            self._cooldowns[host] = max(self._cooldowns.get(host, 0), now + seconds)

    def _wait(self, host: str):
        while True:
            budget = remaining()
            with self._lock:
                delay = self._cooldowns.get(host, 0) - time.monotonic()
            if delay <= 0:
                return
            # Sync workers must wait cooperatively; metadata/download threads
            # are isolated. Other hosts proceed and cancellation is checked often.
            time.sleep(min(delay, budget, 0.05))

    def __call__(self, client, request=None, url=None, is_image=False, **kwargs):
        if request is None:  # jmcomic invokes the hook during client creation.
            return
        deadline = JTC.get_context_value("jmapi_deadline")
        if deadline is None:
            deadline = time.monotonic() + 60  # bound setup/plugin calls as well
        with jm_task_context(jmapi_deadline=deadline):
            return self._request(client, request, url, is_image, **kwargs)

    def _request(self, client, request, url, is_image, **kwargs):
        domains = list(client.domain_list)
        relative = url.startswith("/")
        if relative and not domains:
            return client.fallback(request, url, 0, 0, is_image)
        with self._lock:
            start = getattr(client, "_jmapi_domain_index", 0)
        errors = []
        # retry_times is a per-call retry budget, shared across all domains.
        # Absolute image URLs never repeat once for every API domain.
        attempts = 1 if JTC.get_context_value("jmapi_no_retry") else client.retry_times + 1
        for attempt in range(attempts):
            index = (start + attempt) % len(domains) if relative else 0
            target = client.of_api_url(url, domains[index]) if relative else url
            host = urlsplit(target).netloc.lower()
            self._wait(host)
            options = dict(kwargs)
            client.update_request_with_specify_domain(
                options, domains[index] if relative else None, is_image
            )
            configured = options.get("timeout", client.postman.get_meta_data("timeout", (5, 25)))
            options["timeout"] = request_timeout(configured, remaining())
            response = None
            started = time.monotonic()
            outcome = "error"
            try:
                response = request(target, **options)
                status = response.status_code
                if status == 401:
                    raise UpstreamSessionExpired("JM authentication required")
                if not is_image and status == 200:
                    with contextlib.suppress(ValueError, TypeError, AttributeError):
                        payload = response.json()
                        if isinstance(payload, dict) and payload.get("code") in (401, "401"):
                            raise UpstreamSessionExpired("JM authentication required")
                if status == 429:
                    self._defer(host, retry_after(response.headers.get("Retry-After")))
                    outcome = "rate_limited"
                    raise ResponseUnexpectedException("Upstream rate limited", {"resp": response})
                if status in (408, 425) or status >= 500:
                    raise ResponseUnexpectedException(
                        "Transient upstream HTTP error", {"resp": response}
                    )
                validated = client.raise_if_resp_should_retry(response, is_image)
                outcome = "success"
                if relative:
                    with self._lock:
                        client._jmapi_domain_index = index
                return validated
            except MissingAlbumPhotoException:
                raise
            except (OSError, ResponseUnexpectedException) as error:
                if isinstance(error, TimeoutError):
                    remaining()  # distinguish a transport timeout from the total deadline
                if (
                    response is not None
                    and 400 <= response.status_code < 500
                    and response.status_code not in (408, 425, 429)
                ):
                    raise
                errors.append(dict(domain=host, url=target, retry=attempt, error=error))
                if relative:
                    with self._lock:
                        client._jmapi_domain_index = (index + 1) % len(domains)
            finally:
                metrics.UPSTREAM_REQUESTS.labels(outcome).inc()
                metrics.UPSTREAM_DURATION.observe(time.monotonic() - started)
                if outcome != "success" and response is not None:
                    with contextlib.suppress(Exception):
                        response.close()
        remaining()  # never start an outer retry after cancellation/deadline
        return client.fallback(
            request, url, len(domains), client.retry_times, is_image, retry_errors=errors, **kwargs
        )
