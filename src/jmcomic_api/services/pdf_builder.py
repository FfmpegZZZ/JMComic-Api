"""Stream-merge image files into a (optionally encrypted) PDF using ``pypdf``.

Each image is opened, encoded to a one-page PDF, and added to the writer; only
one decoded image is held in memory at a time.

Reliability invariants:
- **Atomic writes**: PDFs are first written to a sibling ``.tmp`` file and
  then ``os.replace()``-d into place. Crashes / cancellations therefore never
  leave a half-written PDF that future calls would mistake for a valid cache.
- **Page-count cache validation**: ``cache_decryptable`` accepts an optional
  ``expected_pages`` to detect truncated PDFs that still parse OK.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from jmcomic_api.logging_config import get_logger
from jmcomic_api.services.page_cache import evict, page_stream

logger = get_logger(__name__)


def _build_pdf_from_paths(
    paths: Iterable[Path],
    pdf_path: Path,
    password: str | None,
    *,
    jpeg_quality: int | None = None,
    jpeg_optimize: bool = True,
    page_cache_dir: str | None = None,
    page_cache_bytes: int = 512 * 1024**2,
) -> int:
    """Write a PDF atomically. Returns page count.

    Strategy:
    1. Write to a unique temporary sibling.
    2. ``os.replace()`` to final location (POSIX atomic, also works on Windows).
    3. On any exception, remove the ``.tmp`` file so we don't leak partial output.

    When ``jpeg_quality`` is supplied (1..95), each page is embedded as a
    DCT-encoded JPEG at that quality — gives a much smaller PDF for sharing.
    """
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".jmapi-build-", suffix=".pdf.tmp", dir=pdf_path.parent)
    os.close(fd)
    tmp_path = Path(temporary)

    writer = PdfWriter()
    n = 0
    try:
        if page_cache_dir and page_cache_bytes:
            evict(Path(page_cache_dir), page_cache_bytes)
        for img_path in paths:
            with page_stream(
                img_path,
                root=Path(page_cache_dir) if page_cache_dir else None,
                capacity=page_cache_bytes,
                jpeg_quality=jpeg_quality,
                jpeg_optimize=jpeg_optimize,
            ) as stream:
                reader = PdfReader(stream)
                try:
                    for page in reader.pages:
                        writer.add_page(page)
                finally:
                    reader.close()
            n += 1

        if n == 0:
            raise FileNotFoundError("no images to merge")

        if password:
            writer.encrypt(password)
            logger.info("pdf_encrypted", path=str(pdf_path))

        with open(tmp_path, "wb") as f:
            writer.write(f)
        os.replace(tmp_path, pdf_path)
    except BaseException:
        # On any failure (incl. CancelledError), clean up the tmp file.
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
    finally:
        writer.close()
    return n


def merge_webp_to_pdf(
    folder_path: str | Path,
    pdf_path: str | Path,
    password: str | None = None,
    *,
    jpeg_quality: int | None = None,
    jpeg_optimize: bool = True,
    page_cache_dir: str | None = None,
    page_cache_bytes: int = 512 * 1024**2,
) -> int:
    """Merge all .webp/.jpg/.jpeg/.png files (recursive) under ``folder_path`` into ``pdf_path``.

    Returns page count. Atomic: either the destination is the new PDF or it's
    untouched (no half-written file).
    """
    folder = Path(folder_path)
    paths = list_album_image_paths(folder)
    if not paths:
        raise FileNotFoundError(f"no image files under {folder}")
    n = _build_pdf_from_paths(
        paths,
        Path(pdf_path),
        password,
        jpeg_quality=jpeg_quality,
        jpeg_optimize=jpeg_optimize,
        page_cache_dir=page_cache_dir,
        page_cache_bytes=page_cache_bytes,
    )
    logger.info("pdf_built", path=str(pdf_path), pages=n, jpeg_quality=jpeg_quality)
    return n


def merge_image_paths_to_pdf(
    paths: list[Path],
    pdf_path: str | Path,
    password: str | None = None,
    *,
    jpeg_quality: int | None = None,
    jpeg_optimize: bool = True,
    page_cache_dir: str | None = None,
    page_cache_bytes: int = 512 * 1024**2,
) -> int:
    """Build a PDF from a pre-filtered list of image paths.

    Used by the shard route, which selects a slice of an album's pages.
    """
    if not paths:
        raise FileNotFoundError("no image paths provided")
    n = _build_pdf_from_paths(
        paths,
        Path(pdf_path),
        password,
        jpeg_quality=jpeg_quality,
        jpeg_optimize=jpeg_optimize,
        page_cache_dir=page_cache_dir,
        page_cache_bytes=page_cache_bytes,
    )
    logger.info("pdf_shard_built", path=str(pdf_path), pages=n, jpeg_quality=jpeg_quality)
    return n


def list_album_image_paths(folder: Path) -> list[Path]:
    """All .webp/.jpg/.jpeg/.png files under ``folder``, sorted by name.

    jmcomic 2.6.x supports either webp or jpeg suffix depending on
    ``download.image.suffix``; we accept both.
    """
    if not folder.exists():
        return []
    extensions = {".webp", ".jpg", ".jpeg", ".png"}
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in extensions)


def cache_decryptable(
    pdf_path: str | Path, password: str | None, *, expected_pages: int | None = None
) -> bool:
    """Probe a cached PDF: does its encrypted state and page count match?

    Returns True iff:
    - The file is parseable as a PDF.
    - Encryption state matches the request (encrypted ↔ password provided).
    - If ``expected_pages`` is supplied, the page count matches exactly.

    A truncated PDF that still parses (e.g. crash mid-write before this commit
    introduced atomic writes) would previously pass the encryption check; the
    page-count gate catches it.
    """
    try:
        with open(pdf_path, "rb") as stream:
            reader = PdfReader(stream)
            try:
                if bool(reader.is_encrypted) != (password is not None):
                    return False
                if password is not None and not reader.decrypt(password):
                    return False
                return expected_pages is None or len(reader.pages) == expected_pages
            finally:
                reader.close()
    except Exception as error:
        logger.warning("cache_read_failed", path=str(pdf_path), error=str(error))
        return False
