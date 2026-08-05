"""FastAPI `/api/v1` 層(設計書 §7.1)。`abist-kb api serve` でスタンドアロン起動する HTTP 面。"""

from __future__ import annotations

from .app import create_api_app, register_api_routes

__all__ = ["create_api_app", "register_api_routes"]
