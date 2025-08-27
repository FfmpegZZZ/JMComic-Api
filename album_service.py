"""Backward compatibility layer.

原来的 `album_service.get_album_pdf_path` 已移动到 `app.services.album_service`。
当前实现使用“按相册ID串行队列”避免同一 jm_album_id 并发生成冲突。
"""
from app.services.album_service import get_album_pdf_path  # noqa: F401

__all__ = ["get_album_pdf_path"]
