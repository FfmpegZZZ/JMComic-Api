from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ApiResponse(BaseModel):
    """Legacy response envelope. Kept for backward compat with the Flask era."""

    success: bool
    message: str
    data: Any | None = None
    name: str | None = None
