"""`check-contradictions` の移植: 純粋関数と、候補限定・不変更の原則。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.audit.check_contradictions import (
    DEFAULT_SIMILARITY,
    IGNORED_UNITS,
    STATE_TERMS,
    CheckContradictionsService,
    build_candidate_pairs,
    detect_conflict,
    extract_numbers,
    find_state_conflict,
    has_negation,
)
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema


def test_default_similarity_matches_old_high_threshold() -> None:
    """旧実装の実測(0.92で174,280件に爆発)を踏まえた高いしきい値を維持する。"""
    assert DEFAULT_SIMILARITY == 0.99


def test_extract_numbers_ignores_date_like_units() -> None:
    numbers = extract_numbers("板厚2.0mmで、26日までに対応")
    units = {n.unit for n in numbers}
    assert "mm" in units
    assert "日" not in units
    assert "日" in IGNORED_UNITS


def test_extract_numbers_finds_unit_value() -> None:
    numbers = extract_numbers("板厚 2.5mm")
    assert numbers[0].value == 2.5
    assert numbers[0].unit == "mm"


def test_has_negation_detects_common_forms() -> None:
    assert has_negation("これは対応できない") is True
    assert has_negation("これは対応済です") is False


def test_find_state_conflict_detects_opposite_state_terms() -> None:
    conflicts = find_state_conflict("この件は対応済です", "この件は未対応です")
    assert conflicts == [{"a": "対応済", "b": "未対応"}]
    assert ("対応済", "未対応") in STATE_TERMS


def test_detect_conflict_reports_number_mismatch() -> None:
    reasons = detect_conflict("板厚は2.0mmです", "板厚は2.5mmです")
    assert any(r["kind"] == "number" for r in reasons)


def test_detect_conflict_reports_negation_mismatch() -> None:
    reasons = detect_conflict("この機能は対応できない", "この機能は対応できます")
    assert any(r["kind"] == "negation" for r in reasons)


def test_detect_conflict_skips_tables_with_many_numbers() -> None:
    a = " ".join(f"{i}mm" for i in range(10))
    b = " ".join(f"{i + 100}mm" for i in range(10))
    reasons = detect_conflict(a, b)
    assert not any(r["kind"] == "number" for r in reasons)


def test_build_candidate_pairs_limits_to_same_post_number_and_near() -> None:
    docs = [
        {"path": "a.md", "post_number": 1},
        {"path": "b.md", "post_number": 1},
        {"path": "c.md", "post_number": 2},
    ]
    pairs = build_candidate_pairs(docs)
    assert pairs == [("a.md", "b.md", ["same_article"])]

    near_pairs = [{"documents": [{"path": "c.md"}, {"path": "d.md"}]}]
    pairs_with_near = build_candidate_pairs(docs, near_pairs=near_pairs)
    keys = {(p[0], p[1]) for p in pairs_with_near}
    assert ("c.md", "d.md") in keys


@pytest.fixture
def conn(tmp_root: Path):
    connection = connect(tmp_root / "app.sqlite")
    ensure_app_schema(connection)
    return connection


def test_service_finds_conflict_between_same_article_versions(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "a.md").write_text("板厚は2.0mmで対応済です。\n", encoding="utf-8")
    (docs_dir / "b.md").write_text("板厚は2.5mmで対応済です。\n", encoding="utf-8")
    DocumentRepository(conn).upsert({"path": "a.md", "post_number": 1})
    DocumentRepository(conn).upsert({"path": "b.md", "post_number": 1})

    service = CheckContradictionsService(conn, docs_dir=docs_dir)
    result = service.run(similarity=0.5)
    assert result.candidate_pair_count == 1
    assert len(result.candidates) == 1
    assert any(c["kind"] == "number" for c in result.candidates[0].conflicts[0]["reasons"])


def test_service_does_not_mutate_documents(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "a.md").write_text("同じ文章です。\n", encoding="utf-8")
    (docs_dir / "b.md").write_text("同じ文章です。\n", encoding="utf-8")
    DocumentRepository(conn).upsert({"path": "a.md", "post_number": 1})
    DocumentRepository(conn).upsert({"path": "b.md", "post_number": 1})
    before = DocumentRepository(conn).list()

    CheckContradictionsService(conn, docs_dir=docs_dir).run(similarity=0.5)

    after = DocumentRepository(conn).list()
    assert before == after


def test_service_records_audit_run(conn, tmp_root: Path) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(parents=True)
    result = CheckContradictionsService(conn, docs_dir=docs_dir).run()
    run = conn.execute("SELECT * FROM audit_runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run["audit_type"] == "check-contradictions"
    assert run["status"] == "completed"
