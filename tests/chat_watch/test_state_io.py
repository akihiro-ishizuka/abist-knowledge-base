"""state の永続化(設計 §6.4)。

state は監視システムの中核で、書き込み途中に落ちると JSON が壊れて復旧不能に
なる。atomic write で置換すること、未知の schema_version で起動を中止すること
を検査する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from abist_kb.application.chat_watch.state import (
    SCHEMA_VERSION,
    MessageRecord,
    WatchState,
    load_state,
    save_state,
)
from abist_kb.domain.chat_watch import MessageStatus
from abist_kb.domain.errors import AppError, ErrorCode


def test_load_missing_file_returns_empty_state(tmp_path: Path) -> None:
    state = load_state(tmp_path / "absent.json")

    assert state.schema_version == SCHEMA_VERSION
    assert state.initialised is False
    assert state.search_watermark is None
    assert state.messages == {}
    assert state.questions == {}


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = WatchState(
        search_watermark=datetime(2026, 8, 20, 8, 0, tzinfo=UTC),
        messages={
            "1": MessageRecord(
                message_id="1",
                status=MessageStatus.ACCEPTED,
                sender_email="t_isaka@abist.co.jp",
                created_at=datetime(2026, 8, 20, 7, 0, tzinfo=UTC),
            )
        },
    )

    save_state(path, state)
    loaded = load_state(path)

    assert loaded.search_watermark == state.search_watermark
    assert loaded.messages["1"].status is MessageStatus.ACCEPTED


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    save_state(path, WatchState())

    assert path.is_file()
    assert list(tmp_path.glob("*.tmp")) == []


def test_existing_state_survives_a_failed_write(tmp_path: Path) -> None:
    """書き込み中にプロセスが落ちても既存 state を壊さない。"""
    path = tmp_path / "state.json"
    save_state(path, WatchState(search_watermark=datetime(2026, 8, 20, 8, 0, tzinfo=UTC)))
    original = path.read_text(encoding="utf-8")

    class Boom(WatchState):
        def model_dump_json(self, **kwargs: object) -> str:
            raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        save_state(path, Boom())

    assert path.read_text(encoding="utf-8") == original


def test_unknown_schema_version_aborts(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION + 1, "messages": {}}),
        encoding="utf-8",
    )

    with pytest.raises(AppError) as exc_info:
        load_state(path)

    assert exc_info.value.code is ErrorCode.CONFIG_ERROR


def test_corrupt_json_aborts(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(AppError) as exc_info:
        load_state(path)

    assert exc_info.value.code is ErrorCode.CONFIG_ERROR
