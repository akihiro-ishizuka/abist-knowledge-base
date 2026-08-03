"""FastAPI `/api/v1` 層(設計書 §7.1)。NiceGUI アプリへ統合するための HTTP 面。"""

from __future__ import annotations

from .app import create_api_app, register_api_routes

__all__ = ["create_api_app", "register_api_routes"]
