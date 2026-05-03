"""Smoke tests for the 5 public endpoints under FastAPI.

Mirrors the Flask-era assertions to lock in the JSON envelope contract.
External calls (jmcomic, file IO) are mocked.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def _stub_album(client_fixture, pdf_path: Path, filename: str = "fake.pdf"):
    from jmcomic_api.services.album import PdfArtifact

    test_client, _runtime = client_fixture
    test_client.app.state.album_service.get_or_build = AsyncMock(
        return_value=PdfArtifact(path=pdf_path, filename=filename)
    )
    return test_client


def test_health_legacy(client):
    test_client, _ = client
    resp = test_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_live(client):
    test_client, _ = client
    assert test_client.get("/health/live").status_code == 200


def test_health_ready(client):
    test_client, _ = client
    resp = test_client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_get_pdf_returns_base64_by_default(client, tmp_path):
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    tc = _stub_album(client, fake_pdf)

    resp = tc.get("/get_pdf/123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["name"] == "fake.pdf"
    assert base64.b64decode(body["data"]) == b"%PDF-1.4 fake"


def test_get_pdf_streams_when_pdf_true(client, tmp_path):
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 streamed")
    tc = _stub_album(client, fake_pdf)

    resp = tc.get("/get_pdf/123?pdf=true")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content == b"%PDF-1.4 streamed"


def test_get_pdf_path_returns_absolute_path(client, tmp_path):
    fake_pdf = tmp_path / "abs.pdf"
    fake_pdf.write_bytes(b"x")
    tc = _stub_album(client, fake_pdf, filename="abs.pdf")

    resp = tc.get("/get_pdf_path/123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "abs.pdf"
    assert body["data"].startswith("/")


def test_get_pdf_returns_404_when_album_missing(client):
    from jmcomic.jm_exception import MissingAlbumPhotoException

    tc, _ = client
    tc.app.state.album_service.get_or_build = AsyncMock(
        side_effect=MissingAlbumPhotoException("not found", {})
    )
    resp = tc.get("/get_pdf/missing")
    assert resp.status_code == 404
    assert resp.json()["success"] is False


def test_search_missing_query_returns_422(client):
    tc, _ = client
    resp = tc.get("/search")
    assert resp.status_code == 422
    body = resp.json()
    # Unified envelope: never bare {"detail": ...}
    assert body["success"] is False
    assert "message" in body
    assert "detail" not in body
    # Detailed errors still surface so clients can pinpoint the bad field.
    assert isinstance(body.get("errors"), list)


def test_album_details_404_uses_unified_envelope(client, monkeypatch):
    from jmcomic_api.services import jm_client as jm

    monkeypatch.setattr(jm, "get_album_detail", AsyncMock(return_value=None))
    tc, _ = client
    resp = tc.get("/album/missing")
    assert resp.status_code == 404
    body = resp.json()
    assert body == {"success": False, "message": "Album with ID 'missing' not found."}


def test_search_returns_results(client, make_page, monkeypatch):
    from jmcomic_api.services import jm_client as jm

    fake = make_page([("100", "Album One"), ("200", "Album Two")], total=2)
    monkeypatch.setattr(jm, "search", AsyncMock(return_value=fake))

    tc, _ = client
    resp = tc.get("/search?query=test&page=1")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["results"] == [
        {"id": "100", "title": "Album One"},
        {"id": "200", "title": "Album Two"},
    ]
    assert body["current_page"] == 1
    assert body["has_next_page"] is False
    assert body["total"] == 2


def test_album_details_returns_tags(client, monkeypatch):
    from jmcomic_api.services import jm_client as jm

    album = MagicMock()
    album.id = "555"
    album.title = "Some Title"
    album.tags = ["tag1", "tag2"]
    monkeypatch.setattr(jm, "get_album_detail", AsyncMock(return_value=album))

    tc, _ = client
    resp = tc.get("/album/555")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": "555", "title": "Some Title", "tags": ["tag1", "tag2"]}


def test_album_details_404_when_falsy(client, monkeypatch):
    from jmcomic_api.services import jm_client as jm

    monkeypatch.setattr(jm, "get_album_detail", AsyncMock(return_value=None))

    tc, _ = client
    resp = tc.get("/album/missing")
    assert resp.status_code == 404


def test_categories_returns_results(client, make_page, monkeypatch):
    from jmcomic_api.services import jm_client as jm

    fake = make_page([("1", "A"), ("2", "B")], total=2)
    monkeypatch.setattr(jm, "categories_filter", AsyncMock(return_value=fake))

    tc, _ = client
    resp = tc.get("/categories?category=hanman&order_by=view&page=1")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["results"] == [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}]
    assert body["params_used"]["category"] == "hanman"
    assert body["total"] == 2
