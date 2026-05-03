"""End-to-end test for the streaming PDF builder.

Generates a few real .webp images, runs ``merge_webp_to_pdf`` with encryption,
and verifies the output via pypdf:
- correct page count
- decryptable with the album-id password
"""

from __future__ import annotations

import pytest
from PIL import Image
from pypdf import PdfReader

from jmcomic_api.services.pdf_builder import merge_webp_to_pdf


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
