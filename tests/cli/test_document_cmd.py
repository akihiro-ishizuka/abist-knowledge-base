"""`document` CLI コマンド群の受入テスト。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from abist_kb.domain.errors import ErrorCode
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.presentation.cli.app import app

runner = CliRunner()


def _root_args(tmp_root, *rest: str) -> list[str]:
    return ["--root", str(tmp_root), *rest]


def _seed_document(tmp_root, record: dict) -> None:
    conn = connect(tmp_root / "data" / "app.sqlite")
    ensure_app_schema(conn)
    DocumentRepository(conn).upsert(record)
    conn.close()


def test_document_show_returns_metadata(tmp_root):
    _seed_document(tmp_root, {"path": "knowledge/a.md", "title": "A", "source": "esa"})
    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "document", "show", "knowledge/a.md")
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["title"] == "A"
    assert payload["source"] == "esa"


def test_document_show_missing_path_is_not_found(tmp_root):
    result = runner.invoke(app, _root_args(tmp_root, "document", "show", "does/not/exist.md"))
    assert result.exit_code != 0
    assert ErrorCode.NOT_FOUND.value in result.output


def test_document_show_normalizes_windows_path(tmp_root):
    _seed_document(tmp_root, {"path": "knowledge/sub/a.md", "title": "A"})
    result = runner.invoke(
        app,
        _root_args(tmp_root, "--output", "json", "document", "show", "knowledge\\sub\\a.md"),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["title"] == "A"


def _write_doc(tmp_root, relative: str, content: str) -> None:
    target = tmp_root / "docs" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_document_register_disk_defaults_to_dry_run(tmp_root):
    _write_doc(tmp_root, "notes/hello.md", "# こんにちは\n\n本文\n")

    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "document", "register-disk")
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["paths"] == ["notes/hello.md"]

    shown = runner.invoke(app, _root_args(tmp_root, "document", "show", "notes/hello.md"))
    assert shown.exit_code != 0
    assert ErrorCode.NOT_FOUND.value in shown.output


def test_document_register_disk_apply_writes_documents(tmp_root):
    _write_doc(tmp_root, "notes/hello.md", "# こんにちは\n\n本文\n")

    result = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "document", "register-disk", "--apply")
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["registered"] == 1

    shown = runner.invoke(
        app, _root_args(tmp_root, "--output", "json", "document", "show", "notes/hello.md")
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["source"] == "manual"
