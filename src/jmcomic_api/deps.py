"""FastAPI dependency providers."""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, Request

from jmcomic_api.services.album import AlbumService
from jmcomic_api.services.jm_client import JmRuntime
from jmcomic_api.settings import Settings


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


def get_runtime(request: Request) -> JmRuntime:
    runtime: JmRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise RuntimeError("jmcomic runtime not initialised; lifespan did not run")
    return runtime


def get_album_service(request: Request) -> AlbumService:
    return request.app.state.album_service


SettingsDep = Depends(settings)
RuntimeDep = Depends(get_runtime)
AlbumServiceDep = Depends(get_album_service)
