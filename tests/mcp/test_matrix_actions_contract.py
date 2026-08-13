"""マトリクス操作の MCP / API 到達性契約(MCP-only cutover Phase 2b)。

正本は `design/ui-action-matrix.yaml`。各操作について:

1. 宣言された面(mcp/api)から実際に呼び出せること(ツール名 / ルートの存在)。
2. 破壊的操作は確認を経ること(API: `confirmed` 無しで 400、MCP kb-admin:
   preview のみで副作用なし)。
3. 宣言されていない面には `{surface}_omitted_reason` が必須。
4. 検査機構自体が空虚でないこと。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from abist_kb.config import Settings
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.api.app import create_api_app
from abist_kb.presentation.common.container import ServiceContainer
from abist_kb.presentation.mcp import (
    jobs_tools,
    kb_admin,
    kb_search,
    kb_video,
    kb_visualize,
)

MATRIX_PATH = Path(__file__).resolve().parents[2] / "design" / "ui-action-matrix.yaml"
ALL_SURFACES = ("mcp", "api")

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
    "chat_start": "chat_start",
    "chat_ask": "chat_ask",
    "chat_history": "chat_history",
    "visualization_validate": "visualization_validate",
    "visualization_submit_render": "start_render_scene",
    "visualization_deps": "check_visualize_deps",
    "visualization_list": "list_visualizations",
    "visualization_detail": "get_visualization",
    # --- 動画（video Phase 10）---
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


def destructive_actions(matrix: dict[str, Any]) -> list[str]:
    return sorted(
        action_id
        for _screen_id, action_id, action in iter_actions(matrix)
        if action.get("destructive")
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


# ---------------------------------------------------------------------------
# マトリクス整合
# ---------------------------------------------------------------------------


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


def test_matrix_surfaces_are_mcp_first() -> None:
    """Web/TUI 面は残っていないこと。"""
    for screen_id, action_id, action in iter_actions(load_matrix()):
        surfaces = set(action.get("surfaces") or [])
        assert "web" not in surfaces, f"{screen_id}.{action_id} に web が残っている"
        assert "tui" not in surfaces, f"{screen_id}.{action_id} に tui が残っている"


# ---------------------------------------------------------------------------
# MCP 到達性
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# API 到達性(旧 tests/ui から移植)
# ---------------------------------------------------------------------------

API_ROUTE_BY_ACTION: dict[str, tuple[str, str]] = {
    "source_add": ("POST", "/api/v1/sources"),
    "source_edit": ("PATCH", "/api/v1/sources/{source_id}"),
    "source_remove": ("DELETE", "/api/v1/sources/{source_id}"),
    "source_test_connection": ("POST", "/api/v1/sources/{source_id}/test"),
    "batch_add": ("POST", "/api/v1/batches"),
    "batch_edit": ("PATCH", "/api/v1/batches/{batch_id}"),
    "batch_remove": ("DELETE", "/api/v1/batches/{batch_id}"),
    "batch_run": ("POST", "/api/v1/batches/{batch_id}/run"),
    "job_cancel": ("POST", "/api/v1/jobs/{job_id}/cancel"),
    "job_retry": ("POST", "/api/v1/jobs/{job_id}/retry"),
    "document_update_metadata": ("PATCH", "/api/v1/documents/{path:path}"),
    "document_delete": ("DELETE", "/api/v1/documents/{path:path}"),
    "search_run": ("GET", "/api/v1/search"),
    "chat_start": ("POST", "/api/v1/chat/start"),
    "chat_ask": ("POST", "/api/v1/chat/ask"),
    "chat_history": ("GET", "/api/v1/chat/{conversation_id}/history"),
    "visualization_validate": ("POST", "/api/v1/visualization/validate"),
    "visualization_submit_render": ("POST", "/api/v1/visualization/render"),
    "visualization_deps": ("GET", "/api/v1/visualization/deps"),
    "visualization_list": ("GET", "/api/v1/visualizations"),
    "visualization_detail": ("GET", "/api/v1/visualizations/{visualization_id}"),
    "video_create": ("POST", "/api/v1/videos"),
    "video_submit_render": ("POST", "/api/v1/videos/{video_id}/render"),
    "video_list": ("GET", "/api/v1/videos"),
    "video_detail": ("GET", "/api/v1/videos/{video_id}"),
    "video_run_qa": ("POST", "/api/v1/videos/{video_id}/qa"),
    "video_approve": ("POST", "/api/v1/videos/{video_id}/approve"),
    "video_set_distribution": ("POST", "/api/v1/videos/{video_id}/distribution"),
    "video_request_public_review": ("POST", "/api/v1/videos/{video_id}/public-review"),
    "quality_run_integrity": ("POST", "/api/v1/quality/integrity"),
    "quality_run_duplicates": ("POST", "/api/v1/quality/duplicates"),
    "quality_run_contradictions": ("POST", "/api/v1/quality/contradictions"),
    "quality_run_backfill_metadata": ("POST", "/api/v1/quality/backfill-metadata"),
}


def _assert_route_registered(client: TestClient, method: str, path: str) -> None:
    for route in client.app.routes:
        if getattr(route, "path", None) == path and method.upper() in (
            getattr(route, "methods", None) or set()
        ):
            return
    raise AssertionError(f"API ルートが未登録: {method} {path}")


@pytest.fixture
def container(tmp_root: Path) -> ServiceContainer:
    settings = Settings(root_dir=tmp_root / "root", _env_file=None)
    settings.ensure_directories()
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    return ServiceContainer(settings, check_same_thread=False)


@pytest.fixture
def client(container: ServiceContainer) -> TestClient:
    return TestClient(create_api_app(container, bind_host="127.0.0.1"))


def test_every_api_action_has_a_registered_route_mapping() -> None:
    missing = missing_registry_entries(THE_MATRIX, API_ROUTE_BY_ACTION, "api")
    assert missing == [], f"API 到達性チェックが未登録の操作: {missing}"


@pytest.mark.parametrize("action_id", actions_for_surface(THE_MATRIX, "api"))
def test_api_action_route_is_reachable(action_id: str, client: TestClient) -> None:
    method, path = API_ROUTE_BY_ACTION[action_id]
    _assert_route_registered(client, method, path)


def _api_source_remove_without_confirmation(
    client: TestClient, container: ServiceContainer
) -> None:
    source = container.sources.add(
        type="web",
        display_name="契約テスト用ソース",
        connection={"url": "https://example.invalid"},
        output_dir="docs/contract-test",
    )
    response = client.delete(f"/api/v1/sources/{source['id']}")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert any(s["id"] == source["id"] for s in container.sources.list())


def _api_batch_remove_without_confirmation(client: TestClient, container: ServiceContainer) -> None:
    batch = container.batches.add(name="契約テスト用バッチ", type="web", items=[])
    response = client.delete(f"/api/v1/batches/{batch['id']}")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert any(b["id"] == batch["id"] for b in container.batches.list())


def _api_job_cancel_without_confirmation(client: TestClient, container: ServiceContainer) -> None:
    job = JobRepository(container.conn).submit("noop", {})
    response = client.post(f"/api/v1/jobs/{job.id}/cancel")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert container.jobs.get(job.id).state == JobState.QUEUED


def _api_document_delete_without_confirmation(
    client: TestClient, container: ServiceContainer
) -> None:
    container.documents.upsert(
        {"path": "contract-test-keep.md", "source": "manual", "status": "active"}
    )
    response = client.delete("/api/v1/documents/contract-test-keep.md")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert container.documents.get_or_none("contract-test-keep.md") is not None


API_DECLINE_CHECKS: dict[str, Callable[[TestClient, ServiceContainer], None]] = {
    "source_remove": _api_source_remove_without_confirmation,
    "batch_remove": _api_batch_remove_without_confirmation,
    "job_cancel": _api_job_cancel_without_confirmation,
    "document_delete": _api_document_delete_without_confirmation,
}


def test_every_destructive_api_action_has_a_confirmation_check() -> None:
    missing = missing_registry_entries(THE_MATRIX, API_DECLINE_CHECKS, "api")
    destructive = set(destructive_actions(THE_MATRIX))
    missing_destructive = [a for a in missing if a in destructive]
    assert missing_destructive == [], (
        f"破壊的 API 操作で confirmed ガードの検査が未登録: {missing_destructive}"
    )


@pytest.mark.parametrize("action_id", destructive_actions(THE_MATRIX))
def test_destructive_api_action_requires_confirmation(
    action_id: str, client: TestClient, container: ServiceContainer
) -> None:
    check = API_DECLINE_CHECKS.get(action_id)
    if check is None:
        pytest.skip(f"{action_id} は API 面を宣言していない")
    check(client, container)


def test_api_registry_completeness_check_is_non_vacuous() -> None:
    broken = dict(API_ROUTE_BY_ACTION)
    dropped = next(iter(broken))
    del broken[dropped]
    missing = missing_registry_entries(THE_MATRIX, broken, "api")
    assert dropped in missing
