from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_legacy() -> dict:
    """Backward-compat endpoint preserved from the Flask era."""
    return {"status": "ok"}


@router.get("/health/live")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    runtime = getattr(request.app.state, "runtime", None)
    settings = request.app.state.settings
    pdf_dir_ok = settings.pdf_path.exists() and settings.pdf_path.is_dir()
    if runtime is None or not pdf_dir_ok:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "runtime": runtime is not None,
                "pdf_dir_ok": pdf_dir_ok,
            },
        )
    return JSONResponse(content={"status": "ready"})
