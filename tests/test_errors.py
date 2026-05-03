"""Coverage for ``errors.register_handlers`` — every branch of the unified
error envelope, exercised through real routes (no test-only routes).

Locks in:
- 422 validation: ``{success, message, errors[]}``, no ``detail``
- 502 / 503 from jmcomic exception family
- ``Retry-After`` header on 503
- 500 fallback for ``FileNotFoundError``
- 404 fallthrough for unknown routes still uses the unified shape
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from jmcomic.jm_exception import (
    PartialDownloadFailedException,
    RequestRetryAllFailException,
)


def _assert_unified(body: dict) -> None:
    assert body["success"] is False
    assert isinstance(body.get("message"), str)
    assert "detail" not in body, "FastAPI default detail leaked through"


def test_404_unknown_route_uses_envelope(client):
    tc, _ = client
    resp = tc.get("/totally-not-a-route")
    assert resp.status_code == 404
    _assert_unified(resp.json())


def test_request_retry_all_fail_returns_502(client):
    tc, _ = client
    tc.app.state.album_service.get_or_build = AsyncMock(
        side_effect=RequestRetryAllFailException("upstream down", {})
    )
    resp = tc.get("/get_pdf/123")
    assert resp.status_code == 502
    _assert_unified(resp.json())


def test_partial_download_failed_returns_503_with_retry_after(client):
    tc, _ = client
    tc.app.state.album_service.get_or_build = AsyncMock(
        side_effect=PartialDownloadFailedException("partial", {})
    )
    resp = tc.get("/get_pdf/123")
    assert resp.status_code == 503
    assert resp.headers.get("retry-after") == "30"
    _assert_unified(resp.json())


def test_local_file_missing_returns_500(client):
    tc, _ = client
    tc.app.state.album_service.get_or_build = AsyncMock(
        side_effect=FileNotFoundError("/app/webp/missing")
    )
    resp = tc.get("/get_pdf/123")
    assert resp.status_code == 500
    _assert_unified(resp.json())


def test_validation_422_includes_errors_array(client):
    tc, _ = client
    resp = tc.get("/categories?category=NOT_A_CATEGORY")
    assert resp.status_code == 422
    body = resp.json()
    _assert_unified(body)
    assert isinstance(body.get("errors"), list)
    assert any("category" in (".".join(str(p) for p in e["loc"])) for e in body["errors"])
