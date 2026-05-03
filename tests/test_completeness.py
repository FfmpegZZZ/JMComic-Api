"""Tests for the ``.done`` marker completeness invariant.

Lock in:
- folder + ``.done`` → cache hit (no download triggered)
- folder + no ``.done`` → re-trigger download
- folder absent → fresh download
- after partial download (actual < expected pages), ``.done`` is NOT created
  AND ``PartialDownloadFailedException`` propagates (so caller sees 503)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcomic.jm_exception import PartialDownloadFailedException
from PIL import Image

from jmcomic_api.services import jm_client
from jmcomic_api.services.album import DONE_MARKER, AlbumService


def _make_album_folder(base: Path, album_id: str, title: str, num_pages: int) -> Path:
    folder = base / f"[{album_id}]{title}"
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(num_pages):
        Image.new("RGB", (16, 16), "white").save(folder / f"{i:04}.webp")
    return folder


@pytest.fixture
def runtime(tmp_path):
    rt = MagicMock()
    rt.opt.dir_rule.base_dir = str(tmp_path / "webp")
    (tmp_path / "webp").mkdir()
    return rt


@pytest.fixture
def service(runtime):
    return AlbumService(runtime_provider=lambda: runtime)


@pytest.mark.asyncio
async def test_done_marker_present_skips_download(service, runtime, tmp_path, monkeypatch):
    folder = _make_album_folder(tmp_path / "webp", "12345", "Cached", num_pages=10)
    (folder / DONE_MARKER).touch()

    download_spy = AsyncMock()
    monkeypatch.setattr(jm_client, "download_with_retry", download_spy)

    title = await service._ensure_title("12345", runtime)
    assert title == "Cached"
    download_spy.assert_not_called()


@pytest.mark.asyncio
async def test_partial_cache_resumes_with_download(service, runtime, tmp_path, monkeypatch):
    """Folder exists (from a previous failed attempt) but no .done — must re-download."""
    folder = _make_album_folder(tmp_path / "webp", "12345", "Partial", num_pages=5)
    # No .done marker.

    fake_album = MagicMock()
    fake_album.name = "Partial"
    fake_album.page_count = 5  # matches actual on-disk count after re-download

    monkeypatch.setattr(jm_client, "download_with_retry", AsyncMock(return_value=(fake_album, [])))

    title = await service._ensure_title("12345", runtime)
    assert title == "Partial"
    assert (folder / DONE_MARKER).exists(), "marker must be written after successful resume"


@pytest.mark.asyncio
async def test_fresh_download_when_folder_absent(service, runtime, tmp_path, monkeypatch):
    fake_album = MagicMock()
    fake_album.name = "Fresh"
    fake_album.page_count = 0

    async def fake_download(album_id, opt, **kwargs):
        # Simulate jmcomic creating the folder during download.
        _make_album_folder(tmp_path / "webp", "12345", "Fresh", num_pages=3)
        fake_album.page_count = 3
        return fake_album, []

    monkeypatch.setattr(jm_client, "download_with_retry", fake_download)

    title = await service._ensure_title("12345", runtime)
    assert title == "Fresh"
    folder = tmp_path / "webp" / "[12345]Fresh"
    assert (folder / DONE_MARKER).exists()


@pytest.mark.asyncio
async def test_partial_download_does_not_write_done(service, runtime, tmp_path, monkeypatch):
    fake_album = MagicMock()
    fake_album.name = "Truncated"
    fake_album.page_count = 10  # claims 10 pages...

    async def fake_download(album_id, opt, **kwargs):
        # ...but only 4 written to disk
        _make_album_folder(tmp_path / "webp", "12345", "Truncated", num_pages=4)
        return fake_album, []

    monkeypatch.setattr(jm_client, "download_with_retry", fake_download)

    with pytest.raises(PartialDownloadFailedException):
        await service._ensure_title("12345", runtime)

    folder = tmp_path / "webp" / "[12345]Truncated"
    assert not (folder / DONE_MARKER).exists(), (
        "marker must NOT be written when download is incomplete"
    )
