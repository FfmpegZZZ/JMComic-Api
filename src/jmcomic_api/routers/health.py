"""Cheap liveness and threaded storage/readiness probes."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
@router.get("/health/live")
async def liveness():
    return {"status": "ok"}


def storage_status(roots, min_mb):
    writable, free = {}, []
    for name, root in roots.items():
        try:
            # An actual exclusive create/unlink tests effective permissions,
            # including ACLs and bind mounts (os.access would miss failures).
            with tempfile.TemporaryFile(dir=root):
                pass
            writable[name] = True
            free.append(shutil.disk_usage(root).free / 1024**2)
        except OSError:
            writable[name] = False
            free.append(-1)
    minimum = min(free)
    return dict(
        storage_writable=writable,
        pdf_dir_ok=writable["pdf"],
        disk_ok=minimum >= min_mb,
        free_disk_mb=round(minimum, 1),
    )


@router.get("/health/ready")
async def readiness(request: Request):
    state = request.app.state
    cfg, runtime = state.settings, getattr(state, "runtime", None)
    roots = dict(pdf=cfg.pdf_path, shard=cfg.shard_cache_path)
    if cfg.auth_enabled:
        roots["sessions"] = cfg.auth_state_dir.resolve()
    if runtime is not None:
        roots["images"] = Path(runtime.opt.dir_rule.base_dir).resolve()
    result = await asyncio.to_thread(storage_status, roots, cfg.min_free_disk_mb)
    last = state.last_download_unix
    closing = state.manager.closing
    ready = (
        runtime is not None
        and all(result["storage_writable"].values())
        and result["disk_ok"]
        and not closing
    )
    return JSONResponse(
        status_code=200 if ready else 503,
        content=dict(
            status="ready" if ready else "not_ready",
            runtime=runtime is not None,
            closing=closing,
            min_free_disk_mb=cfg.min_free_disk_mb,
            last_download_age_seconds=time.time() - last if last else None,
            **result,
        ),
    )
