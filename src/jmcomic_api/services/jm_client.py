"""Adapter around jmcomic. Centralizes all upstream calls so:
- they can be mocked in one place
- sync calls run inside ``asyncio.to_thread`` (so the event loop never blocks)
- a future jmcomic API change touches only this file
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jmcomic import (
    JmAlbumDetail,
    JmApiClient,
    JmCategoryPage,
    JmModuleConfig,
    JmSearchPage,
    create_option_by_file,
    download_album,
)


@dataclass
class JmRuntime:
    """Holds the live jmcomic option + client. Rebuilt by ``reload()``."""

    opt: Any
    client: JmApiClient


def install_album_dirname_advice() -> None:
    """Force downloaded album folders to be named ``[id]title`` regardless of
    the user's ``dir_rule`` shortcut. Must run before any download.
    """
    JmModuleConfig.AFIELD_ADVICE["jmbook"] = lambda album: f"[{album.id}]{album.title}"


def load_runtime(option_file: str | Path) -> JmRuntime:
    opt = create_option_by_file(str(option_file))
    return JmRuntime(opt=opt, client=opt.new_jm_client())


# --- thin async wrappers (each runs in default thread pool) ---


async def search(client: JmApiClient, query: str, page: int) -> JmSearchPage:
    return await asyncio.to_thread(client.search_site, search_query=query, page=page)


async def get_album_detail(client: JmApiClient, album_id: str) -> JmAlbumDetail | None:
    """May return ``None`` if the upstream lookup yields no album (callers in
    ``routers/catalog.py`` handle the ``None`` case explicitly)."""
    return await asyncio.to_thread(client.get_album_detail, album_id)


async def categories_filter(
    client: JmApiClient,
    *,
    page: int,
    time: str,
    category: str,
    order_by: str,
) -> JmCategoryPage:
    return await asyncio.to_thread(
        client.categories_filter,
        page=page,
        time=time,
        category=category,
        order_by=order_by,
    )


async def download(album_id: str, opt: Any) -> tuple[Any, Iterable[Any]]:
    return await asyncio.to_thread(download_album, album_id, option=opt)
