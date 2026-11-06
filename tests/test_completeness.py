"""Trusted manifest migration, missing/corrupt images and upstream page order."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcomic.jm_exception import PartialDownloadFailedException
from PIL import Image

from jmcomic_api.services import jm_client
from jmcomic_api.services.album import AlbumService
from jmcomic_api.services.jm_client import JmRuntime
from jmcomic_api.services.storage import manifest_path, read_json
from jmcomic_api.settings import Settings
from tests.helpers import download_result


@pytest.fixture
def album_setup(tmp_path, monkeypatch):
    root = tmp_path / "images"
    folder = root / "[123]Same title"
    folder.mkdir(parents=True)
    paths = [folder / "chapter2-001.webp", folder / "chapter1-002.webp"]
    for i, path in enumerate(paths):
        Image.new("RGB", (16 + i, 24), "white").save(path)
    album = MagicMock()
    album.name, album.page_count = "Same title", 2
    rt = JmRuntime(SimpleNamespace(dir_rule=SimpleNamespace(base_dir=str(root))), None)
    cfg = Settings(pdf_dir=tmp_path / "pdf", min_free_disk_mb=0)
    service = AlbumService(lambda: rt, settings=cfg)
    mocked = AsyncMock(return_value=download_result(album, paths))
    monkeypatch.setattr(jm_client, "download_with_retry", mocked)
    return service, rt, folder, paths, mocked


async def test_legacy_done_is_migrated_using_ordered_download_manifest(album_setup):
    service, _rt, folder, paths, download = album_setup
    (folder / ".done").touch()
    meta = await service.metadata("jM00123")
    assert meta.images == paths  # deliberate non-lexical chapter order
    assert download.await_count == 1
    persisted = read_json(manifest_path(folder.parent, "123"))
    assert persisted["expected_pages"] == 2
    assert all(len(entry["digest"]) == 64 for entry in persisted["entries"])
    assert (await service.metadata("JM123")).images == paths
    assert download.await_count == 1


async def test_incomplete_upstream_manifest_is_rejected(album_setup):
    service, _, folder, _paths, download = album_setup
    download.return_value.detail.page_count = 10
    with pytest.raises(PartialDownloadFailedException):
        await service.metadata("123")
    assert not manifest_path(folder.parent, "123").exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_missing_and_corrupt_images_are_repaired(album_setup, damage):
    service, _, _, paths, download = album_setup
    await service.metadata("123")
    if damage == "missing":
        paths[0].unlink()
    else:
        paths[0].write_bytes(b"corrupt")

    async def repair(*args, **kwargs):
        assert not paths[0].exists()  # corrupt cached image must not be reused
        Image.new("RGB", (17, 24), "blue").save(paths[0])
        return download.return_value

    download.side_effect = repair
    repaired = await service.metadata("123")
    assert repaired.total_pages == 2
    assert download.await_count == 2
