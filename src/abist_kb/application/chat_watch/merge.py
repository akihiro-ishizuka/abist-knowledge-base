"""probe 結果のマージと重複排除(設計 §5.1)。

`chat_message_search` はチャットIDで絞れずクエリが必須のため、高頻度のかなを
複数 probe として投げ、結果を統合する。probe は部分一致なので網羅性は保証
されない(設計 §9-1)。どの程度取れているかを観測できるよう、probe ごとの件数を
返り値に残す。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from abist_kb.domain.chat_watch import InboundMessage


class MergeResult(BaseModel):
    """マージ結果と観測用の件数。"""

    model_config = ConfigDict(frozen=True)

    messages: list[InboundMessage]
    per_probe: dict[str, int]
    merged: int
    duplicates: int


def merge_probe_results(results: dict[str, list[InboundMessage]], *, chat_id: str) -> MergeResult:
    """probe ごとの検索結果を統合する。

    対象チャットのものだけ残し、`message_id` で重複を除き、`created_at` 昇順で
    返す。`per_probe` はチャット絞り込み後の件数(検索品質の観測が目的)。
    """
    seen: dict[str, InboundMessage] = {}
    per_probe: dict[str, int] = {}
    duplicates = 0

    for probe, messages in results.items():
        in_chat = [m for m in messages if m.chat_id == chat_id]
        per_probe[probe] = len(in_chat)
        for message in in_chat:
            if message.message_id in seen:
                duplicates += 1
                continue
            seen[message.message_id] = message

    ordered = sorted(seen.values(), key=lambda m: (m.created_at, m.message_id))
    return MergeResult(
        messages=ordered,
        per_probe=per_probe,
        merged=len(ordered),
        duplicates=duplicates,
    )
