"""Adaptive Card の組み立てと Webhook 送信(設計 §6.1.1, §8.2)。

`HTTP 202` は Power Automate が受理したことを示すだけで、Teams 画面への配送完了
とは同義でない。したがって結果は `accepted` であって `posted` ではない。
"""

from __future__ import annotations

import httpx
import pytest

from abist_kb.application.chat_watch.membership import AI_PREFIX
from abist_kb.infrastructure.notify.teams import (
    DeliveryOutcome,
    build_card,
    fingerprint,
    post_card,
)

WEBHOOK = "https://example.com/workflows/invoke?sig=secret"


def _texts(payload: dict) -> list[str]:
    body = payload["attachments"][0]["content"]["body"]
    return [block["text"] for block in body if block["type"] == "TextBlock"]


def test_card_starts_with_ai_prefix() -> None:
    payload = build_card("見出し", "本文です")

    assert _texts(payload)[0].startswith(AI_PREFIX)


def test_card_includes_body_and_sources() -> None:
    payload = build_card("見出し", "本文です", sources=["https://abist.esa.io/posts/5346"])

    texts = _texts(payload)
    assert "本文です" in texts
    assert any("https://abist.esa.io/posts/5346" in t for t in texts)


def test_card_shape_is_adaptive_card() -> None:
    payload = build_card("見出し", "本文")

    attachment = payload["attachments"][0]
    assert payload["type"] == "message"
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"]["type"] == "AdaptiveCard"


def test_fingerprint_is_stable_and_content_sensitive() -> None:
    a = build_card("見出し", "本文")
    b = build_card("見出し", "別の本文")

    assert fingerprint(a) == fingerprint(a)
    assert fingerprint(a) != fingerprint(b)


def test_202_is_accepted() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(202))

    with httpx.Client(transport=transport) as client:
        outcome = post_card(WEBHOOK, build_card("t", "b"), client=client)

    assert outcome is DeliveryOutcome.ACCEPTED


@pytest.mark.parametrize("status", [400, 403, 500, 503])
def test_error_response_is_failed(status: int) -> None:
    """応答を受け取れた明確な失敗は再送してよいので `failed`。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(status))

    with httpx.Client(transport=transport) as client:
        outcome = post_card(WEBHOOK, build_card("t", "b"), client=client)

    assert outcome is DeliveryOutcome.FAILED


def test_transport_error_is_unknown() -> None:
    """応答が無い場合は届いたか判らないので `unknown`。再送しない。"""

    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    transport = httpx.MockTransport(_raise)

    with httpx.Client(transport=transport) as client:
        outcome = post_card(WEBHOOK, build_card("t", "b"), client=client)

    assert outcome is DeliveryOutcome.UNKNOWN


def test_webhook_url_never_appears_in_outcome() -> None:
    """秘密情報を戻り値やログへ漏らさない(Global Constraints)。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(500))

    with httpx.Client(transport=transport) as client:
        outcome = post_card(WEBHOOK, build_card("t", "b"), client=client)

    assert "sig=secret" not in repr(outcome)
