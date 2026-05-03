"""Tests for atomic PDF writes + page-count cache validation.

Locks in:
- mid-build crash leaves no half-written PDF and no .tmp residue
- ``cache_decryptable(..., expected_pages=N)`` rejects truncated caches
- successful builds still produce a complete PDF (regression — atomic logic
  must not have broken the happy path)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from pypdf import PdfReader

from jmcomic_api.services.pdf_builder import (
    cache_decryptable,
    merge_image_paths_to_pdf,
    merge_webp_to_pdf,
)


def _three_webps(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(3):
        p = folder / f"{i:03}.webp"
        Image.new("RGB", (16, 24), "white").save(p)
        paths.append(p)
    return paths


def test_happy_path_writes_complete_pdf(tmp_path):
    paths = _three_webps(tmp_path / "src")
    out = tmp_path / "out.pdf"
    n = merge_image_paths_to_pdf(paths, out)
    assert n == 3
    assert out.exists()
    assert not (tmp_path / "out.pdf.tmp").exists()
    reader = PdfReader(str(out))
    assert len(reader.pages) == 3


def test_crash_mid_build_leaves_no_partial_or_tmp(tmp_path):
    paths = _three_webps(tmp_path / "src")
    out = tmp_path / "out.pdf"

    # Force PdfReader (used during page assembly) to blow up on the 2nd image.
    counter = {"n": 0}
    real_init = PdfReader.__init__

    def boom_init(self, *args, **kwargs):
        counter["n"] += 1
        if counter["n"] == 2:
            raise RuntimeError("simulated crash")
        return real_init(self, *args, **kwargs)

    with (
        patch.object(PdfReader, "__init__", boom_init),
        pytest.raises(RuntimeError, match="simulated crash"),
    ):
        merge_image_paths_to_pdf(paths, out)

    # Critical: neither the final PDF nor the temp file is left behind.
    assert not out.exists(), f"final PDF must not exist after crash: {out}"
    assert not (tmp_path / "out.pdf.tmp").exists(), "tmp file must be cleaned up"


def test_stale_tmp_from_previous_run_is_overwritten(tmp_path):
    """If a previous crash left a .tmp file, the next build must clean it before writing."""
    paths = _three_webps(tmp_path / "src")
    out = tmp_path / "out.pdf"
    stale_tmp = tmp_path / "out.pdf.tmp"
    stale_tmp.write_bytes(b"garbage from a previous crash")

    merge_image_paths_to_pdf(paths, out)
    assert out.exists()
    # The .tmp from before should have been replaced (and then renamed).
    assert not stale_tmp.exists()


def test_cache_decryptable_rejects_wrong_page_count(tmp_path):
    """Even a perfectly valid PDF should be rejected if its page count is wrong."""
    paths = _three_webps(tmp_path / "src")
    out = tmp_path / "out.pdf"
    merge_image_paths_to_pdf(paths, out)

    assert cache_decryptable(out, password=None, expected_pages=3) is True
    assert cache_decryptable(out, password=None, expected_pages=5) is False
    # Without expected_pages, behaviour is unchanged (encryption-only check).
    assert cache_decryptable(out, password=None) is True


def test_cache_decryptable_encryption_state_must_match(tmp_path):
    paths = _three_webps(tmp_path / "src")

    plain = tmp_path / "plain.pdf"
    merge_image_paths_to_pdf(paths, plain)
    assert cache_decryptable(plain, password=None) is True
    assert cache_decryptable(plain, password="abc") is False  # plain when enc requested

    enc = tmp_path / "enc.pdf"
    merge_image_paths_to_pdf(paths, enc, password="42")
    assert cache_decryptable(enc, password="42") is True
    assert cache_decryptable(enc, password=None) is False  # enc when plain requested
    assert cache_decryptable(enc, password="wrong") is False


def test_merge_webp_to_pdf_atomic_on_failure(tmp_path):
    """merge_webp_to_pdf goes through the same atomic path."""
    folder = tmp_path / "album"
    _three_webps(folder)
    out = tmp_path / "out.pdf"

    counter = {"n": 0}
    real_open = Image.open

    def boom_open(*args, **kwargs):
        counter["n"] += 1
        if counter["n"] == 2:
            raise RuntimeError("simulated PIL crash")
        return real_open(*args, **kwargs)

    with patch.object(Image, "open", boom_open), pytest.raises(RuntimeError):
        merge_webp_to_pdf(str(folder), str(out))

    assert not out.exists()
    assert not (tmp_path / "out.pdf.tmp").exists()
