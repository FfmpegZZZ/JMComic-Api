"""End-to-end + unit tests for the streaming PDF builder.

Generates a few real .webp images, runs the builder with encryption, and
verifies the output via pypdf:
- correct page count
- decryptable with the album-id password
- ``list_album_image_paths`` filters/sorts as expected
- ``merge_image_paths_to_pdf`` raises on empty input
"""

from __future__ import annotations

import pytest
from PIL import Image
from pypdf import PdfReader

from jmcomic_api.services.pdf_builder import (
    list_album_image_paths,
    merge_image_paths_to_pdf,
    merge_webp_to_pdf,
)


@pytest.mark.e2e
def test_streaming_merge_with_encryption(tmp_path):
    folder = tmp_path / "webp_album"
    folder.mkdir()
    # 5 small distinct images
    for i, color in enumerate(["red", "green", "blue", "yellow", "purple"]):
        img = Image.new("RGB", (32, 48), color=color)
        img.save(folder / f"{i:03}.webp", format="WEBP")

    pdf_path = tmp_path / "out.pdf"
    merge_webp_to_pdf(str(folder), str(pdf_path), password="12345")

    assert pdf_path.exists()

    reader = PdfReader(str(pdf_path))
    assert reader.is_encrypted
    assert reader.decrypt("12345"), "PDF should decrypt with the album-id password"
    assert len(reader.pages) == 5


@pytest.mark.e2e
def test_streaming_merge_without_encryption(tmp_path):
    folder = tmp_path / "webp_plain"
    folder.mkdir()
    for i in range(3):
        Image.new("RGB", (16, 24), "white").save(folder / f"{i}.webp", format="WEBP")

    pdf_path = tmp_path / "plain.pdf"
    merge_webp_to_pdf(str(folder), str(pdf_path))

    reader = PdfReader(str(pdf_path))
    assert not reader.is_encrypted
    assert len(reader.pages) == 3


def test_empty_folder_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        merge_webp_to_pdf(str(tmp_path), str(tmp_path / "x.pdf"))


def test_list_album_image_paths_filters_and_sorts(tmp_path):
    folder = tmp_path / "album"
    folder.mkdir()
    # Mixed extensions, intentionally unsorted.
    for name in ("002.webp", "001.webp", "010.jpg", "005.jpeg"):
        Image.new("RGB", (16, 16), "white").save(folder / name)
    # Should be ignored:
    (folder / "readme.txt").write_text("not an image")
    (folder / ".DS_Store").write_bytes(b"")

    paths = list_album_image_paths(folder)
    assert [p.name for p in paths] == ["001.webp", "002.webp", "005.jpeg", "010.jpg"]


def test_list_album_image_paths_returns_empty_when_dir_missing(tmp_path):
    assert list_album_image_paths(tmp_path / "nope") == []


def test_merge_image_paths_to_pdf_empty_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        merge_image_paths_to_pdf([], tmp_path / "out.pdf")


@pytest.mark.e2e
def test_merge_image_paths_to_pdf_explicit_list(tmp_path):
    """Pass a deliberately reverse-ordered slice — output PDF should follow
    the order given, not re-sorted (the route layer is responsible for ordering).
    """
    a = tmp_path / "a.webp"
    b = tmp_path / "b.webp"
    Image.new("RGB", (16, 16), "white").save(a)
    Image.new("RGB", (16, 16), "white").save(b)

    out = tmp_path / "out.pdf"
    pages = merge_image_paths_to_pdf([b, a], out, password=None)
    assert pages == 2
    reader = PdfReader(str(out))
    assert not reader.is_encrypted
    assert len(reader.pages) == 2
