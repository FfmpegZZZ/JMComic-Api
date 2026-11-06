"""Search / categories / album-detail endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from jmcomic_api.deps import get_runtime
from jmcomic_api.schemas.catalog import Category, OrderBy, TimeRange
from jmcomic_api.services import jm_client
from jmcomic_api.services.auth import IdentityRoute
from jmcomic_api.services.pdf_paths import clean_album_id

router = APIRouter(tags=["catalog"], route_class=IdentityRoute)


@router.get("/search")
async def search_albums(
    request: Request,
    query: Annotated[str, Query(min_length=1, description="search keyword")],
    page: int = Query(1, ge=1),
):
    query = query.strip()
    runtime = get_runtime(request)
    result = await request.app.state.queries.run(
        ("search", query.strip(), page),
        lambda job: jm_client.search(
            runtime.client,
            query,
            page,
            timeout=request.app.state.settings.metadata_timeout_seconds,
            job=job,
            executor=request.app.state.blocking.metadata,
        ),
        runtime=runtime,
        ttl=request.app.state.settings.query_cache_ttl_seconds,
    )
    return {
        "success": True,
        "message": "Search successful",
        "data": {
            "results": [{"id": aid, "title": title} for aid, title in result],
            "current_page": page,
            "has_next_page": page < result.page_count,
            "total": result.total,
        },
    }


@router.get("/album/{album_id}")
async def album_details(album_id: str, request: Request):
    album_id = clean_album_id(album_id)
    runtime = get_runtime(request)
    album = await request.app.state.queries.run(
        ("detail", album_id),
        lambda job: jm_client.get_album_detail(
            runtime.client,
            album_id,
            timeout=request.app.state.settings.metadata_timeout_seconds,
            job=job,
            executor=request.app.state.blocking.metadata,
        ),
        runtime=runtime,
        ttl=request.app.state.settings.detail_cache_ttl_seconds,
    )
    if not album:
        raise HTTPException(status_code=404, detail=f"Album with ID '{album_id}' not found.")
    return {
        "success": True,
        "message": "Album details retrieved",
        "data": {"id": album.id, "title": album.title, "tags": album.tags},
    }


@router.get("/categories")
async def categories(
    request: Request,
    page: int = Query(1, ge=1),
    time: TimeRange = Query(TimeRange.all),
    category: Category = Query(Category.all),
    order_by: OrderBy = Query(OrderBy.latest),
):
    runtime = get_runtime(request)
    result = await request.app.state.queries.run(
        ("categories", page, time, category, order_by),
        lambda job: jm_client.categories_filter(
            runtime.client,
            page=page,
            time=time.to_jm(),
            category=category.to_jm(),
            order_by=order_by.to_jm(),
            timeout=request.app.state.settings.metadata_timeout_seconds,
            job=job,
            executor=request.app.state.blocking.metadata,
        ),
        runtime=runtime,
        ttl=request.app.state.settings.query_cache_ttl_seconds,
    )
    return {
        "success": True,
        "message": "Categories retrieved successfully",
        "data": {
            "results": [{"id": aid, "title": title} for aid, title in result],
            "current_page": page,
            "has_next_page": page < result.page_count,
            "total": result.total,
            "params_used": {
                "time": time.to_jm(),
                "category": category.to_jm(),
                "order_by": order_by.to_jm(),
            },
        },
    }
