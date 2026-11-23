"""Submit, inspect and download PDFs without holding a request during a build."""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from jmcomic_api.deps import get_runtime
from jmcomic_api.routers.pdf import _normalize_passwd, _resolve_quality
from jmcomic_api.services.auth import IdentityRoute
from jmcomic_api.services.pdf_paths import clean_album_id
from jmcomic_api.services.responses import LeasedFileResponse

router = APIRouter(tags=["pdf tasks"], route_class=IdentityRoute)


class PdfJobRequest(BaseModel):
    album_id: str
    shard_index: int = Field(default=0, ge=0)
    passwd: str = "true"
    quality: str | None = None


@router.post("/pdf/jobs", status_code=202)
async def submit_pdf(request: Request, body: PdfJobRequest):
    job = request.app.state.pdf_jobs.submit(
        get_runtime(request), album_id=clean_album_id(body.album_id),
        shard_index=body.shard_index, encrypted=_normalize_passwd(body.passwd),
        quality=_resolve_quality(body.quality),
    )
    return request.app.state.pdf_jobs.status(job)


@router.get("/pdf/jobs/{job_id}")
async def pdf_status(job_id: str, request: Request):
    registry = request.app.state.pdf_jobs
    return registry.status(registry.get(job_id, get_runtime(request).identity))


@router.get("/pdf/jobs/{job_id}/file")
async def pdf_file(job_id: str, request: Request,
                   max_bytes: int | None = Query(None, ge=1)):
    job = request.app.state.pdf_jobs.get(job_id, get_runtime(request).identity)
    if job.state != "ready":
        raise HTTPException(409, "PDF task not ready", headers={"Retry-After": "2"})
    if max_bytes and job.artifact.path.stat().st_size > max_bytes:
        raise HTTPException(413, "PDF exceeds requested file size limit")
    return LeasedFileResponse(job.artifact.path, job.artifact.filename,
                             send_timeout=request.app.state.settings.response_send_timeout_seconds,
                             delete_after_send=request.app.state.settings.delete_pdf_after_response,
                             on_close=lambda: request.app.state.pdf_jobs.mark_sent(job))
