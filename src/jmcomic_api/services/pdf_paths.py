"""Filename and path derivation for the album → PDF mapping."""

from __future__ import annotations

import re
from pathlib import Path

_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def clean_album_id(raw: str) -> str:
    """Strip a leading ``JM`` prefix so ``JM12345`` and ``12345`` share a cache slot."""
    return raw.removeprefix("JM").removeprefix("jm").removeprefix("Jm")


def sanitize_filename(name: str) -> str:
    return _INVALID_NAME_CHARS.sub("", name).strip()


def derive_pdf_filename(
    album_id: str,
    title: str,
    title_type: int,
    *,
    jpeg_quality: int | None = None,
) -> str:
    """0 → ``<id>.pdf``; 1 → ``<title>.pdf``; 2/other → ``[<id>] <title>.pdf``.

    When ``jpeg_quality`` is set, a ``.qN`` suffix is inserted before ``.pdf`` so
    the lossy variant never overwrites the lossless cache (and vice versa).
    """
    safe = sanitize_filename(title)
    suffix = f".q{jpeg_quality}" if jpeg_quality is not None else ""
    if title_type == 0:
        return f"{album_id}{suffix}.pdf"
    if title_type == 1:
        return f"{safe}{suffix}.pdf"
    return f"[{album_id}] {safe}{suffix}.pdf"


def webp_folder(base_dir: str | Path, album_id: str, title: str) -> Path:
    return Path(base_dir) / f"[{album_id}]{title}"


def find_existing_album_title(base_dir: str | Path, album_id: str) -> str | None:
    """Return the album title if the webp folder exists, else None.

    Folder convention: ``[<id>]<title>``. ``album_id`` should already be the
    clean (no ``JM``) form. If multiple folders match (e.g. manual copies),
    the lexicographically smallest name wins — deterministic across platforms,
    unlike ``Path.iterdir()``.
    """
    root = Path(base_dir)
    if not root.exists():
        return None
    pattern = re.compile(rf"\[{re.escape(album_id)}\]")
    matches = sorted(item for item in root.iterdir() if item.is_dir() and pattern.match(item.name))
    if not matches:
        return None
    name = matches[0].name
    idx = name.find("]")
    return name[idx + 1 :].strip() if idx != -1 else None
