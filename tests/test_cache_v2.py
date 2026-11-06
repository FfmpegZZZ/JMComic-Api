from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image
from pypdf import PdfReader

from jmcomic_api.services import jm_client, pdf_builder
from jmcomic_api.services.album import AlbumService
from jmcomic_api.services.jm_client import JmRuntime
from jmcomic_api.settings import Settings
from tests.helpers import download_result


async def test_cache_identity_variants_hot_without_originals_and_legacy_retention(
    tmp_path, monkeypatch
):
    root = tmp_path / "images"
    rt = JmRuntime(SimpleNamespace(dir_rule=SimpleNamespace(base_dir=root)), None)
    cfg = Settings(pdf_dir=tmp_path / "pdf", min_free_disk_mb=0)
    service = AlbumService(lambda: rt, settings=cfg)
    calls = []

    async def download(album_id, opt, **kwargs):
        calls.append(album_id)
        folder = root / f"[{album_id}]相同名称"
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(3):
            path = folder / f"{i}.png"
            Image.new("RGB", (20 + i, 30), "blue" if album_id == "1" else "red").save(path)
            paths.append(path)
        album = MagicMock()
        album.name, album.page_count = "相同名称", 3
        return download_result(album, paths)

    monkeypatch.setattr(jm_client, "download_with_retry", download)
    cfg.pdf_path.mkdir()
    legacy = cfg.pdf_path / "相同名称.pdf"
    legacy.write_bytes(b"legacy remains")
    options = dict(enable_pwd=False, title_type=1)
    a, b = await asyncio.gather(
        service.get_or_build("1", cfg.pdf_path, **options),
        service.get_or_build("2", cfg.pdf_path, **options),
    )
    assert a.filename == b.filename == "相同名称.pdf"
    assert a.path != b.path
    encrypted = await service.get_or_build("1", cfg.pdf_path, **(options | {"enable_pwd": True}))
    quality = await service.get_or_build("1", cfg.pdf_path, jpeg_quality=20, **options)
    assert len({a.path, b.path, encrypted.path, quality.path}) == 4
    with encrypted.path.open("rb") as stream:
        reader = PdfReader(stream)
        assert reader.decrypt("1")
        assert len(reader.pages) == 3
        reader.close()

    # Hot hits must not enumerate originals or open PdfReader.
    def forbidden(*args, **kwargs):
        raise AssertionError("hot cache touched originals or PDF reader")

    monkeypatch.setattr(pdf_builder, "PdfReader", forbidden)
    monkeypatch.setattr(service, "metadata", forbidden)
    for path in root.rglob("*.png"):
        path.unlink()
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    hit = await service.get_or_build("jM01", cfg.pdf_path, **options)
    assert hit.path == a.path
    assert sorted(calls) == ["1", "2"]
    assert legacy.read_bytes() == b"legacy remains"


async def test_same_full_build_is_coalesced_and_full_shard_share_page_cache(tmp_path, monkeypatch):
    from jmcomic_api.services.album import AlbumImages
    from jmcomic_api.services.storage import digest_file, identity

    paths = []
    for i in range(4):
        p = tmp_path / f"{i}.png"
        Image.new("RGB", (20 + i, 30), "red").save(p)
        paths.append(p)
    cfg = Settings(pdf_dir=tmp_path / "pdf", min_free_disk_mb=0)
    service = AlbumService(lambda: JmRuntime(None, None), settings=cfg)
    digests = [digest_file(p) for p in paths]
    meta = AlbumImages("1", "Test", tmp_path, paths, identity(digests), digests)

    async def metadata(*args):
        return meta

    monkeypatch.setattr(service, "metadata", metadata)
    results = await asyncio.gather(
        *[
            service.get_or_build("1", cfg.pdf_path, enable_pwd=False, title_type=i % 3)
            for i in range(6)
        ],
        service.build_shard("1", 1, 2, tmp_path / "shards", enable_pwd=False),
    )
    assert len({result.path for result in results[:-1]}) == 1
    cached = list((cfg.pdf_path / "v2" / "pages").glob("*.pdf"))
    assert len(cached) == 4
    assert all(p.stat().st_size > 0 for p in cached)


async def test_manifest_change_invalidates_related_pdf_variants_without_removing_old_paths(
    tmp_path, monkeypatch
):
    from jmcomic_api.services.storage import digest_file

    root = tmp_path / "images"
    folder = root / "[1]Test"
    folder.mkdir(parents=True)
    image = folder / "one.png"
    Image.new("RGB", (20, 30), "red").save(image)
    album = MagicMock()
    album.name, album.page_count = "Test", 1

    async def download(*args, **kwargs):
        return download_result(album, [image])

    monkeypatch.setattr(jm_client, "download_with_retry", download)
    rt = JmRuntime(SimpleNamespace(dir_rule=SimpleNamespace(base_dir=root)), None)
    cfg = Settings(pdf_dir=tmp_path / "pdf", min_free_disk_mb=0)
    service = AlbumService(lambda: rt, settings=cfg)
    original = await service.get_or_build("1", cfg.pdf_path, enable_pwd=False, title_type=2)
    old_hash = digest_file(original.path)
    # Valid replacement image: refresh the trusted manifest through metadata.
    Image.new("RGB", (22, 30), "blue").save(image)
    await service.metadata("1")
    replacement = await service.get_or_build("1", cfg.pdf_path, enable_pwd=False, title_type=2)
    assert replacement.path != original.path
    assert digest_file(original.path) == old_hash
    # The path is pinned throughout service-to-response handoff.
    from jmcomic_api.services.storage import leases

    assert not leases.remove(replacement.path)
    # File attribute changes invalidate the PDF index too.
    replacement.path.write_bytes(b"corrupt PDF")
    repaired = await service.get_or_build("1", cfg.pdf_path, enable_pwd=False, title_type=2)
    assert repaired.path.read_bytes().startswith(b"%PDF")
