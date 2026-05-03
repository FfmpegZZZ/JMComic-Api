"""Search / categories / album-detail endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from jmcomic_api.schemas.catalog import Category, OrderBy, TimeRange
from jmcomic_api.services import jm_client

router = APIRouter(tags=["catalog"])


@router.get("/search")
async def search_albums(
    request: Request,
    query: Annotated[str, Query(min_length=1, description="search keyword")],
    page: int = Query(1, ge=1),
):
    runtime = request.app.state.runtime
    result = await jm_client.search(runtime.client, query, page)
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
    runtime = request.app.state.runtime
    album = await jm_client.get_album_detail(runtime.client, album_id)
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
    runtime = request.app.state.runtime
    result = await jm_client.categories_filter(
        runtime.client,
        page=page,
        time=time.to_jm(),
        category=category.to_jm(),
        order_by=order_by.to_jm(),
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
