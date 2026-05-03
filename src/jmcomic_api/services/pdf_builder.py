"""Stream-merge image files into a (optionally encrypted) PDF using ``pypdf``.

Each image is opened, encoded to a one-page PDF, and added to the writer; only
one decoded image is held in memory at a time. The PyPDF2-based predecessor is
gone — pypdf is the maintained fork.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter

from jmcomic_api.logging_config import get_logger

logger = get_logger(__name__)


def _build_pdf_from_paths(paths: Iterable[Path], pdf_path: Path, password: str | None) -> int:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    n = 0
    for img_path in paths:
        with Image.open(img_path) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PDF")
        buf.seek(0)
        for page in PdfReader(buf).pages:
            writer.add_page(page)
            writer.pages[-1].compress_content_streams()
        n += 1

    if n == 0:
        raise FileNotFoundError("no images to merge")

    if password:
        writer.encrypt(password)
        logger.info("pdf_encrypted", path=str(pdf_path))

    with open(pdf_path, "wb") as f:
        writer.write(f)
    return n


def merge_webp_to_pdf(
    folder_path: str | Path, pdf_path: str | Path, password: str | None = None
) -> int:
    """Merge all .webp/.jpg/.jpeg/.png files (recursive) under ``folder_path`` into ``pdf_path``.

    Returns page count.
    """
    folder = Path(folder_path)
    paths = list_album_image_paths(folder)
    if not paths:
        raise FileNotFoundError(f"no image files under {folder}")
    n = _build_pdf_from_paths(paths, Path(pdf_path), password)
    logger.info("pdf_built", path=str(pdf_path), pages=n)
    return n


def merge_image_paths_to_pdf(
    paths: list[Path], pdf_path: str | Path, password: str | None = None
) -> int:
    """Build a PDF from a pre-filtered list of image paths.

    Used by the shard route, which selects a slice of an album's pages.
    """
    if not paths:
        raise FileNotFoundError("no image paths provided")
    n = _build_pdf_from_paths(paths, Path(pdf_path), password)
    logger.info("pdf_shard_built", path=str(pdf_path), pages=n)
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


def cache_decryptable(pdf_path: str | Path, password: str | None) -> bool:
    """Probe a cached PDF: does its encrypted state match what was requested?

    Returns True iff the cache is reusable.
    """
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        logger.warning("cache_read_failed", path=str(pdf_path), error=str(e))
        return False

    if password is None:
        return not reader.is_encrypted

    if not reader.is_encrypted:
        return False
    try:
        return bool(reader.decrypt(password))
    except Exception as e:
        logger.warning("cache_decrypt_failed", path=str(pdf_path), error=str(e))
        return False
