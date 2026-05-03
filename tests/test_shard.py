"""Tests for the /pdf/info and /pdf/shard endpoints (ported from develop)."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from pypdf import PdfReader

from jmcomic_api.services.album import AlbumImages, AlbumService


def _make_real_album(tmp_path, num_pages: int = 12, album_id: str = "12345"):
    """Lay out a fake album folder with real webp files so AlbumService can
    enumerate them. Returns the AlbumImages record + the parent base_dir."""
    base = tmp_path / "webp"
    base.mkdir(exist_ok=True)
    folder = base / f"[{album_id}]Test Album"
    folder.mkdir()
    paths = []
    for i in range(num_pages):
        p = folder / f"{i:04}.webp"
        Image.new("RGB", (32, 48), "white").save(p, format="WEBP")
        paths.append(p)
    return AlbumImages(album_id=album_id, title="Test Album", folder=folder, images=paths)


def test_pdf_info_returns_shards(client, monkeypatch):
    tc, _ = client
    fake = _make_real_album(tc.app.state.settings.pdf_path.parent, num_pages=237)
    tc.app.state.album_service.metadata = AsyncMock(return_value=fake)

    resp = tc.get("/pdf/info/12345")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["jm_album_id"] == "12345"
    assert data["title"] == "Test Album"
    assert data["total_pages"] == 237
    assert data["shard_size"] == 100
    assert data["shards"] == [
        {"shard_index": 1, "start_page": 1, "end_page": 100},
        {"shard_index": 2, "start_page": 101, "end_page": 200},
        {"shard_index": 3, "start_page": 201, "end_page": 237},
    ]


def test_pdf_info_handles_empty_album(client):
    tc, _ = client
    empty = AlbumImages(
        album_id="999", title="Empty", folder=tc.app.state.settings.pdf_path, images=[]
    )
    tc.app.state.album_service.metadata = AsyncMock(return_value=empty)

    resp = tc.get("/pdf/info/999")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_pages"] == 0
    assert data["shards"] == []


def test_pdf_shard_streams_when_pdf_true(client, tmp_path):
    """End-to-end: build a real shard PDF off real webp files."""
    tc, _ = client
    cache_dir = tmp_path / "shards"
    cache_dir.mkdir()
    tc.app.state.settings.pdf_shard_cache_dir = cache_dir
    tc.app.state.settings.pdf_shard_size = 5

    fake = _make_real_album(tmp_path, num_pages=12)
    # Use the real AlbumService so build_shard actually merges images.
    real = AlbumService(runtime_provider=lambda: type("R", (), {"opt": None})())
    real.metadata = AsyncMock(return_value=fake)
    tc.app.state.album_service = real

    resp = tc.get("/pdf/shard/12345/2?pdf=true&passwd=false")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")

    # Validate the returned PDF: shard 2 of size 5 = pages 6..10 → 5 pages.
    out = tmp_path / "got.pdf"
    out.write_bytes(resp.content)
    reader = PdfReader(str(out))
    assert not reader.is_encrypted
    assert len(reader.pages) == 5


def test_pdf_shard_returns_base64_json_by_default(client, tmp_path):
    tc, _ = client
    cache_dir = tmp_path / "shards"
    cache_dir.mkdir()
    tc.app.state.settings.pdf_shard_cache_dir = cache_dir
    tc.app.state.settings.pdf_shard_size = 5

    fake = _make_real_album(tmp_path, num_pages=8)
    real = AlbumService(runtime_provider=lambda: type("R", (), {"opt": None})())
    real.metadata = AsyncMock(return_value=fake)
    tc.app.state.album_service = real

    resp = tc.get("/pdf/shard/12345/1?passwd=false")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["title"] == "Test Album"
    assert body["shard_index"] == 1
    assert body["start_page"] == 1
    assert body["end_page"] == 5
    assert body["total_pages"] == 8

    decoded = base64.b64decode(body["data"])
    assert decoded.startswith(b"%PDF")


def test_pdf_shard_404_for_out_of_range(client, tmp_path):
    tc, _ = client
    cache_dir = tmp_path / "shards"
    cache_dir.mkdir()
    tc.app.state.settings.pdf_shard_cache_dir = cache_dir
    tc.app.state.settings.pdf_shard_size = 5

    fake = _make_real_album(tmp_path, num_pages=8)
    real = AlbumService(runtime_provider=lambda: type("R", (), {"opt": None})())
    real.metadata = AsyncMock(return_value=fake)
    tc.app.state.album_service = real

    resp = tc.get("/pdf/shard/12345/99")
    assert resp.status_code == 404
    body = resp.json()
    # Unified envelope — no bare ``{"detail": ...}`` from FastAPI's HTTPException.
    assert body["success"] is False
    assert "out of range" in body["message"]
    assert "detail" not in body


def test_pdf_shard_422_for_zero_index(client):
    tc, _ = client
    resp = tc.get("/pdf/shard/12345/0")
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert "detail" not in body


def test_pdf_shard_caches_second_request(client, tmp_path):
    """Second request to the same shard should reuse the cached file."""
    tc, _ = client
    cache_dir = tmp_path / "shards"
    cache_dir.mkdir()
    tc.app.state.settings.pdf_shard_cache_dir = cache_dir
    tc.app.state.settings.pdf_shard_size = 5

    fake = _make_real_album(tmp_path, num_pages=8)
    real = AlbumService(runtime_provider=lambda: type("R", (), {"opt": None})())
    real.metadata = AsyncMock(return_value=fake)
    tc.app.state.album_service = real

    r1 = tc.get("/pdf/shard/12345/1?pdf=true&passwd=false")
    r2 = tc.get("/pdf/shard/12345/1?pdf=true&passwd=false")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Identical bytes → cache reuse path was taken (PDF generation is non-deterministic
    # in metadata fields like CreationDate, but deterministic here because we just
    # return the same on-disk file).
    assert r1.content == r2.content


@pytest.mark.e2e
def test_shard_pdf_is_decryptable(tmp_path):
    """Direct service test: encrypted shard PDF can be decrypted with the album id."""
    import asyncio

    from jmcomic_api.concurrency.keyed_lock import KeyedAsyncLock
    from jmcomic_api.services.album import AlbumService

    fake = _make_real_album(tmp_path, num_pages=8, album_id="555")
    svc = AlbumService(
        runtime_provider=lambda: type("R", (), {"opt": None})(),
        lock=KeyedAsyncLock(),
    )
    svc.metadata = AsyncMock(return_value=fake)

    cache = tmp_path / "shards"
    cache.mkdir()

    art, total, start, end = asyncio.run(svc.build_shard("JM555", 1, 5, cache, enable_pwd=True))
    assert (total, start, end) == (8, 1, 5)

    reader = PdfReader(str(art.path))
    assert reader.is_encrypted
    assert reader.decrypt("555")
    assert len(reader.pages) == 5
