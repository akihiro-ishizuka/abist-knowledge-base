"""`application.sync_service.SyncService`: esaソース/バッチ同期のオーケストレーション。

`EsaSyncRunner` 自体のシナリオ(create/unchanged/conflict等)は `test_esa.py` で
検証済みのため、ここでは「モックHTTPサーバー越しに検索・取得を行い、レポートを
`reports/sync/` へ旧形式互換で書き出す」という接着部分だけを検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

from abist_kb.application.sync_service import SyncService
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.infrastructure.db.sources_repo import SourceRepository

from .conftest import FAKE_TOKEN, MockEsaServer
from .test_esa import make_post


def _build_service(tmp_root: Path, esa_server: MockEsaServer) -> tuple[SyncService, str]:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    sources = SourceRepository(conn)
    source = sources.create(
        type="esa",
        display_name="テストesa",
        connection={
            "team": esa_server.team,
            "access_token": FAKE_TOKEN,
            "base_url": esa_server.base_url,
        },
        output_dir="docs/_svc_test",
    )
    service = SyncService(
        root_dir=tmp_root,
        docs_dir=docs_dir,
        reports_dir=tmp_root / "reports",
        documents=DocumentRepository(conn),
        sources=sources,
        batches=BatchRepository(conn),
    )
    return service, source["id"]


def test_sync_source_writes_files_and_report(tmp_root: Path, esa_server: MockEsaServer) -> None:
    esa_server.add_post(make_post(category="対象カテゴリ"))
    esa_server.add_post(make_post(category="対象カテゴリ"))
    service, source_id = _build_service(tmp_root, esa_server)

    summary, report_path = service.sync_source(source_id, categories=["対象カテゴリ"])

    assert summary.totals["added"] == 2
    assert report_path is not None
    assert report_path.name.startswith("sync-esa-")
    assert report_path.parent.name == "sync"

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["source"] == "esa"
    assert payload["totals"]["added"] == 2
    assert FAKE_TOKEN not in report_path.read_text(encoding="utf-8")

    saved_files = list((tmp_root / "docs" / "_svc_test" / "対象カテゴリ").glob("*.md"))
    assert len(saved_files) == 2


def test_sync_source_dry_run_writes_no_files_and_no_report(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    esa_server.add_post(make_post(category="対象カテゴリ"))
    service, source_id = _build_service(tmp_root, esa_server)

    summary, report_path = service.sync_source(source_id, categories=["対象カテゴリ"], dry_run=True)

    assert summary.totals["added"] == 1
    assert report_path is None
    assert not (tmp_root / "reports" / "sync").exists()
    assert not list((tmp_root / "docs").rglob("*.md"))


def test_sync_batch_uses_batch_items_as_categories(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    esa_server.add_post(make_post(category="カテゴリ1"))
    esa_server.add_post(make_post(category="カテゴリ2"))
    service, source_id = _build_service(tmp_root, esa_server)
    batch = service._batches.create(  # noqa: SLF001 - テストの都合上直接組み立てる
        name="テストバッチ",
        type="esa",
        output_dir="docs/_svc_test",
        items=[
            {"source_id": source_id, "target": "カテゴリ1"},
            {"source_id": source_id, "target": "カテゴリ2"},
        ],
    )

    summary, report_path = service.sync_batch(batch["id"])

    assert summary.totals["added"] == 2
    assert report_path is not None
    assert "テストバッチ" in report_path.name


def test_sync_all_iterates_enabled_esa_batches_only(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    esa_server.add_post(make_post(category="カテゴリA"))
    service, source_id = _build_service(tmp_root, esa_server)
    service._batches.create(  # noqa: SLF001
        name="有効バッチ",
        type="esa",
        output_dir="docs/_svc_test",
        items=[{"source_id": source_id, "target": "カテゴリA"}],
    )
    service._batches.create(  # noqa: SLF001
        name="無効バッチ",
        type="esa",
        output_dir="docs/_svc_test",
        enabled=False,
        items=[{"source_id": source_id, "target": "カテゴリA"}],
    )

    results = service.sync_all()

    assert len(results) == 1
    assert results[0]["batch_name"] == "有効バッチ"
