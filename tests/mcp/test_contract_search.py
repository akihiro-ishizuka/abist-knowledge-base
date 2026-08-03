"""kb-search MCP 契約テスト(M5 task-2-brief)。

`tests/fixtures/mcp/kb-search/**/*.json` を「本番索引に対する実測ゴールデン」
として読み、`KbSearchTools` の応答が同じキー集合・型・エラー種別を持つことを
検証する。生産環境の `docs/`/`data/*.sqlite` はこのリポジトリのテストからは
参照できないため(`tests/search/test_services.py` と同じ制約)、合成した小さな
work/reference コーパスに対して実行し、`tests/mcp/replay.py` の構造比較器で
fixture と突き合わせる。`path`/`chunk_id` など具体的な値を要求するケース
(`get_document` の正常系・`get_chunk` の正常系)だけは、本物の値の代わりに
合成コーパス内の実在する値へ差し替える(brief: 「実装だけ堅くする」の精神を
テストにも適用し、値そのものではなく契約の形を検証する)。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from replay import check_result, load_fixture

from abist_kb.application.index_service import IndexService
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.presentation.mcp.kb_search import KbSearchTools

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "kb-search"

_SEEDED_CONTENT = """---
title: "テスト文書"
date: "2026-01-01"
updated_at: "2026-01-02"
author: "tester"
---

# 見出し1

CATIAの起動時間を短縮する手順について、shrink_clamp_bellow_overlap_mm を
用いて PySide6 のダイアログから設定する。

## 見出し2

行9
行10
行11
"""


@dataclasses.dataclass(frozen=True, slots=True)
class Env:
    tools: KbSearchTools
    docs_dir: Path
    work_index_path: Path
    seeded_path: str
    seeded_chunk_id: int


@pytest.fixture
def env(tmp_root: Path) -> Iterator[Env]:
    docs_dir = tmp_root / "docs"
    app_db_path = tmp_root / "app.sqlite"
    work_index_path = tmp_root / "work-index.sqlite"
    reference_index_path = tmp_root / "reference-index.sqlite"

    seeded_path = "テスト/文書.md"
    full = docs_dir / seeded_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(_SEEDED_CONTENT, encoding="utf-8")

    conn = connect(app_db_path)
    try:
        ensure_app_schema(conn)
        DocumentRepository(conn).upsert(
            {
                "path": seeded_path,
                "title": "テスト文書",
                "source": "esa",
                "document_type": "meeting",
                "status": "active",
                "post_number": 1,
            }
        )
    finally:
        conn.close()

    index_service = IndexService(
        docs_dir=docs_dir,
        app_db_path=app_db_path,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
    )
    index_service.build("work")
    index_service.build("reference")  # docs/knowledge/B32doc が無いので0件で成功する

    tools = KbSearchTools(
        docs_dir=docs_dir,
        work_index_path=work_index_path,
        reference_index_path=reference_index_path,
    )

    conn = connect(work_index_path, read_only=True)
    try:
        chunk_id = conn.execute("SELECT id FROM chunks LIMIT 1").fetchone()[0]
    finally:
        conn.close()

    yield Env(
        tools=tools,
        docs_dir=docs_dir,
        work_index_path=work_index_path,
        seeded_path=seeded_path,
        seeded_chunk_id=chunk_id,
    )
    tools.close()


def _fixture_files(subdir: str) -> list[Path]:
    return sorted((FIXTURES_DIR / subdir).glob("*.json"))


# ---------------------------------------------------------------------------
# search_kb (5ケース)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_path", _fixture_files("search_kb"), ids=lambda p: p.stem)
def test_search_kb_matches_fixture_shape(env: Env, fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    result = env.tools.search_kb(fixture.arguments)
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


# ---------------------------------------------------------------------------
# get_document
# ---------------------------------------------------------------------------


def test_get_document_normal_matches_fixture_shape(env: Env) -> None:
    fixture = load_fixture(FIXTURES_DIR / "get_document" / "normal.json")
    result = env.tools.get_document({"path": env.seeded_path})
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_get_document_with_line_range_matches_fixture_shape(env: Env) -> None:
    fixture = load_fixture(FIXTURES_DIR / "get_document" / "with_line_range.json")
    result = env.tools.get_document({"path": env.seeded_path, "start_line": 1, "end_line": 5})
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_get_document_with_line_range_uses_gutter_and_range_hash(env: Env) -> None:
    result = env.tools.get_document({"path": env.seeded_path, "start_line": 1, "end_line": 3})
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["range"] == {"from": 1, "to": 3}
    assert "range_hash" in payload
    lines = payload["content"].split("\n")
    assert lines[0].startswith("    1 | ")
    assert lines[2].startswith("    3 | ")


def test_get_document_nonexistent_path_matches_fixture_shape(env: Env) -> None:
    fixture = load_fixture(FIXTURES_DIR / "get_document" / "nonexistent_path.json")
    result = env.tools.get_document(fixture.arguments)
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_get_document_path_traversal_attempt_matches_fixture_shape(env: Env) -> None:
    fixture = load_fixture(FIXTURES_DIR / "get_document" / "path_traversal_attempt.json")
    result = env.tools.get_document(fixture.arguments)
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_get_document_rejects_sibling_directory_prefix_collision(env: Env) -> None:
    """`docs-backup` のような兄弟ディレクトリは `docs` の文字列 prefix には一致するが、

    `Path.resolve()` + `relative_to()` によるチェックでは docs/ の外として扱われる
    (旧実装の `startswith(DOCS_DIR)` バグの再現防止)。
    """
    sibling = env.docs_dir.parent / "docs-backup"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "leak.md").write_text("secret", encoding="utf-8")

    result = env.tools.get_document({"path": "../docs-backup/leak.md"})
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert result.isError is True


# ---------------------------------------------------------------------------
# get_chunk
# ---------------------------------------------------------------------------


def test_get_chunk_normal_matches_fixture_shape(env: Env) -> None:
    fixture = load_fixture(FIXTURES_DIR / "get_chunk" / "normal.json")
    result = env.tools.get_chunk({"chunk_id": env.seeded_chunk_id})
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_get_chunk_nonexistent_chunk_id_matches_fixture_shape(env: Env) -> None:
    fixture = load_fixture(FIXTURES_DIR / "get_chunk" / "nonexistent_chunk_id.json")
    result = env.tools.get_chunk(fixture.arguments)
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


# ---------------------------------------------------------------------------
# index_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_path", _fixture_files("index_status"), ids=lambda p: p.stem)
def test_index_status_matches_fixture_shape(env: Env, fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    result = env.tools.index_status({})
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_index_status_call_1_and_call_2_are_idempotent(env: Env) -> None:
    """fixture の call_1/call_2 は「引数を取らないため冪等性確認を兼ねた2件目」。"""
    first = env.tools.index_status({})
    second = env.tools.index_status({})
    assert first.content[0].text == second.content[0].text
