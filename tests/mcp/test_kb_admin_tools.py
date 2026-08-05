"""kb-admin MCP ツールの契約テスト(Phase 1)。

互換3サーバー(`kb-download`/`kb-search`/`kb-visualize`)には触れない。
破壊的操作は preview(未確認)→確認+対象再指定の契約を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.config import Settings
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.infrastructure.jobs import leases
from abist_kb.presentation.common.container import ServiceContainer
from abist_kb.presentation.mcp import kb_admin
from abist_kb.presentation.mcp.kb_admin import KbAdminTools


def _payload(result: object) -> dict:
    return json.loads(result.content[0].text)  # type: ignore[attr-defined]


@pytest.fixture
def tools(tmp_root: Path) -> tuple[KbAdminTools, ServiceContainer]:
    (tmp_root / "docs").mkdir()
    (tmp_root / "data").mkdir()
    (tmp_root / "reports").mkdir()
    settings = Settings(root_dir=tmp_root)
    settings.ensure_directories()
    container = ServiceContainer(settings)
    return KbAdminTools(container), container


def _audit_count(container: ServiceContainer) -> int:
    return int(container.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])


# ---------------------------------------------------------------------------
# list_tools / annotations
# ---------------------------------------------------------------------------


def test_list_tools_includes_required_admin_names() -> None:
    names = {tool.name for tool in kb_admin.list_tools()}
    required = {
        "source_list",
        "source_add",
        "source_edit",
        "source_remove",
        "source_test_connection",
        "batch_list",
        "batch_add",
        "batch_edit",
        "batch_remove",
        "batch_run",
        "job_list",
        "job_show",
        "job_retry",
        "document_list",
        "document_detail",
        "document_update_metadata",
        "document_delete",
        "chat_start",
        "chat_ask",
        "chat_history",
        "quality_run_integrity",
        "quality_run_duplicates",
        "quality_run_contradictions",
        "quality_run_backfill_metadata",
        "visualization_validate",
    }
    assert required <= names
    assert "cancel_job" not in names  # jobs_tools 側に既にあるため重複しない


def test_destructive_tools_have_destructive_hint() -> None:
    by_name = {tool.name: tool for tool in kb_admin.list_tools()}
    for name in ("source_remove", "batch_remove", "document_delete"):
        anns = by_name[name].annotations
        assert anns is not None
        assert anns.destructiveHint is True


# ---------------------------------------------------------------------------
# Happy paths: list / add
# ---------------------------------------------------------------------------


def test_source_list_and_add(tools: tuple[KbAdminTools, ServiceContainer]) -> None:
    admin, _container = tools
    empty = _payload(admin.source_list({}))
    assert empty["ok"] is True
    assert empty["sources"] == []

    added = _payload(
        admin.source_add(
            {
                "type": "web",
                "display_name": "Example",
                "output_dir": "docs/example",
                "connection": {"url": "https://example.com"},
            }
        )
    )
    assert added["ok"] is True
    assert added["source"]["display_name"] == "Example"

    listed = _payload(admin.source_list({}))
    assert len(listed["sources"]) == 1


def test_batch_list_and_add(tools: tuple[KbAdminTools, ServiceContainer]) -> None:
    admin, _container = tools
    added = _payload(
        admin.batch_add(
            {
                "name": "バッチA",
                "type": "web",
                "output_dir": "docs/a",
                "items": [],
            }
        )
    )
    assert added["ok"] is True
    assert added["batch"]["name"] == "バッチA"

    listed = _payload(admin.batch_list({}))
    assert len(listed["batches"]) == 1


# ---------------------------------------------------------------------------
# Destructive: preview / confirm / mismatch
# ---------------------------------------------------------------------------


def test_source_remove_preview_confirm_mismatch(
    tools: tuple[KbAdminTools, ServiceContainer],
) -> None:
    admin, container = tools
    src = container.sources.add(
        type="web",
        display_name="DelSrc",
        connection={"url": "https://example.com"},
        output_dir="docs/del",
    )

    before = _audit_count(container)
    preview = _payload(admin.source_remove({"source_id": src["id"]}))
    assert preview["ok"] is True
    assert preview.get("preview") is True
    assert preview["target"]["id"] == src["id"]
    assert _audit_count(container) == before
    assert container.conn.execute("SELECT 1 FROM sources WHERE id=?", (src["id"],)).fetchone()

    mismatch = _payload(
        admin.source_remove(
            {
                "source_id": src["id"],
                "confirmed": True,
                "confirm_source_id": "wrong-id",
            }
        )
    )
    assert mismatch["ok"] is False
    assert mismatch["code"] == "INVALID_INPUT"
    assert _audit_count(container) == before
    assert container.conn.execute("SELECT 1 FROM sources WHERE id=?", (src["id"],)).fetchone()

    deleted = _payload(
        admin.source_remove(
            {
                "source_id": src["id"],
                "confirmed": True,
                "confirm_source_id": src["id"],
            }
        )
    )
    assert deleted["ok"] is True
    assert deleted.get("deleted") is True
    assert _audit_count(container) == before + 1
    assert (
        container.conn.execute("SELECT 1 FROM sources WHERE id=?", (src["id"],)).fetchone() is None
    )


def test_batch_remove_preview_and_confirm(
    tools: tuple[KbAdminTools, ServiceContainer],
) -> None:
    admin, container = tools
    batch = container.batches.add(name="削除バッチ", type="web", output_dir="docs/x", items=[])
    before = _audit_count(container)

    preview = _payload(admin.batch_remove({"batch_id": batch["id"]}))
    assert preview["ok"] is True
    assert preview["preview"] is True
    assert preview["target"]["id"] == batch["id"]
    assert _audit_count(container) == before

    bad = _payload(
        admin.batch_remove({"batch_id": batch["id"], "confirmed": True, "confirm_batch_id": "nope"})
    )
    assert bad["ok"] is False
    assert bad["code"] == "INVALID_INPUT"
    assert _audit_count(container) == before

    done = _payload(
        admin.batch_remove(
            {
                "batch_id": batch["id"],
                "confirmed": True,
                "confirm_batch_id": batch["id"],
            }
        )
    )
    assert done["ok"] is True
    assert done["deleted"] is True
    assert _audit_count(container) == before + 1


def test_document_delete_preview_and_confirm(
    tools: tuple[KbAdminTools, ServiceContainer],
) -> None:
    admin, container = tools
    container.documents.upsert({"path": "notes/a.md", "source": "web", "title": "A"})
    before = _audit_count(container)

    preview = _payload(admin.document_delete({"path": "notes/a.md"}))
    assert preview["ok"] is True
    assert preview["preview"] is True
    assert preview["target"]["path"] == "notes/a.md"
    assert _audit_count(container) == before
    assert container.documents.get_or_none("notes/a.md") is not None

    bad = _payload(
        admin.document_delete({"path": "notes/a.md", "confirmed": True, "confirm_path": "other.md"})
    )
    assert bad["ok"] is False
    assert bad["code"] == "INVALID_INPUT"
    assert container.documents.get_or_none("notes/a.md") is not None

    done = _payload(
        admin.document_delete(
            {"path": "notes/a.md", "confirmed": True, "confirm_path": "notes/a.md"}
        )
    )
    assert done["ok"] is True
    assert done["deleted"] is True
    assert _audit_count(container) == before + 1
    assert container.documents.get_or_none("notes/a.md") is None


# ---------------------------------------------------------------------------
# batch_run / jobs / quality / visualize (smoke)
# ---------------------------------------------------------------------------


def test_batch_run_queues_batch_kind_when_worker_live(
    tools: tuple[KbAdminTools, ServiceContainer],
) -> None:
    admin, container = tools
    batch = container.batches.add(name="実行バッチ", type="web", output_dir="docs/r", items=[])
    leases.try_acquire_worker_lease(container.conn, "test-worker", ttl_seconds=300)

    result = _payload(admin.batch_run({"batch_id": batch["id"]}))
    assert result["ok"] is True
    assert result["kind"] == "batch"
    assert result["state"] == "queued"
    assert result["job_id"]


def test_job_list_and_show(
    tools: tuple[KbAdminTools, ServiceContainer],
) -> None:
    admin, container = tools
    leases.try_acquire_worker_lease(container.conn, "test-worker", ttl_seconds=300)
    job = container.jobs.detach("noop", {"n": 1})

    listed = _payload(admin.job_list({}))
    assert listed["ok"] is True
    assert any(j["id"] == job.id for j in listed["jobs"])

    shown = _payload(admin.job_show({"job_id": job.id}))
    assert shown["ok"] is True
    assert shown["job"]["id"] == job.id


def test_quality_run_integrity_smoke(
    tools: tuple[KbAdminTools, ServiceContainer],
) -> None:
    admin, _container = tools
    result = _payload(admin.quality_run_integrity({"update_db": False}))
    assert result["ok"] is True
    assert "run_id" in result
    assert "totals" in result


def test_visualization_validate_invalid_spec(
    tools: tuple[KbAdminTools, ServiceContainer],
) -> None:
    admin, _container = tools
    result = _payload(admin.visualization_validate({"scene_spec": {"not": "a-spec"}}))
    assert result["ok"] is False
    assert result.get("code") == "INVALID_SCENE_SPEC"


def test_build_kb_admin_server_lists_tools(tmp_root: Path) -> None:
    from abist_kb.presentation.mcp.server_core import build_kb_admin_server

    (tmp_root / "docs").mkdir()
    (tmp_root / "data").mkdir()
    settings = Settings(root_dir=tmp_root)
    settings.ensure_directories()
    open_app_db(settings.app_db_path)

    server = build_kb_admin_server(
        docs_dir=settings.docs_dir,
        work_index_path=settings.work_index_path,
        reference_index_path=settings.reference_index_path,
        app_db_path=settings.app_db_path,
        root_dir=settings.root_dir,
        reports_dir=settings.reports_dir,
    )
    assert server.name == "kb-admin"
