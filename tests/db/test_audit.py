"""`audit_events`: 破壊的操作の監査証跡(設計書 §12)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.infrastructure.db.audit import record_event
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema


@pytest.fixture
def conn(tmp_root: Path):
    c = connect(tmp_root / "app.sqlite")
    ensure_app_schema(c)
    yield c
    c.close()


def test_record_event_persists_row(conn) -> None:
    record_event(
        conn,
        action="document.delete",
        target_type="document",
        target_id="knowledge/a.md",
        details={"reason": "duplicate"},
        actor="cli-user",
    )
    row = conn.execute("SELECT * FROM audit_events").fetchone()
    assert row["action"] == "document.delete"
    assert row["target_type"] == "document"
    assert row["target_id"] == "knowledge/a.md"
    assert row["actor"] == "cli-user"
    assert row["occurred_at"]


def test_record_event_serializes_details_as_json(conn) -> None:
    import json

    record_event(conn, action="batch.delete", target_type="batch", target_id="b1", details={"n": 3})
    row = conn.execute("SELECT details FROM audit_events").fetchone()
    assert json.loads(row["details"]) == {"n": 3}


def test_record_event_details_optional(conn) -> None:
    record_event(conn, action="batch.delete", target_type="batch", target_id="b1")
    row = conn.execute("SELECT details FROM audit_events").fetchone()
    assert row["details"] is None
