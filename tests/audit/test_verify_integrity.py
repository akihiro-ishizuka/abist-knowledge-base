"""`verify-integrity` の移植: 6状態分類と「欠落は削除の証拠ではない」の原則。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.audit.verify_integrity import VerifyIntegrityService
from abist_kb.domain.frontmatter import hash_body
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema


@pytest.fixture
def conn(tmp_root: Path):
    connection = connect(tmp_root / "app.sqlite")
    ensure_app_schema(connection)
    return connection


def _write(docs_dir: Path, path: str, content: str) -> None:
    full = docs_dir / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def test_ok_when_hash_matches(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    content = "---\nstatus: active\n---\nbody\n"
    _write(docs_dir, "a.md", content)
    DocumentRepository(conn).upsert(
        {
            "path": "a.md",
            "managed_by": "esa-sync",
            "status": "active",
            "local_content_hash": hash_body(content),
        }
    )

    result = VerifyIntegrityService(conn, docs_dir=docs_dir).run()
    assert result.totals.ok == 1
    assert result.totals.modified_local == 0


def test_modified_local_for_managed_document(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    original = "---\nstatus: active\n---\noriginal\n"
    _write(docs_dir, "a.md", original)
    DocumentRepository(conn).upsert(
        {
            "path": "a.md",
            "managed_by": "esa-sync",
            "status": "active",
            "local_content_hash": hash_body(original),
        }
    )
    _write(docs_dir, "a.md", "---\nstatus: active\n---\nedited locally\n")

    result = VerifyIntegrityService(conn, docs_dir=docs_dir).run()
    assert result.totals.modified_local == 1
    finding = next(f for f in result.findings if f["type"] == "modified_local")
    assert finding["path"] == "a.md"


def test_manual_edited_for_human_document(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    original = "body\n"
    _write(docs_dir, "notes.md", original)
    DocumentRepository(conn).upsert(
        {"path": "notes.md", "managed_by": "human", "local_content_hash": hash_body(original)}
    )
    _write(docs_dir, "notes.md", "edited by a human\n")

    result = VerifyIntegrityService(conn, docs_dir=docs_dir).run()
    assert result.totals.manual_edited == 1
    assert result.totals.modified_local == 0


def test_metadata_changed_when_only_status_differs(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    original = "---\nstatus: active\n---\nsame body\n"
    _write(docs_dir, "a.md", original)
    DocumentRepository(conn).upsert(
        {
            "path": "a.md",
            "managed_by": "esa-sync",
            "status": "active",
            "local_content_hash": hash_body(original),
        }
    )
    changed_status = "---\nstatus: archived\n---\nsame body\n"
    _write(docs_dir, "a.md", changed_status)
    DocumentRepository(conn).upsert(
        {"path": "a.md", "local_content_hash": hash_body(changed_status)}
    )

    result = VerifyIntegrityService(conn, docs_dir=docs_dir).run()
    assert result.totals.metadata_changed == 1
    doc = DocumentRepository(conn).get("a.md")
    assert doc["status"] == "archived"


def test_missing_local_is_a_candidate_not_a_deletion(conn, tmp_root: Path) -> None:
    """設計原則5: ローカル欠落は「取得元の欠落」ではない。sync_status を deleted にしない。"""
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    DocumentRepository(conn).upsert(
        {"path": "gone.md", "managed_by": "esa-sync", "local_content_hash": "deadbeef"}
    )

    result = VerifyIntegrityService(conn, docs_dir=docs_dir).run()
    assert result.totals.missing_local == 1
    doc = DocumentRepository(conn).get("gone.md")
    # sync_status は 'error'(要確認)であって、'deleted' や欠落確定ではない
    assert doc["sync_status"] != "deleted"
    assert doc["sync_status"] == "error"


def test_untracked_files_are_reported(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "new.md", "brand new\n")

    result = VerifyIntegrityService(conn, docs_dir=docs_dir).run()
    assert result.totals.untracked == 1
    assert result.findings[0]["type"] == "untracked"


def test_update_db_false_does_not_mutate(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    DocumentRepository(conn).upsert(
        {
            "path": "gone.md",
            "managed_by": "esa-sync",
            "local_content_hash": "deadbeef",
            "sync_status": "synced",
        }
    )

    VerifyIntegrityService(conn, docs_dir=docs_dir).run(update_db=False)
    doc = DocumentRepository(conn).get("gone.md")
    assert doc["sync_status"] == "synced"


def test_records_audit_run_and_findings(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "new.md", "brand new\n")

    result = VerifyIntegrityService(conn, docs_dir=docs_dir).run()
    run = conn.execute("SELECT * FROM audit_runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run["audit_type"] == "verify-integrity"
    assert run["status"] == "completed"
    findings = conn.execute(
        "SELECT * FROM audit_findings WHERE run_id = ?", (result.run_id,)
    ).fetchall()
    assert len(findings) == 1
    assert findings[0]["finding_type"] == "untracked"


def test_check_lease_is_invoked_per_document(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    _write(docs_dir, "a.md", "content\n")
    DocumentRepository(conn).upsert({"path": "a.md", "local_content_hash": "deadbeef"})

    calls = []
    VerifyIntegrityService(conn, docs_dir=docs_dir).run(check_lease=lambda: calls.append(1))
    assert len(calls) >= 1
