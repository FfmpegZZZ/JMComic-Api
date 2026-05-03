from __future__ import annotations

from pydantic import BaseModel


class PdfJsonResponse(BaseModel):
    """Legacy JSON-encoded PDF response, kept for backward compat."""

    success: bool
    message: str
    name: str
    data: str  # base64-encoded PDF


class PdfPathResponse(BaseModel):
    success: bool
    message: str
    name: str
    data: str  # absolute filesystem path


class ShardRange(BaseModel):
    shard_index: int
    start_page: int
    end_page: int


class ShardInfoData(BaseModel):
    jm_album_id: str
    title: str
    total_pages: int
    shard_size: int
    shards: list[ShardRange]


class ShardInfoResponse(BaseModel):
    success: bool
    message: str
    data: ShardInfoData


class ShardJsonResponse(BaseModel):
    success: bool
    message: str
    title: str
    shard_index: int
    total_pages: int
    start_page: int
    end_page: int
    data: str  # base64
