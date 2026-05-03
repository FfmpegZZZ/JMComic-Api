"""High-level: download an album (if needed) and produce its PDF.

All blocking calls (jmcomic downloads, PIL encode, pypdf write) happen inside
``asyncio.to_thread`` + ``asyncio.wait_for`` so the FastAPI event loop stays
responsive AND no operation can block forever. Same-album concurrency is
serialised through a per-id ``KeyedAsyncLock``; the global download semaphore
caps concurrent jmcomic activity overall.

A ``.done`` marker file inside each album folder is the source of truth for
"download fully completed". Folder existence alone is NOT sufficient — partial
downloads (e.g. a transient ``PartialDownloadFailedException``) can leave the
folder behind with missing pages.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
from dataclasses import dataclass
from pathlib import Path

from jmcomic.jm_exception import PartialDownloadFailedException

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

DONE_MARKER = ".done"


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


@dataclass
class ReliabilityConfig:
    """Knobs read from ``Settings`` and threaded through ``AlbumService``."""

    download_timeout_seconds: float = 600
    pdf_build_timeout_seconds: float = 300
    download_retry_attempts: int = 3
    download_retry_initial_wait: float = 2
    download_retry_max_wait: float = 30


class AlbumService:
    def __init__(
        self,
        runtime_provider,
        *,
        lock: KeyedAsyncLock | None = None,
        download_semaphore: asyncio.Semaphore | None = None,
        reliability: ReliabilityConfig | None = None,
    ) -> None:
        # ``runtime_provider`` is a callable returning the current ``JmRuntime``
        # so we always pick up the latest opt/client after a SIGHUP reload.
        self._runtime = runtime_provider
        self._lock = lock or KeyedAsyncLock()
        # Default semaphore size 1 keeps the test fixture deterministic; the
        # real app constructs the service with the configured cap.
        self._sem = download_semaphore or asyncio.Semaphore(1)
        self._cfg = reliability or ReliabilityConfig()

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
            folder = webp_folder(runtime.opt.dir_rule.base_dir, album_id, title)
            expected_pages = len(list_album_image_paths(folder))

            if pdf_path.exists():
                if cache_decryptable(pdf_path, password, expected_pages=expected_pages):
                    logger.info(
                        "pdf_cache_hit",
                        album=album_id,
                        path=str(pdf_path),
                        pages=expected_pages,
                    )
                    return PdfArtifact(path=pdf_path, filename=filename)
                logger.info("pdf_cache_mismatch_rebuild", album=album_id, path=str(pdf_path))
                with contextlib.suppress(OSError):
                    os.remove(pdf_path)

            await asyncio.wait_for(
                asyncio.to_thread(
                    merge_webp_to_pdf,
                    str(folder),
                    str(pdf_path),
                    password=password,
                ),
                timeout=self._cfg.pdf_build_timeout_seconds,
            )
            return PdfArtifact(path=pdf_path, filename=filename)

    # ---- shard support -----------------------------------------------------

    async def metadata(self, raw_album_id: str) -> AlbumImages:
        """Resolve total pages + title.

        If the album is not yet downloaded (or the previous attempt was
        partial — no ``.done`` marker), trigger a fresh download with retry.
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
        expected_shard_pages = end - start + 1

        album_cache = cache_dir / album_id
        album_cache.mkdir(parents=True, exist_ok=True)
        filename = f"shard_{shard_index}_of_{num_shards}_size{shard_size}.pdf"
        out_path = album_cache / filename

        password = album_id if enable_pwd else None

        async with self._lock.acquire(f"shard:{album_id}:{shard_index}:{shard_size}"):
            if out_path.exists() and cache_decryptable(
                out_path, password, expected_pages=expected_shard_pages
            ):
                logger.info(
                    "shard_cache_hit",
                    album=album_id,
                    shard=shard_index,
                    pages=expected_shard_pages,
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
                with contextlib.suppress(OSError):
                    os.remove(out_path)

            await asyncio.wait_for(
                asyncio.to_thread(
                    merge_image_paths_to_pdf,
                    slice_paths,
                    str(out_path),
                    password=password,
                ),
                timeout=self._cfg.pdf_build_timeout_seconds,
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
        """Ensure the album is downloaded *completely*, returning its title.

        - Folder + ``.done`` marker present → trust the cache, skip download.
        - Folder present but no ``.done`` (partial cache) → re-trigger
          ``download_with_retry``; jmcomic's own image cache will skip already
          downloaded files, so this only fetches the missing tail.
        - Folder absent → fresh download.

        After download, the actual file count must match ``album.page_count``
        before we touch ``.done`` — otherwise we re-raise
        ``PartialDownloadFailedException`` so callers see 503 not a silent
        partial result.
        """
        existing_title = find_existing_album_title(runtime.opt.dir_rule.base_dir, album_id)
        if existing_title is not None:
            folder = webp_folder(runtime.opt.dir_rule.base_dir, album_id, existing_title)
            if (folder / DONE_MARKER).exists():
                logger.info("album_cache_hit", album=album_id, title=existing_title)
                return existing_title
            logger.info(
                "album_partial_cache_resuming",
                album=album_id,
                title=existing_title,
                folder=str(folder),
            )

        logger.info("album_downloading", album=album_id)
        async with self._sem:  # global concurrency cap
            album, _ = await jm_client.download_with_retry(
                album_id,
                runtime.opt,
                timeout=self._cfg.download_timeout_seconds,
                max_attempts=self._cfg.download_retry_attempts,
                initial_wait=self._cfg.download_retry_initial_wait,
                max_wait=self._cfg.download_retry_max_wait,
            )

        title = album.name
        folder = webp_folder(runtime.opt.dir_rule.base_dir, album_id, title)
        actual_pages = len(list_album_image_paths(folder))
        expected_pages = getattr(album, "page_count", None) or actual_pages
        if actual_pages == 0 or actual_pages < expected_pages:
            # Don't write .done — callers retry / surface 503.
            logger.warning(
                "album_incomplete_after_download",
                album=album_id,
                expected=expected_pages,
                actual=actual_pages,
            )
            raise PartialDownloadFailedException(
                f"album {album_id}: got {actual_pages}/{expected_pages} pages",
                {"expected": expected_pages, "actual": actual_pages},
            )

        try:
            (folder / DONE_MARKER).touch()
            logger.info(
                "album_download_complete",
                album=album_id,
                title=title,
                pages=actual_pages,
            )
        except OSError as e:
            # Don't fail the request just because we couldn't drop a marker —
            # but log loudly so we notice repeated failures.
            logger.error("done_marker_write_failed", folder=str(folder), error=str(e))
        return title
