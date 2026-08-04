"""`find-duplicates` の移植: same_article / identical / near、変更しないこと。"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from abist_kb.application.audit.find_duplicates import (
    DEFAULT_NEAR_THRESHOLD,
    FindDuplicatesService,
    find_identical,
    find_same_article,
    is_likely_series,
)
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema


@pytest.fixture
def conn(tmp_root: Path):
    connection = connect(tmp_root / "app.sqlite")
    ensure_app_schema(connection)
    return connection


def test_find_same_article_groups_by_post_number() -> None:
    docs = [
        {
            "path": "a/1.md",
            "post_number": 5,
            "title": "T",
            "status": "active",
            "local_content_hash": "x",
        },
        {
            "path": "b/1.md",
            "post_number": 5,
            "title": "T",
            "status": "active",
            "local_content_hash": "y",
        },
        {
            "path": "c/2.md",
            "post_number": 6,
            "title": "U",
            "status": "active",
            "local_content_hash": "z",
        },
    ]
    groups = find_same_article(docs)
    assert len(groups) == 1
    assert groups[0]["type"] == "same_article"
    assert {d["path"] for d in groups[0]["documents"]} == {"a/1.md", "b/1.md"}
    assert groups[0]["same_content"] is False


def test_find_identical_excludes_same_post_number() -> None:
    docs = [
        {"path": "a.md", "post_number": 1, "local_content_hash": "same"},
        {"path": "b.md", "post_number": 1, "local_content_hash": "same"},
        {"path": "c.md", "post_number": 2, "local_content_hash": "same"},
    ]
    groups = find_identical(docs)
    assert len(groups) == 1
    assert {d["path"] for d in groups[0]["documents"]} == {"a.md", "b.md", "c.md"}


def test_find_identical_requires_multiple_post_numbers() -> None:
    docs = [
        {"path": "a.md", "post_number": 1, "local_content_hash": "same"},
        {"path": "b.md", "post_number": 1, "local_content_hash": "same"},
    ]
    assert find_identical(docs) == []


def test_is_likely_series_matches_same_dir_normalized_title() -> None:
    a = {"path": "reports/2026-01.md", "title": "週次報告 2026年1月"}
    b = {"path": "reports/2026-02.md", "title": "週次報告 2026年2月"}
    assert is_likely_series(a, b) is True


def test_is_likely_series_false_for_different_dir() -> None:
    a = {"path": "reports/2026-01.md", "title": "週次報告 2026年1月"}
    b = {"path": "other/2026-02.md", "title": "週次報告 2026年2月"}
    assert is_likely_series(a, b) is False


def _vec_blob(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def _build_index_db(connection, chunks: list[tuple[int, str, list[float]]]) -> None:
    connection.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, path TEXT, chunk_index INTEGER)"
    )
    connection.execute("CREATE TABLE embeddings (chunk_id INTEGER, vector BLOB)")
    for chunk_id, path, vector in chunks:
        connection.execute(
            "INSERT INTO chunks (id, path, chunk_index) VALUES (?, ?, 0)", (chunk_id, path)
        )
        connection.execute(
            "INSERT INTO embeddings (chunk_id, vector) VALUES (?, ?)", (chunk_id, _vec_blob(vector))
        )
    connection.commit()


def test_service_finds_near_duplicates_via_index_embeddings(conn, tmp_path: Path) -> None:
    DocumentRepository(conn).upsert({"path": "a.md", "title": "A", "local_content_hash": "ha"})
    DocumentRepository(conn).upsert({"path": "b.md", "title": "B", "local_content_hash": "hb"})

    index_conn = connect(tmp_path / "index.sqlite")
    _build_index_db(index_conn, [(1, "a.md", [1.0, 0.0]), (2, "b.md", [1.0, 0.0001])])

    result = FindDuplicatesService(conn, index_conn=index_conn).run(
        threshold=DEFAULT_NEAR_THRESHOLD
    )
    assert len(result.near) == 1
    assert {d["path"] for d in result.near[0]["documents"]} == {"a.md", "b.md"}


def test_service_does_not_mutate_documents(conn, tmp_path: Path) -> None:
    DocumentRepository(conn).upsert({"path": "a/1.md", "post_number": 1, "status": "active"})
    DocumentRepository(conn).upsert({"path": "b/1.md", "post_number": 1, "status": "active"})
    before = DocumentRepository(conn).list()

    FindDuplicatesService(conn).run()

    after = DocumentRepository(conn).list()
    assert before == after


def test_service_records_audit_run(conn) -> None:
    DocumentRepository(conn).upsert({"path": "a/1.md", "post_number": 1})
    DocumentRepository(conn).upsert({"path": "b/1.md", "post_number": 1})

    result = FindDuplicatesService(conn).run()
    run = conn.execute("SELECT * FROM audit_runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run["audit_type"] == "find-duplicates"
    assert run["status"] == "completed"
