"""プレゼンテーション層の共通配線(MCP / API / Web / TUI)。"""

from __future__ import annotations

from abist_kb.presentation.common.container import ServiceContainer, build_container
from abist_kb.presentation.common.serialize import error_to_dict, event_to_dict, job_to_dict

__all__ = [
    "ServiceContainer",
    "build_container",
    "error_to_dict",
    "event_to_dict",
    "job_to_dict",
]
