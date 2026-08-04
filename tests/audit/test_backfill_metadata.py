"""`backfill-metadata` の移植: dry-run既定・4キーのみ・ハッシュ検証・ロールバック。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from abist_kb.application.audit.backfill_metadata import (
    BACKFILL_KEYS,
    BackfillMetadataService,
    plan_document,
    run_backfill_inline,
)
from abist_kb.domain.errors import AppError
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema


@pytest.fixture
def conn(tmp_root: Path):
    connection = connect(tmp_root / "app.sqlite")
    ensure_app_schema(connection)
    return connection


def test_backfill_keys_are_exactly_four() -> None:
    assert BACKFILL_KEYS == ("source", "managed_by", "document_type", "status")


def test_plan_document_fills_only_missing_keys() -> None:
    content = "---\nstatus: active\n---\nbody\n"
    record = {
        "source": "esa",
        "managed_by": "esa-sync",
        "document_type": "article",
        "status": "archived",
    }
    plan = plan_document("a.md", content, record)
    assert plan.action == "write"
    # status は既にファイルにあるので触らない(DB の archived で上書きしない)
    assert "status" not in plan.values
    assert plan.values == {"source": "esa", "managed_by": "esa-sync", "document_type": "article"}


def test_plan_document_unchanged_when_all_keys_present() -> None:
    content = (
        "---\nsource: esa\nmanaged_by: esa-sync\n"
        "document_type: article\nstatus: active\n---\nbody\n"
    )
    record = {
        "source": "esa",
        "managed_by": "esa-sync",
        "document_type": "article",
        "status": "active",
    }
    plan = plan_document("a.md", content, record)
    assert plan.action == "unchanged"


def test_plan_document_missing_record() -> None:
    plan = plan_document("a.md", "body\n", None)
    assert plan.action == "missing_record"


def test_dry_run_is_the_default_and_does_not_write(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    original = "body only\n"
    (docs_dir / "a.md").write_text(original, encoding="utf-8")
    DocumentRepository(conn).upsert({"path": "a.md", "source": "esa", "managed_by": "esa-sync"})

    service = BackfillMetadataService(conn, docs_dir=docs_dir)
    result = service.run()  # apply 省略 = dry-run

    assert result.mode == "dry-run"
    assert (docs_dir / "a.md").read_text(encoding="utf-8") == original
    assert result.totals.written == 0


def test_apply_writes_only_missing_keys_and_hash_verifies(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "a.md").write_text("body only\n", encoding="utf-8")
    DocumentRepository(conn).upsert(
        {
            "path": "a.md",
            "source": "esa",
            "managed_by": "esa-sync",
            "document_type": "article",
            "status": "active",
        }
    )

    service = BackfillMetadataService(conn, docs_dir=docs_dir)
    result = service.run(apply=True)

    assert result.totals.written == 1
    written = (docs_dir / "a.md").read_text(encoding="utf-8")
    assert "source: esa" in written
    assert "status: active" in written
    doc = DocumentRepository(conn).get("a.md")
    assert doc["source"] == "esa"


def test_apply_rolls_back_on_hash_mismatch(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    original = "body only\n"
    (docs_dir / "a.md").write_text(original, encoding="utf-8")
    DocumentRepository(conn).upsert({"path": "a.md", "source": "esa"})

    service = BackfillMetadataService(conn, docs_dir=docs_dir)
    # 書込後の再読込結果を意図的に破損させ、ハッシュ不一致→ロールバックを誘発する
    with patch(
        "abist_kb.application.audit.backfill_metadata.hash_body",
        side_effect=["expectedhash", "differenthash"],
    ):
        result = service.run(apply=True)

    assert result.totals.rolled_back == 1
    assert result.totals.errors == 1
    assert (docs_dir / "a.md").read_text(encoding="utf-8") == original


def test_run_backfill_inline_requires_confirmation_for_apply(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    service = BackfillMetadataService(conn, docs_dir=docs_dir)
    with pytest.raises(AppError):
        run_backfill_inline(service, conn, apply=True, confirmed=False)


def test_run_backfill_inline_allows_apply_when_confirmed(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "a.md").write_text("body\n", encoding="utf-8")
    DocumentRepository(conn).upsert({"path": "a.md", "source": "esa"})
    service = BackfillMetadataService(conn, docs_dir=docs_dir)

    result = run_backfill_inline(service, conn, apply=True, confirmed=True)
    assert result.mode == "apply"


def test_dry_run_does_not_require_confirmation(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    service = BackfillMetadataService(conn, docs_dir=docs_dir)
    result = run_backfill_inline(service, conn, apply=False)
    assert result.mode == "dry-run"


def test_records_audit_run(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    result = BackfillMetadataService(conn, docs_dir=docs_dir).run()
    run = conn.execute("SELECT * FROM audit_runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run["audit_type"] == "backfill-metadata"
    assert run["mode"] == "dry-run"
