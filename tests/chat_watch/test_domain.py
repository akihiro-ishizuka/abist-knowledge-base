"""`domain.chat_watch` の値オブジェクト。

状態名そのものが state ファイルへ永続化されるため、値を変えると既存 state を
壊す。文字列値を固定する検査をここに置く(設計 §6.1)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from abist_kb.domain.chat_watch import (
    InboundMessage,
    MemberRole,
    MessageStatus,
    QuestionStatus,
)


def test_message_status_values_are_stable() -> None:
    assert [s.value for s in MessageStatus] == [
        "discovered",
        "processing",
        "sending",
        "accepted",
        "failed",
        "unknown",
        "skipped",
        "closed_cold_start",
    ]


def test_question_status_values_are_stable() -> None:
    assert [s.value for s in QuestionStatus] == [
        "open",
        "acknowledged",
        "resolved",
        "reminded",
        "stale",
    ]


def test_member_role_values_are_stable() -> None:
    assert [r.value for r in MemberRole] == ["active", "context_only", "system"]


def test_inbound_message_requires_aware_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        InboundMessage(
            message_id="1",
            chat_id="19:x@thread.v2",
            sender_email="t_isaka@abist.co.jp",
            sender_name="井坂 孝",
            body="質問です",
            created_at=datetime(2026, 8, 20, 8, 0),  # naive
        )


def test_inbound_message_normalises_to_utc() -> None:
    message = InboundMessage(
        message_id="1",
        chat_id="19:x@thread.v2",
        sender_email="T_Isaka@ABIST.co.jp",
        sender_name="井坂 孝",
        body="質問です",
        created_at=datetime(2026, 8, 20, 17, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
    )

    assert message.created_at == datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    assert message.sender_email == "t_isaka@abist.co.jp"
