"""Album singleflight, trusted image manifests and versioned PDF indexes."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from pathlib import Path

from jmcomic import JmSyncRuntime
from jmcomic.jm_exception import PartialDownloadFailedException

from jmcomic_api import metrics
from jmcomic_api.concurrency.executors import BlockingPools
from jmcomic_api.concurrency.keyed_lock import KeyedAsyncLock
from jmcomic_api.concurrency.tasks import Lane, TaskManager
from jmcomic_api.services import jm_client
from jmcomic_api.services.build_process import build_pdf_process
from jmcomic_api.services.pdf_paths import clean_album_id, derive_pdf_filename, webp_folder
from jmcomic_api.services.storage import (
    FilePin,
    atomic_json,
    confined,
    disk_admission,
    file_attributes,
    identity,
    leases,
    make_manifest,
    manifest_path,
    read_json,
    validate_manifest,
)
from jmcomic_api.settings import Settings

DONE_MARKER = ".done"  # Legacy marker retained; only v2 manifests are trusted.
BUILD_VERSION = "pillow-pypdf-v2"


@dataclass
class PdfArtifact:
    path: Path
    filename: str
    _pin: FilePin | None = None


@dataclass
class AlbumImages:
    album_id: str
    title: str
    folder: Path
    images: list[Path]
    fingerprint: str = ""
    digests: list[str] | None = None

    @property
    def total_pages(self):
        return len(self.images)


@dataclass
class ReliabilityConfig:
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
        lock=None,
        download_semaphore=None,
        reliability=None,
        on_download_success=None,
        manager=None,
        download_lane=None,
        pdf_lane=None,
        lock_timeout=600,
        settings=None,
    ):
        self._runtime = runtime_provider
        self._lock = lock or KeyedAsyncLock()
        self.manager = manager or TaskManager(wait_timeout=lock_timeout)
        self.settings = settings or Settings()
        self.download_lane = download_lane or Lane("download", 3, 32, 30)
        self.pdf_lane = pdf_lane or Lane("pdf", 1, 32, 30)
        self._sem = download_semaphore or asyncio.Semaphore(3)
        self._cfg = reliability or ReliabilityConfig()
        self.lock_timeout = lock_timeout
        self._on_download_success = on_download_success

    @property
    def lock(self):
        return self._lock

    def _root(self, runtime):
        return Path(runtime.opt.dir_rule.base_dir).resolve()

    def _meta(self, runtime, data):
        root = self._root(runtime)
        return AlbumImages(
            data["album_id"],
            data["title"],
            webp_folder(root, data["album_id"], data["title"]),
            [confined(root, root / e["path"]) for e in data["entries"]],
            data["fingerprint"],
            [e["digest"] for e in data["entries"]],
        )

    async def metadata(self, raw_album_id):
        rt = self._runtime()
        rt.retain()
        try:
            return await self._metadata(raw_album_id, rt)
        finally:
            rt.release()

    async def _metadata(self, raw_album_id, rt):
        if self.manager.closing:
            from jmcomic_api.concurrency.tasks import BusyError

            raise BusyError("application shutting down")
        album_id = clean_album_id(raw_album_id)

        root = self._root(rt)
        cached = await asyncio.to_thread(read_json, manifest_path(root, album_id))
        if cached and await asyncio.to_thread(validate_manifest, root, cached):
            metrics.record_cache("album", True)
            return self._meta(rt, cached)

        async def owned(job):
            async with self._lock.acquire(f"album:{album_id}", timeout=self.lock_timeout):
                root = self._root(rt)
                path = manifest_path(root, album_id)
                data = await asyncio.to_thread(read_json, path)
                if data and await asyncio.to_thread(validate_manifest, root, data):
                    metrics.record_cache("album", True)
                    return self._meta(rt, data)
                metrics.record_cache("album", False)
                # Modified images need a fresh download manifest. Remove only
                # damaged/missing cache entries so jmcomic can repair them.
                if data:

                    def remove_corrupt():
                        from PIL import Image

                        for entry in data.get("entries", []):
                            image = confined(root, root / entry["path"])
                            try:
                                with Image.open(image) as img:
                                    img.verify()
                            except (OSError, ValueError):
                                image.unlink(missing_ok=True)

                    await asyncio.to_thread(remove_corrupt)
                await asyncio.to_thread(disk_admission, root, self.settings.min_free_disk_mb)
                async with self._sem:
                    remaining = self._cfg.download_timeout_seconds - (
                        time.monotonic() - job.created
                    )
                    if remaining <= 0:
                        raise TimeoutError("download total deadline exceeded")
                    result = await jm_client.download_with_retry(
                        album_id,
                        rt.opt,
                        timeout=remaining,
                        max_attempts=self._cfg.download_retry_attempts,
                        initial_wait=self._cfg.download_retry_initial_wait,
                        max_wait=self._cfg.download_retry_max_wait,
                        sync=rt.sync if isinstance(rt.sync, JmSyncRuntime) else None,
                        job=job,
                        executor=rt.executors.download
                        if isinstance(rt.executors, BlockingPools)
                        else None,
                    )
                album, _ = result
                # Never infer page order by sorting filenames. The upstream
                # manifest sorts chapters and images by their actual index.
                manifest = getattr(result, "manifest", None)
                paths = (
                    [Path(p) for p in manifest.image_filepath_list] if manifest is not None else []
                )
                try:
                    data = await asyncio.to_thread(
                        make_manifest, root, album_id, album.name, paths, int(album.page_count)
                    )
                except (OSError, ValueError) as error:
                    metrics.record_download_outcome("partial_fail")
                    raise PartialDownloadFailedException(str(error), {}) from error
                await asyncio.to_thread(atomic_json, path, data)
                metrics.record_download_outcome("success")
                if self._on_download_success:
                    self._on_download_success()
                return self._meta(rt, data)

        return await self.manager.run(
            ("download", getattr(rt, "identity", "anonymous"), rt.generation, album_id),
            self.download_lane,
            owned,
            runtime=rt,
            total_timeout=self._cfg.download_timeout_seconds,
        )

    async def _ensure_title(self, album_id, runtime):
        return (await self.metadata(album_id)).title

    def _variant(self, quality, encrypted, shard_index=0, shard_size=0):
        return dict(
            builder=BUILD_VERSION,
            quality=quality,
            encrypted=encrypted,
            shard_index=shard_index,
            shard_size=shard_size,
            optimize=self.settings.jpeg_optimize,
        )

    def _hot(self, root, album_id, variant):
        folder = confined(root, root / "v2" / album_id)
        index = read_json(folder / "index.json")
        if not isinstance(index, dict):
            return None
        entry = index.get(identity(variant))
        if not isinstance(entry, dict):
            return None
        pin = None
        try:
            path = confined(root, folder / entry["file"])
            rt = self._runtime()
            current = (
                read_json(manifest_path(self._root(rt), album_id)) if rt.opt is not None else None
            )
            if current and entry["fingerprint"] != current["fingerprint"]:
                return None
            pin = FilePin(path)
            if file_attributes(path) != entry["attributes"]:
                pin.close()
                return None
            import os

            stat = path.stat()
            os.utime(path, ns=(time.time_ns(), stat.st_mtime_ns))
            return pin, entry
        except (OSError, KeyError, TypeError, AttributeError):
            if pin:
                pin.close()
            return None

    async def _build(self, album_id, root, variant, meta):
        paths = meta.images
        total = len(paths)
        start, end = 1, total
        if variant["shard_index"]:
            start = (variant["shard_index"] - 1) * variant["shard_size"] + 1
            end = min(start + variant["shard_size"] - 1, total)
            if start > total:
                raise ValueError(
                    f"shard {variant['shard_index']} out of range for size {variant['shard_size']}"
                )
            paths = paths[start - 1 : end]
        if not paths:
            raise FileNotFoundError(f"album {album_id} contains no images")
        fingerprint = meta.fingerprint
        if not fingerprint:
            # Private service callers may supply explicit ordered paths.
            from jmcomic_api.services.storage import digest_file

            digests = await asyncio.to_thread(lambda: [digest_file(p) for p in paths])
            fingerprint = identity(digests)
        key = identity(
            dict(album_id=album_id, fingerprint=fingerprint, start=start, end=end, **variant)
        )
        folder = confined(root, root / "v2" / album_id)
        output = confined(root, folder / f"{key}.pdf")
        password = album_id if variant["encrypted"] else None

        async def owned(job):
            async with self.pdf_lane.enter():
                hot = await asyncio.to_thread(self._hot, root, album_id, variant)
                if hot:
                    return hot
                await asyncio.to_thread(disk_admission, root, self.settings.min_free_disk_mb)
                if self.settings.page_cache_mb:
                    await asyncio.to_thread(
                        disk_admission, self.settings.pdf_path, self.settings.min_free_disk_mb
                    )
                t0 = time.monotonic()
                pin = FilePin(output)
                with leases.hold(output):
                    await build_pdf_process(
                        paths,
                        output,
                        password=password,
                        jpeg_quality=variant["quality"],
                        timeout=self._cfg.pdf_build_timeout_seconds,
                        jpeg_optimize=self.settings.jpeg_optimize,
                        page_cache_dir=str(self.settings.pdf_path / "v2" / "pages"),
                        page_cache_bytes=self.settings.page_cache_mb * 1024**2,
                        expected_digests=meta.digests[start - 1 : end] if meta.digests else None,
                    )
                    entry = dict(
                        file=output.name,
                        attributes=file_attributes(output),
                        fingerprint=fingerprint,
                        title=meta.title,
                        total=total,
                        start=start,
                        end=end,
                    )
                    # Serialize index read/modify/write across full/shard builds.
                    async with self._lock.acquire(f"index:{folder}"):
                        index_path = folder / "index.json"
                        index = await asyncio.to_thread(read_json, index_path) or {}
                        index[identity(variant)] = entry
                        await asyncio.to_thread(atomic_json, index_path, index)
                metrics.record_pdf_build_duration(
                    "shard" if variant["shard_index"] else "full", time.monotonic() - t0
                )
                return pin, entry

        return await self.manager.run(("pdf", str(output)), None, owned)

    async def get_or_build(
        self, raw_album_id, pdf_dir, *, enable_pwd, title_type, jpeg_quality=None
    ):
        if self.manager.closing:
            from jmcomic_api.concurrency.tasks import BusyError

            raise BusyError("application shutting down")
        album_id = clean_album_id(raw_album_id)
        root = Path(pdf_dir).resolve()
        variant = self._variant(jpeg_quality, enable_pwd)
        hot = await asyncio.to_thread(self._hot, root, album_id, variant)
        metrics.record_cache("pdf", bool(hot))
        if hot is None:
            meta = await self.metadata(album_id)
            hot = await self._build(album_id, root, variant, meta)
        pin, entry = hot
        return PdfArtifact(
            pin.path,
            derive_pdf_filename(album_id, entry["title"], title_type, jpeg_quality=jpeg_quality),
            pin,
        )

    async def build_shard(
        self, raw_album_id, shard_index, shard_size, cache_dir, *, enable_pwd, jpeg_quality=None
    ):
        if self.manager.closing:
            from jmcomic_api.concurrency.tasks import BusyError

            raise BusyError("application shutting down")
        album_id = clean_album_id(raw_album_id)
        root = Path(cache_dir).resolve()
        variant = self._variant(jpeg_quality, enable_pwd, shard_index, shard_size)
        hot = await asyncio.to_thread(self._hot, root, album_id, variant)
        metrics.record_cache("shard", bool(hot))
        if hot is None:
            meta = await self.metadata(album_id)
            hot = await self._build(album_id, root, variant, meta)
        pin, entry = hot
        num_shards = math.ceil(entry["total"] / shard_size)
        quality_tag = f".q{jpeg_quality}" if jpeg_quality is not None else ""
        filename = f"shard_{shard_index}_of_{num_shards}_size{shard_size}{quality_tag}.pdf"
        return (
            PdfArtifact(pin.path, filename, pin),
            entry["total"],
            entry["start"],
            entry["end"],
            entry["title"],
        )
