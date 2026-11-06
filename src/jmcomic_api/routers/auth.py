"""Personal API sessions; profiles are fetched by a fresh JM login on every read."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from jmcomic import jm_task_context
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from jmcomic_api.deps import get_runtime
from jmcomic_api.services.auth import IdentityRoute, personal_session
from jmcomic_api.services.network import UpstreamSessionExpired
from jmcomic_api.services.pdf_paths import clean_album_id

router = APIRouter(tags=["personal"], route_class=IdentityRoute)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=1024)


def registry(request):
    auth = request.app.state.auth
    if auth is None:
        raise HTTPException(404, "Personal accounts are disabled")
    return auth


def envelope(data):
    return {"success": True, "message": "OK", "data": data}


@router.post("/auth/login")
async def login(body: LoginBody, request: Request):
    auth = registry(request)
    source = request.client.host if request.client else "unknown"
    result = await auth.supervise(
        auth.login(body.username, body.password.get_secret_value(), source)
    )
    return envelope(result)


@router.get("/auth/me")
async def me(request: Request):
    auth, record = registry(request), personal_session()
    profile = await auth.supervise(auth.refresh(record))
    return envelope(
        dict(
            profile=profile,
            fetched_at=record.data["fetched_at"],
            expires_at=record.data["expires_at"],
        )
    )


@router.delete("/auth/session")
async def logout(request: Request):
    auth, record = registry(request), personal_session()
    await auth.supervise(auth.revoke(record))
    return envelope({"logged_out": True})


@router.get("/me/favorites")
async def favorites(request: Request, page: int = Query(1, ge=1), folder_id: str = "0"):
    auth = registry(request)
    personal_session()
    runtime = get_runtime(request)
    result = await request.app.state.queries.run(
        ("favorites", page, folder_id),
        lambda _job: auth.upstream(
            lambda: runtime.client.favorite_folder(page=page, folder_id=folder_id)
        ),
        runtime=runtime,
    )
    return envelope(
        dict(
            results=[{"id": aid, "title": title} for aid, title in result],
            folders=[{"id": fid, "name": name} for fid, name in result.iter_folder_id_name()],
            current_page=page,
            total=result.total,
            has_next_page=page < result.page_count,
        )
    )


@router.get("/me/daily")
async def daily(request: Request):
    auth = registry(request)
    personal_session()
    runtime = get_runtime(request)
    result = await request.app.state.queries.run(
        ("daily",),
        lambda _job: auth.upstream(lambda: runtime.client.get_daily().res_data),
        runtime=runtime,
    )
    return envelope(result)


async def write_operation(request, operation, album_id, idempotency_key):
    auth, record = registry(request), personal_session()
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(422, "Idempotency-Key must contain 1 to 128 characters")
    key = hashlib.sha256(idempotency_key.encode()).hexdigest()
    parameters = [operation, album_id]

    async def execute():
        async with (
            auth.locks.acquire(f"uid:{record.data['uid']}"),
            auth.locks.acquire(f"session:{record.digest}"),
        ):
            if record.revoked:
                raise HTTPException(401, "Session revoked")
            # A durable uid ledger survives token replacement and logout.
            ledger = hashlib.sha256(f"operations:{record.data['uid']}".encode()).hexdigest()
            receipts = await asyncio.to_thread(auth.store.read, ledger, "ledger") or {}
            receipts = {k: v for k, v in receipts.items() if time.time() - v["created"] < 86400}
            receipt = receipts.get(key)
            if receipt:
                if receipt["parameters"] != parameters:
                    raise HTTPException(409, "Idempotency-Key parameters changed")
                if receipt["state"] != "done":
                    raise HTTPException(409, "Operation outcome unknown; query current state")
                return receipt["result"]
            if len(receipts) >= 1024:
                raise HTTPException(503, "Account operation receipt capacity reached")
            receipt = dict(parameters=parameters, created=time.time(), state="pending")
            receipts[key] = receipt
            await asyncio.to_thread(auth.store.save, ledger, receipts, "ledger")
            runtime = record.runtime
            runtime.retain()
            try:

                def perform():
                    with jm_task_context(jmapi_no_retry=True):
                        if operation == "favorite":
                            data = runtime.client.toggle_favorite_album(album_id).res_data
                            return {"album_id": album_id, "action": data["type"]}
                        result = runtime.client.daily_checkin()
                        return {"already_checked_in": result.code == 1, "message": result.msg}

                try:
                    result = await auth.upstream(perform)
                except UpstreamSessionExpired:
                    # Explicit rejection proves the write did not execute.
                    receipts.pop(key)
                    await asyncio.to_thread(auth.store.save, ledger, receipts, "ledger")
                    raise
                receipt.update(state="done", result=result)
                await asyncio.to_thread(auth.store.save, ledger, receipts, "ledger")
                return result
            finally:
                runtime.release()

    return envelope(await auth.supervise(execute()))


@router.post("/me/favorites/{album_id}/toggle")
async def toggle_favorite(
    album_id: str,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
):
    return await write_operation(request, "favorite", clean_album_id(album_id), idempotency_key)


@router.post("/me/daily/check-in")
async def check_in(
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
):
    return await write_operation(request, "check-in", None, idempotency_key)
