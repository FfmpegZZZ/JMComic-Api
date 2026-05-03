"""Health endpoints.

- ``/health``       — Flask-era backward-compat shim, always 200 if process is up.
- ``/health/live``  — process liveness (cheap; never blocks). Use for k8s
  ``livenessProbe`` so a hung downstream doesn't restart the pod.
- ``/health/ready`` — actual readiness: jmcomic runtime initialised, PDF dir
  writable, disk has at least ``min_free_disk_mb`` MB. Use for k8s
  ``readinessProbe`` and load-balancer health checks.
"""

from __future__ import annotations

import shutil
import time

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])

# Minimum free disk required to keep accepting traffic. Below this we report
# not-ready so the LB sheds load. Tunable via ``JMAPI_MIN_FREE_DISK_MB`` if we
# ever surface it; default 500 MB is enough for a few albums of headroom.
DEFAULT_MIN_FREE_DISK_MB = 500


@router.get("/health")
async def health_legacy() -> dict:
    """Backward-compat endpoint preserved from the Flask era."""
    return {"status": "ok"}


@router.get("/health/live")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    runtime = getattr(request.app.state, "runtime", None)

    pdf_dir_ok = settings.pdf_path.exists() and settings.pdf_path.is_dir()

    # Disk space probe — pdf cache + webp downloads can blow up fast.
    try:
        usage = shutil.disk_usage(settings.pdf_path)
        free_mb = usage.free / (1024 * 1024)
    except OSError:
        free_mb = -1
    min_free_mb = getattr(settings, "min_free_disk_mb", DEFAULT_MIN_FREE_DISK_MB)
    disk_ok = free_mb >= min_free_mb

    # Last successful download — handy when triaging "why is everything stuck"
    # incidents. Set by services/album.py on success; None until first hit.
    last_success = getattr(request.app.state, "last_download_unix", None)
    last_success_age = (time.time() - last_success) if last_success else None

    components = {
        "runtime": runtime is not None,
        "pdf_dir_ok": pdf_dir_ok,
        "disk_ok": disk_ok,
        "free_disk_mb": round(free_mb, 1),
        "min_free_disk_mb": min_free_mb,
        "last_download_age_seconds": last_success_age,
    }

    if not (runtime is not None and pdf_dir_ok and disk_ok):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", **components},
        )
    return JSONResponse(content={"status": "ready", **components})
