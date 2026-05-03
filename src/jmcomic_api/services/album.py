"""High-level: download an album (if needed) and produce its PDF.

All blocking calls (jmcomic downloads, PIL encode, pypdf write) happen inside
``asyncio.to_thread`` so the FastAPI event loop stays responsive. Same-album
concurrency is serialized through a per-id ``KeyedAsyncLock``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
from dataclasses import dataclass
from pathlib import Path

from jmcomic_api.concurrency.keyed_lock import KeyedAsyncLock
from jmcomic_api.logging_config import get_logger
from jmcomic_api.services import jm_client
from jmcomic_api.services.pdf_builder import (
    cache_decryptable,
    list_album_image_paths,
    merge_image_paths_to_pdf,
    merge_webp_to_pdf,
)
from jmcomic_api.services.pdf_paths import (
    clean_album_id,
    derive_pdf_filename,
    find_existing_album_title,
    webp_folder,
)

logger = get_logger(__name__)


@dataclass
class PdfArtifact:
    path: Path
    filename: str


@dataclass
class AlbumImages:
    """Resolved on-disk state for a downloaded album."""

    album_id: str
    title: str
    folder: Path
    images: list[Path]

    @property
    def total_pages(self) -> int:
        return len(self.images)


class AlbumService:
    def __init__(self, runtime_provider, lock: KeyedAsyncLock | None = None) -> None:
        # ``runtime_provider`` is a callable returning the current ``JmRuntime``
        # so we always pick up the latest opt/client after a SIGHUP reload.
        self._runtime = runtime_provider
        self._lock = lock or KeyedAsyncLock()

    @property
    def lock(self) -> KeyedAsyncLock:
        """Expose the per-album lock so other routes (shard) can reuse it."""
        return self._lock

    # ---- full-album PDF ----------------------------------------------------

    async def get_or_build(
        self,
        raw_album_id: str,
        pdf_dir: Path,
        *,
        enable_pwd: bool,
        title_type: int,
    ) -> PdfArtifact:
        album_id = clean_album_id(raw_album_id)
        runtime = self._runtime()

        async with self._lock.acquire(f"album:{album_id}"):
            title = await self._ensure_title(album_id, runtime)
            filename = derive_pdf_filename(album_id, title, title_type)
            pdf_path = pdf_dir.resolve() / filename
            pdf_path.parent.mkdir(parents=True, exist_ok=True)

            password = album_id if enable_pwd else None

            if pdf_path.exists():
                if cache_decryptable(pdf_path, password):
                    logger.info("pdf_cache_hit", album=album_id, path=str(pdf_path))
                    return PdfArtifact(path=pdf_path, filename=filename)
                logger.info("pdf_cache_mismatch_rebuild", album=album_id, path=str(pdf_path))
                try:
                    os.remove(pdf_path)
                except OSError as e:
                    logger.error("pdf_cache_remove_failed", path=str(pdf_path), error=str(e))

            folder = webp_folder(runtime.opt.dir_rule.base_dir, album_id, title)
            await asyncio.to_thread(
                merge_webp_to_pdf,
                str(folder),
                str(pdf_path),
                password=password,
            )
            return PdfArtifact(path=pdf_path, filename=filename)

    # ---- shard support -----------------------------------------------------

    async def metadata(self, raw_album_id: str) -> AlbumImages:
        """Resolve total pages + title without forcing a download.

        If the album is not yet downloaded, downloads it. (jmcomic doesn't
        expose a cheap "page count" probe — the entry point is `download_album`,
        which is idempotent if the folder already exists.)
        """
        album_id = clean_album_id(raw_album_id)
        runtime = self._runtime()

        async with self._lock.acquire(f"album:{album_id}"):
            title = await self._ensure_title(album_id, runtime)
            folder = webp_folder(runtime.opt.dir_rule.base_dir, album_id, title)
            images = list_album_image_paths(folder)
            return AlbumImages(album_id=album_id, title=title, folder=folder, images=images)

    async def build_shard(
        self,
        raw_album_id: str,
        shard_index: int,
        shard_size: int,
        cache_dir: Path,
        *,
        enable_pwd: bool,
    ) -> tuple[PdfArtifact, int, int, int, str]:
        """Build (or reuse) a shard PDF.

        Returns ``(artifact, total_pages, start_page, end_page, title)``.
        ``shard_index`` is 1-based. The title is included so callers don't
        need a second ``metadata()`` round-trip (which would re-acquire the
        per-album lock).
        """
        album_id = clean_album_id(raw_album_id)
        meta = await self.metadata(raw_album_id)
        total = meta.total_pages
        if total == 0:
            raise FileNotFoundError(f"album {album_id} contains no images")

        num_shards = math.ceil(total / shard_size)
        if not (1 <= shard_index <= num_shards):
            raise ValueError(
                f"shard {shard_index} out of range (1..{num_shards}) for size {shard_size}"
            )

        start = (shard_index - 1) * shard_size + 1
        end = min(shard_index * shard_size, total)
        slice_paths = meta.images[start - 1 : end]

        album_cache = cache_dir / album_id
        album_cache.mkdir(parents=True, exist_ok=True)
        # Filename includes shard_size so changing it doesn't read stale caches.
        filename = f"shard_{shard_index}_of_{num_shards}_size{shard_size}.pdf"
        out_path = album_cache / filename

        password = album_id if enable_pwd else None

        async with self._lock.acquire(f"shard:{album_id}:{shard_index}:{shard_size}"):
            if out_path.exists() and cache_decryptable(out_path, password):
                logger.info(
                    "shard_cache_hit",
                    album=album_id,
                    shard=shard_index,
                    path=str(out_path),
                )
                return (
                    PdfArtifact(path=out_path, filename=filename),
                    total,
                    start,
                    end,
                    meta.title,
                )

            if out_path.exists():
                # Encryption mismatch — rebuild.
                with contextlib.suppress(OSError):
                    os.remove(out_path)

            await asyncio.to_thread(
                merge_image_paths_to_pdf,
                slice_paths,
                str(out_path),
                password=password,
            )
            return (
                PdfArtifact(path=out_path, filename=filename),
                total,
                start,
                end,
                meta.title,
            )

    # ---- internals ---------------------------------------------------------

    async def _ensure_title(self, album_id: str, runtime) -> str:
        existing = find_existing_album_title(runtime.opt.dir_rule.base_dir, album_id)
        if existing is not None:
            logger.info("album_cache_hit", album=album_id, title=existing)
            return existing
        logger.info("album_downloading", album=album_id)
        album, _ = await jm_client.download(album_id, runtime.opt)
        return album.name
