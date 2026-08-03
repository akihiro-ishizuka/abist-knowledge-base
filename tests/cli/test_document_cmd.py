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
