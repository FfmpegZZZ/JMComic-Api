"""Short HTTP requests supervising bounded, reusable PDF builds."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from jmcomic.jm_exception import MissingAlbumPhotoException, PartialDownloadFailedException

from jmcomic_api.concurrency.tasks import BusyError
from jmcomic_api.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class PdfJob:
    id: str
    owner: str
    key: tuple
    created: float
    state: str = "queued"
    finished: float | None = None
    artifact: Any = None
    message: str = ""


class PdfJobs:
    def __init__(self, manager, service, settings):
        self.manager, self.service, self.settings = manager, service, settings
        self.jobs: dict[str, PdfJob] = {}
        self.capacity = settings.queue_capacity + settings.max_concurrent_downloads
        self.ttl = 1800

    def prune(self):
        now = time.monotonic()
        for key, job in list(self.jobs.items()):
            if job.finished is not None and now - job.finished >= self.ttl:
                del self.jobs[key]  # Releases the artifact's FilePin.

    def get(self, job_id, owner):
        self.prune()
        job = self.jobs.get(job_id)
        if job is None or job.owner != owner:
            raise HTTPException(404, "PDF task not found or expired; submit again")
        return job

    def submit(self, runtime, *, album_id, shard_index, encrypted, quality):
        self.prune()
        if self.manager.closing:
            raise BusyError("application shutting down")
        key = (runtime.identity, runtime.generation, album_id, shard_index, encrypted, quality,
               self.settings.pdf_shard_size)
        for existing in self.jobs.values():
            if existing.key == key and existing.state not in {"failed", "cancelled"}:
                return existing
        active = sum(job.finished is None for job in self.jobs.values())
        if active >= self.capacity or len(self.jobs) >= 128:
            raise BusyError("PDF task queue full; retry later")
        job = PdfJob(uuid.uuid4().hex, runtime.identity, key, time.monotonic())
        self.jobs[job.id] = job
        runtime.retain()

        async def build():
            try:
                job.state = "running"
                logger.info("pdf_job_started", job_id=job.id, album_id=album_id,
                            shard_index=shard_index)
                budget = (self.settings.download_timeout_seconds
                          + self.settings.pdf_build_timeout_seconds
                          + self.settings.queue_timeout_seconds * 2)
                async with asyncio.timeout(budget):
                    if shard_index:
                        result = await self.service.build_shard(
                            album_id, shard_index, self.settings.pdf_shard_size,
                            self.settings.shard_cache_path, enable_pwd=encrypted,
                            jpeg_quality=quality,
                        )
                        job.artifact = result[0]
                    else:
                        job.artifact = await self.service.get_or_build(
                            album_id, self.settings.pdf_path, enable_pwd=encrypted,
                            title_type=2, jpeg_quality=quality,
                        )
                job.state = "ready"
                logger.info("pdf_job_ready", job_id=job.id,
                            elapsed_seconds=round(time.monotonic() - job.created, 3))
            except asyncio.CancelledError:
                job.state, job.message = "cancelled", "Service stopped; submit again"
                raise
            except Exception as error:
                job.state = "failed"
                if isinstance(error, MissingAlbumPhotoException):
                    job.message = "Album not found"
                elif isinstance(error, PartialDownloadFailedException):
                    job.message = "Some images failed to download; try again later"
                elif isinstance(error, (ValueError, FileNotFoundError)):
                    job.message = "Invalid shard or no images in album"
                elif isinstance(error, TimeoutError):
                    job.message = "PDF generation timed out; try again later"
                else:
                    job.message = "PDF generation failed; check server logs or retry later"
                logger.exception("pdf_job_failed", job_id=job.id, album_id=album_id)
            finally:
                job.finished = time.monotonic()

        task = self.manager.track(build(), name=f"pdf-job:{job.id}")
        # Personal-session revocation also cancels the supervised PDF request.
        self.manager.owners[task] = runtime.identity
        def completed(done):
            self.manager.owners.pop(done, None)
            if job.finished is None:
                job.state, job.message = "cancelled", "Service stopped; submit again"
                job.finished = time.monotonic()
            runtime.release()
        task.add_done_callback(completed)
        return job

    @staticmethod
    def status(job):
        data = {"job_id": job.id, "state": job.state, "message": job.message,
                "elapsed_seconds": round(time.monotonic() - job.created, 1)}
        if job.state in {"ready", "sent"} and job.artifact is not None:
            data.update(filename=job.artifact.filename,
                        size_bytes=(job.artifact.path.stat().st_size
                                    if job.artifact.path.exists() else 0))
        return {"success": True, "data": data}

    def mark_sent(self, job):
        artifact = job.artifact
        if artifact is not None and getattr(artifact, "_pin", None) is not None:
            artifact._pin.close()
        job.state = "sent"
        job.message = "PDF 已发送并清理服务器文件"
