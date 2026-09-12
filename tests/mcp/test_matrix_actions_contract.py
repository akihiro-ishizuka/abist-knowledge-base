"""マトリクス操作の MCP 到達性契約。

正本は `design/ui-action-matrix.yaml`。各操作について:

1. 宣言された面(mcp)から実際に呼び出せること(ツール名の存在)。
2. 宣言されていない面には `{surface}_omitted_reason` が必須。
3. 検査機構自体が空虚でないこと。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from abist_kb.presentation.mcp import (
    jobs_tools,
    kb_admin,
    kb_search,
    kb_video,
    kb_visualize,
)

MATRIX_PATH = Path(__file__).resolve().parents[2] / "design" / "ui-action-matrix.yaml"
ALL_SURFACES = ("mcp",)

# action_id -> MCP ツール名(サーバー横断)。マトリクスの mcp 面の到達先。
MCP_TOOL_BY_ACTION: dict[str, str] = {
    "source_add": "source_add",
    "source_edit": "source_edit",
    "source_remove": "source_remove",
    "source_test_connection": "source_test_connection",
    "batch_add": "batch_add",
    "batch_edit": "batch_edit",
    "batch_remove": "batch_remove",
    "batch_run": "batch_run",
    "job_cancel": "cancel_job",
    "job_retry": "job_retry",
    "document_update_metadata": "document_update_metadata",
    "document_delete": "document_delete",
    "search_run": "search_kb",
    "visualization_validate": "visualization_validate",
    "visualization_submit_render": "start_render_scene",
    "visualization_deps": "check_visualize_deps",
    "visualization_list": "list_visualizations",
    "visualization_detail": "get_visualization",
    "video_create": "create_video_project",
    "video_submit_render": "start_render_video",
    "video_list": "list_videos",
    "video_detail": "get_video",
    "video_run_qa": "run_video_qa",
    "video_approve": "approve_video",
    "video_set_distribution": "set_distribution",
    "video_request_public_review": "request_public_review",
    "quality_run_integrity": "quality_run_integrity",
    "quality_apply_integrity_updates": "quality_apply_integrity_updates",
    "quality_run_duplicates": "quality_run_duplicates",
    "quality_run_contradictions": "quality_run_contradictions",
    "quality_preview_backfill_metadata": "quality_preview_backfill_metadata",
    "quality_apply_backfill_metadata": "quality_apply_backfill_metadata",
}


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def iter_actions(matrix: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    result: list[tuple[str, str, dict[str, Any]]] = []
    for screen_id, screen in (matrix.get("screens") or {}).items():
        for action_id, action in (screen.get("actions") or {}).items():
            result.append((screen_id, action_id, action))
    return result


def actions_for_surface(matrix: dict[str, Any], surface: str) -> list[str]:
    return sorted(
        action_id
        for _screen_id, action_id, action in iter_actions(matrix)
        if surface in (action.get("surfaces") or [])
    )


def missing_registry_entries(
    matrix: dict[str, Any], registry: dict[str, Any], surface: str
) -> list[str]:
    return [
        action_id for action_id in actions_for_surface(matrix, surface) if action_id not in registry
    ]


def all_mcp_tool_names() -> set[str]:
    names: set[str] = set()
    for listing in (
        kb_admin.list_tools(),
        jobs_tools.list_tools(),
        kb_search.list_tools(),
        kb_visualize.list_tools(),
        kb_video.list_tools(),
    ):
        names.update(tool.name for tool in listing)
    return names


THE_MATRIX = load_matrix()


def test_every_action_declares_a_reason_for_every_omitted_surface() -> None:
    matrix = load_matrix()
    failures: list[str] = []
    for screen_id, action_id, action in iter_actions(matrix):
        surfaces = set(action.get("surfaces") or [])
        unknown = surfaces - set(ALL_SURFACES)
        if unknown:
            failures.append(f"{screen_id}.{action_id}: 未知の surface {sorted(unknown)}")
        for surface in ALL_SURFACES:
            if surface not in surfaces and not action.get(f"{surface}_omitted_reason"):
                failures.append(f"{screen_id}.{action_id}: {surface}_omitted_reason が無い")
    assert failures == [], "\n".join(failures)


def test_matrix_has_no_orphaned_actions_without_any_surface() -> None:
    matrix = load_matrix()
    orphans = [
        f"{screen_id}.{action_id}"
        for screen_id, action_id, action in iter_actions(matrix)
        if not (action.get("surfaces") or [])
    ]
    assert orphans == [], f"どの面にも配線されていない操作: {orphans}"


def test_matrix_surfaces_are_mcp_only() -> None:
    """Web/TUI/REST 面は残っていないこと。"""
    for screen_id, action_id, action in iter_actions(load_matrix()):
        surfaces = set(action.get("surfaces") or [])
        assert "web" not in surfaces, f"{screen_id}.{action_id} に web が残っている"
        assert "tui" not in surfaces, f"{screen_id}.{action_id} に tui が残っている"
        assert "api" not in surfaces, f"{screen_id}.{action_id} に api が残っている"


def test_every_mcp_action_has_a_tool_mapping() -> None:
    missing = missing_registry_entries(THE_MATRIX, MCP_TOOL_BY_ACTION, "mcp")
    assert missing == [], f"MCP 到達性チェックが未登録の操作: {missing}"


@pytest.mark.parametrize("action_id", actions_for_surface(THE_MATRIX, "mcp"))
def test_mcp_action_tool_is_listed(action_id: str) -> None:
    tool_name = MCP_TOOL_BY_ACTION[action_id]
    assert tool_name in all_mcp_tool_names(), (
        f"{action_id} → MCP ツール {tool_name!r} が list_tools に無い"
    )


def test_mcp_registry_completeness_check_is_non_vacuous() -> None:
    broken = dict(MCP_TOOL_BY_ACTION)
    dropped = next(iter(broken))
    del broken[dropped]
    missing = missing_registry_entries(THE_MATRIX, broken, "mcp")
    assert dropped in missing
