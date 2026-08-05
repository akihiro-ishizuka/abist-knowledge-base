"""プレゼンテーション層共通: Application Service の束。

Web / TUI / API / MCP が同じ `ServiceContainer` 経由で `application/` へ
到達する。MCP ツールハンドラは画面 HTML ではなく本 Container の
サービス属性を直接呼ぶ(MCP-only cutover Phase 1)。
"""

from __future__ import annotations

from abist_kb.presentation.common.container import (
    WEB_WORKER_HANDLERS,
    WEB_WORKER_RESOURCES,
    ServiceContainer,
    build_container,
)

__all__ = [
    "WEB_WORKER_HANDLERS",
    "WEB_WORKER_RESOURCES",
    "ServiceContainer",
    "build_container",
]
