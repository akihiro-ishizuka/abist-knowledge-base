"""`MessageSource` の継ぎ目(設計 §3.1.1)。

Track A では Python から Teams を読めない。Claude が MCP 検索の結果を JSON へ
書き、それをこの Source が読む。Track B では Graph 実装へ差し替える。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from abist_kb.application.chat_watch.source import JsonFileMessageSource
from abist_kb.domain.errors import AppError, ErrorCode

INBOX = {
    "probes": {
        "い": [
            {
                "message_id": "1787213787781",
                "chat_id": "19:x@thread.v2",
                "sender_email": "t_isaka@abist.co.jp",
                "sender_name": "井坂 孝",
                "body": "#527 の状態を教えてください",
                "created_at": "2026-08-20T08:16:30Z",
            }
        ],
        "の": [],
    }
}


def test_reads_probe_results(tmp_path: Path) -> None:
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps(INBOX, ensure_ascii=False), encoding="utf-8")

    result = JsonFileMessageSource(path).fetch(
        since=datetime(2026, 8, 20, 7, 0, tzinfo=UTC), probes=("い", "の")
    )

    assert list(result) == ["い", "の"]
    assert result["い"][0].message_id == "1787213787781"
    assert result["い"][0].created_at == datetime(2026, 8, 20, 8, 16, 30, tzinfo=UTC)
    assert result["の"] == []


def test_missing_probe_key_yields_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps(INBOX, ensure_ascii=False), encoding="utf-8")

    result = JsonFileMessageSource(path).fetch(
        since=datetime(2026, 8, 20, 7, 0, tzinfo=UTC), probes=("い", "の", "す")
    )

    assert result["す"] == []


def test_missing_file_is_an_app_error(tmp_path: Path) -> None:
    with pytest.raises(AppError) as exc_info:
        JsonFileMessageSource(tmp_path / "absent.json").fetch(
            since=datetime(2026, 8, 20, 7, 0, tzinfo=UTC), probes=("い",)
        )

    assert exc_info.value.code is ErrorCode.NOT_FOUND
