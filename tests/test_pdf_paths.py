"""Tests for the JM-prefix bug fix and filename derivation."""

from __future__ import annotations

import pytest

from jmcomic_api.services.pdf_paths import (
    clean_album_id,
    derive_pdf_filename,
    find_existing_album_title,
    sanitize_filename,
    webp_folder,
)


@pytest.mark.parametrize(
    "raw,expected",
    [("JM12345", "12345"), ("12345", "12345"), ("jm999", "999"), ("Jm7", "7"), ("777", "777")],
)
def test_clean_album_id(raw, expected):
    assert clean_album_id(raw) == expected


def test_sanitize_filename_strips_invalid_chars():
    assert sanitize_filename(r'a/b\c:d*e?f"g<h>i|j') == "abcdefghij"


def test_derive_filename_title_type_0():
    assert derive_pdf_filename("12345", "Whatever Title", 0) == "12345.pdf"


def test_derive_filename_title_type_1():
    assert derive_pdf_filename("12345", "Some Title", 1) == "Some Title.pdf"


def test_derive_filename_title_type_2_default():
    assert derive_pdf_filename("12345", "Some Title", 2) == "[12345] Some Title.pdf"


def test_derive_filename_unknown_type_defaults_to_2():
    assert derive_pdf_filename("12345", "X", 99) == "[12345] X.pdf"


def test_webp_folder_format():
    folder = webp_folder("/tmp/webp", "12345", "MyBook")
    assert folder.name == "[12345]MyBook"
    assert str(folder).endswith("/webp/[12345]MyBook")


def test_find_existing_album_title_jm_prefix_immune(tmp_path):
    """The folder convention is ``[bareid]title``. Look-up must work for both
    ``"JM12345"`` and ``"12345"`` after they're normalized via clean_album_id.
    """
    base = tmp_path / "webp"
    base.mkdir()
    (base / "[12345]My Book").mkdir()

    assert find_existing_album_title(base, clean_album_id("JM12345")) == "My Book"
    assert find_existing_album_title(base, clean_album_id("12345")) == "My Book"
    assert find_existing_album_title(base, clean_album_id("JM12345")) == "My Book"


def test_find_existing_returns_none_when_missing(tmp_path):
    base = tmp_path / "webp"
    base.mkdir()
    assert find_existing_album_title(base, "999") is None


def test_find_existing_returns_none_when_dir_absent(tmp_path):
    assert find_existing_album_title(tmp_path / "nope", "1") is None
