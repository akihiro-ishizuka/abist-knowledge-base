"""連絡チャットへの投稿(設計 §6.1.1, §8.2)。

Webhook は Power Automate Workflows の HTTP トリガであり送信専用。読み取りは
できない。`HTTP 202` は「受理した」の意味で、Teams 画面への配送完了ではない。

idempotency 機構が無いため exactly-once は保証できない。応答を受け取れなかった
場合は `UNKNOWN` を返し、呼び出し側は自動再送しない(at-most-once)。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

import httpx

from abist_kb.application.chat_watch.membership import AI_PREFIX

_TIMEOUT_SECONDS = 30.0


class DeliveryOutcome(StrEnum):
    """送信結果。`MessageStatus` へそのまま対応させる。"""

    ACCEPTED = "accepted"
    FAILED = "failed"
    UNKNOWN = "unknown"


def build_card(title: str, body: str, *, sources: list[str] | None = None) -> dict[str, Any]:
    """Adaptive Card を組み立てる。

    先頭は必ず `AI_PREFIX`。名義が Workflows のままで「石塚 昭宏 used a Workflow
    template」と表示されるため、名乗らないと石塚さんの発言と誤読される。
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": f"{AI_PREFIX} {title}",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {"type": "TextBlock", "text": body, "wrap": True},
    ]
    if sources:
        blocks.append(
            {
                "type": "TextBlock",
                "text": "出典\n" + "\n".join(f"- {url}" for url in sources),
                "wrap": True,
                "isSubtle": True,
            }
        )

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptive-card.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": blocks,
                },
            }
        ],
    }


def fingerprint(payload: dict[str, Any]) -> str:
    """監査用の指紋。安全機構ではない(設計 §6.1.1)。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def post_card(
    webhook_url: str, payload: dict[str, Any], *, client: httpx.Client
) -> DeliveryOutcome:
    """カードを投稿する。

    - `202`(および 2xx): `ACCEPTED`
    - 応答を受け取れた非 2xx: `FAILED`(再送してよい)
    - 応答が無い(タイムアウト・接続断): `UNKNOWN`(**再送しない**)

    例外は送出しない。呼び出し側が state 遷移だけで判断できるようにする。
    URL は戻り値にもログにも含めない。
    """
    try:
        response = client.post(webhook_url, json=payload, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        return DeliveryOutcome.UNKNOWN

    if 200 <= response.status_code < 300:
        return DeliveryOutcome.ACCEPTED
    return DeliveryOutcome.FAILED
