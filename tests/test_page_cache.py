from __future__ import annotations

from PIL import Image
from pypdf import PdfReader

from jmcomic_api.services import page_cache
from jmcomic_api.services.pdf_builder import merge_image_paths_to_pdf


def test_full_and_shard_encode_each_page_once_and_quality_optimize_separate(tmp_path, monkeypatch):
    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (20 + index, 30), "blue").save(path)
        paths.append(path)
    calls = 0
    original = page_cache.encode_page

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(page_cache, "encode_page", counted)
    options = dict(page_cache_dir=str(tmp_path / "pages"), jpeg_quality=70)
    full = tmp_path / "full.pdf"
    merge_image_paths_to_pdf(paths, full, **options)
    merge_image_paths_to_pdf(paths[1:], tmp_path / "shard.pdf", **options)
    assert calls == 3
    merge_image_paths_to_pdf(paths, tmp_path / "quick.pdf", jpeg_optimize=False, **options)
    assert calls == 6
    with full.open("rb") as stream:
        reader = PdfReader(stream)
        # No per-page Flate pass; JPEG image streams remain unchanged DCTDecode.
        assert reader.pages[0].get_contents().get("/Filter") is None
        assert list(reader.pages[0].mediabox) == [0, 0, 20, 30]
        reader.close()


def test_page_cache_eviction_skips_shared_reader(tmp_path):
    path = tmp_path / "source.png"
    Image.new("RGB", (30, 40)).save(path)
    root = tmp_path / "cache"
    with page_cache.page_stream(path, root=root, capacity=1) as stream:
        page_cache.evict(root, 1)
        assert stream.read(4) == b"%PDF"
        assert list(root.glob("*.pdf"))
    assert not list(root.glob("*.pdf"))


def test_warm_build_scans_page_cache_once_instead_of_once_per_page(tmp_path, monkeypatch):
    paths = []
    for index in range(20):
        image = tmp_path / f"{index}.png"
        Image.new("RGB", (20 + index, 30), "blue").save(image)
        paths.append(image)
    root = tmp_path / "pages"
    options = dict(page_cache_dir=str(root), jpeg_quality=70)
    merge_image_paths_to_pdf(paths, tmp_path / "full.pdf", **options)
    scans = 0
    entries = page_cache._entries

    def counted(directory):
        nonlocal scans
        scans += 1
        return entries(directory)

    monkeypatch.setattr(page_cache, "_entries", counted)

    def forbidden(*args, **kwargs):
        raise AssertionError("warm pages must not be encoded again")

    monkeypatch.setattr(page_cache, "encode_page", forbidden)
    merge_image_paths_to_pdf(paths, tmp_path / "warm.pdf", **options)
    assert scans == 1
    from jmcomic_api.services.storage import read_json

    assert read_json(root / ".usage.json")["bytes"] == sum(
        p.stat().st_size for p in root.glob("*.pdf")
    )


def test_incremental_quota_accounting_and_active_page_overflow(tmp_path):
    from jmcomic_api.services.storage import read_json

    root = tmp_path / "pages"
    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (20 + index, 30), "red").save(path)
        paths.append(path)
    with page_cache.page_stream(paths[0], root=root, capacity=2000) as stream:
        first_size = len(stream.read())
    # Allow only one encoded page. The new page is protected while being used.
    with page_cache.page_stream(paths[1], root=root, capacity=first_size + 100) as stream:
        assert stream.read(4) == b"%PDF"
        assert len(list(root.glob("*.pdf"))) == 1
    assert read_json(root / ".usage.json")["bytes"] == sum(
        p.stat().st_size for p in root.glob("*.pdf")
    )
    # A deleted/corrupt usage ledger is rebuilt from actual files on the next miss.
    (root / ".usage.json").write_text("{broken")
    with page_cache.page_stream(paths[2], root=root, capacity=100000):
        pass
    assert read_json(root / ".usage.json")["bytes"] == sum(
        p.stat().st_size for p in root.glob("*.pdf")
    )
