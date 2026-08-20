"""送信者分類と自己応答ループの防止(設計 §4)。

自己投稿の除外は active 判定の副作用に頼らず、送信者判定と本文先頭判定の2つを
独立したガードとして持つ。メンバー構成が変わっても崩れないようにするため。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from abist_kb.application.chat_watch.membership import (
    AI_PREFIX,
    WORKFLOWS_SENDER,
    classify,
    is_self_post,
)
from abist_kb.domain.chat_watch import InboundMessage, MemberRole


def _message(sender_email: str, body: str = "本文") -> InboundMessage:
    return InboundMessage(
        message_id="1",
        chat_id="19:x@thread.v2",
        sender_email=sender_email,
        sender_name="送信者",
        body=body,
        created_at=datetime(2026, 8, 20, 8, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "email",
    [
        "t_isaka@abist.co.jp",
        "d_suzuki@abist.co.jp",
        "a_ishizuka@abist.co.jp",
        "ma_ishii@abist.co.jp",
        "y_osawa@abist.co.jp",
        "t_niizeki@abist.co.jp",
    ],
)
def test_active_members(email: str) -> None:
    assert classify(_message(email)) is MemberRole.ACTIVE


@pytest.mark.parametrize(
    "email",
    [
        "yamaura@abist.co.jp",
        "mi_hashikawa@abist.co.jp",
        "morita.abist@gmail.com",
        "j_saga@abist.co.jp",
    ],
)
def test_context_only_members(email: str) -> None:
    assert classify(_message(email)) is MemberRole.CONTEXT_ONLY


def test_unknown_sender_is_context_only() -> None:
    """未知の送信者は応答対象にしない。安全側へ倒す。"""
    assert classify(_message("newcomer@abist.co.jp")) is MemberRole.CONTEXT_ONLY


def test_workflows_sender_is_system() -> None:
    assert classify(_message(WORKFLOWS_SENDER)) is MemberRole.SYSTEM


def test_case_insensitive_sender_match() -> None:
    assert classify(_message("T_Isaka@ABIST.co.jp")) is MemberRole.ACTIVE


def test_is_self_post_by_sender() -> None:
    assert is_self_post(_message(WORKFLOWS_SENDER, "ふつうの本文")) is True


def test_is_self_post_by_body_prefix() -> None:
    """送信者判定をすり抜けても本文先頭で止める(二重の防御)。"""
    assert is_self_post(_message("t_isaka@abist.co.jp", f"{AI_PREFIX} 回答です")) is True


def test_human_message_is_not_self_post() -> None:
    assert is_self_post(_message("t_isaka@abist.co.jp", "質問です")) is False


def test_ai_prefix_value() -> None:
    assert AI_PREFIX == "【AI設計エージェント】"
