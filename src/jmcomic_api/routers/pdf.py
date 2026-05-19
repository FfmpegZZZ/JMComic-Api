"""Album → PDF endpoints. Streaming by default, base64 JSON via flag."""

from __future__ import annotations

import base64
import math
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from jmcomic_api.deps import AlbumServiceDep, settings
from jmcomic_api.services.album import AlbumService

router = APIRouter(tags=["pdf"])


def _normalize_passwd(value: str) -> bool:
    """Match Flask-era semantics: ``'false'``/``'0'`` → False, else True."""
    return value.lower() not in ("false", "0")


# Preset → JPEG quality. Picked so each tier is visibly distinct in size on a
# typical comic album (see README): small ~ -55%, medium ~ -38%, large ~ -15%.
_QUALITY_PRESETS: dict[str, int] = {
    "small": 20,
    "medium": 45,
    "large": 70,
    "小": 20,
    "中": 45,
    "大": 70,
    "s": 20,
    "m": 45,
    "l": 70,
}


def _resolve_quality(value: str | None) -> int | None:
    """Accept preset name (small/medium/large + CN aliases) or 1-95 int.

    Returns ``None`` when the caller didn't pass anything — native Pillow
    default (no extra recompression knob).
    """
    if value is None or value == "":
        return None
    key = value.strip().lower()
    if key in _QUALITY_PRESETS:
        return _QUALITY_PRESETS[key]
    try:
        n = int(key)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=(
                f"quality '{value}' invalid; expected small/medium/large or an integer 1-95"
            ),
        ) from e
    if not (1 <= n <= 95):
        raise HTTPException(
            status_code=422,
            detail=f"quality {n} out of range; integer must be 1-95",
        )
    return n


# Legacy URL: ``/get_pdf/<id>?passwd=&Titletype=&pdf=true``
@router.get("/get_pdf/{album_id}")
async def get_pdf_legacy(
    album_id: str,
    album_service: Annotated[AlbumService, AlbumServiceDep],
    settings_dep=Depends(settings),
    passwd: str = Query("true"),
    Titletype: int = Query(2),
    pdf: str = Query("false"),
    quality: str | None = Query(
        None,
        description=(
            "Compression preset (small/medium/large or 小/中/大) "
            "or raw JPEG quality 1-95. Omit for native (largest)."
        ),
    ),
):
    artifact = await album_service.get_or_build(
        album_id,
        settings_dep.pdf_path,
        enable_pwd=_normalize_passwd(passwd),
        title_type=Titletype,
        jpeg_quality=_resolve_quality(quality),
    )
    if not artifact.path.exists():
        raise HTTPException(status_code=404, detail="PDF 文件不存在")

    if pdf.lower() == "true":
        return FileResponse(
            path=artifact.path,
            media_type="application/pdf",
            filename=artifact.filename,
        )

    encoded = base64.b64encode(artifact.path.read_bytes()).decode("utf-8")
    return {
        "success": True,
        "message": "PDF 获取成功",
        "name": artifact.filename,
        "data": encoded,
    }


@router.get("/get_pdf_path/{album_id}")
async def get_pdf_path_legacy(
    album_id: str,
    album_service: Annotated[AlbumService, AlbumServiceDep],
    settings_dep=Depends(settings),
    passwd: str = Query("true"),
    Titletype: int = Query(2),
    quality: str | None = Query(
        None,
        description=(
            "Compression preset (small/medium/large or 小/中/大) "
            "or raw JPEG quality 1-95. Omit for native (largest)."
        ),
    ),
):
    artifact = await album_service.get_or_build(
        album_id,
        settings_dep.pdf_path,
        enable_pwd=_normalize_passwd(passwd),
        title_type=Titletype,
        jpeg_quality=_resolve_quality(quality),
    )
    return {
        "success": True,
        "message": "PDF 获取成功",
        "data": os.path.abspath(artifact.path),
        "name": artifact.filename,
    }


# ---- Sharded endpoints (ported from the develop branch) --------------------


@router.get("/pdf/info/{album_id}")
async def pdf_shard_info(
    album_id: str,
    album_service: Annotated[AlbumService, AlbumServiceDep],
    settings_dep=Depends(settings),
):
    """Resolve total page count and shard layout for an album.

    The album is downloaded if not yet present. Shard size is server-configured
    (``JMAPI_PDF_SHARD_SIZE``, default 100). Returns a JSON like::

        {
          "success": true,
          "data": {
            "jm_album_id": "12345",
            "title": "Some Album",
            "total_pages": 237,
            "shard_size": 100,
            "shards": [{"shard_index": 1, "start_page": 1, "end_page": 100}, ...]
          }
        }
    """
    shard_size = settings_dep.pdf_shard_size
    meta = await album_service.metadata(album_id)
    total = meta.total_pages

    if total == 0:
        return {
            "success": True,
            "message": "Album found, but contains no images.",
            "data": {
                "jm_album_id": meta.album_id,
                "title": meta.title,
                "total_pages": 0,
                "shard_size": shard_size,
                "shards": [],
            },
        }

    num_shards = math.ceil(total / shard_size)
    shards = [
        {
            "shard_index": i + 1,
            "start_page": i * shard_size + 1,
            "end_page": min((i + 1) * shard_size, total),
        }
        for i in range(num_shards)
    ]
    return {
        "success": True,
        "message": "PDF shard info retrieved successfully",
        "data": {
            "jm_album_id": meta.album_id,
            "title": meta.title,
            "total_pages": total,
            "shard_size": shard_size,
            "shards": shards,
        },
    }


@router.get("/pdf/shard/{album_id}/{shard_index}")
async def pdf_shard(
    album_id: str,
    shard_index: int,
    album_service: Annotated[AlbumService, AlbumServiceDep],
    settings_dep=Depends(settings),
    pdf: bool = Query(False, description="If true, stream raw application/pdf instead of JSON."),
    passwd: str = Query("true"),
    quality: str | None = Query(
        None,
        description=(
            "Compression preset (small/medium/large or 小/中/大) "
            "or raw JPEG quality 1-95. Omit for native (largest)."
        ),
    ),
):
    """Build (or reuse cache) the Nth shard PDF for an album."""
    if shard_index <= 0:
        raise HTTPException(status_code=422, detail="shard_index must be >= 1")

    try:
        artifact, total, start, end, title = await album_service.build_shard(
            album_id,
            shard_index=shard_index,
            shard_size=settings_dep.pdf_shard_size,
            cache_dir=settings_dep.shard_cache_path,
            enable_pwd=_normalize_passwd(passwd),
            jpeg_quality=_resolve_quality(quality),
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    if pdf:
        return FileResponse(
            path=artifact.path,
            media_type="application/pdf",
            filename=artifact.filename,
        )

    encoded = base64.b64encode(artifact.path.read_bytes()).decode("utf-8")
    return {
        "success": True,
        "message": "PDF shard generated successfully",
        "title": title,
        "shard_index": shard_index,
        "total_pages": total,
        "start_page": start,
        "end_page": end,
        "data": encoded,
    }
