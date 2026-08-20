"""メッセージ取得の継ぎ目(設計 §3.1.1)。

Track A では `chat_message_search` が Claude 側の MCP コネクタにあり、`abist-kb`
プロセスからは呼べない。そこで取得を protocol で切り離し、Claude が書いた JSON を
読む実装を置く。Track B で Graph 直叩きになったら実装だけ差し替える。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from abist_kb.domain.chat_watch import InboundMessage
from abist_kb.domain.errors import AppError, ErrorCode


class MessageSource(Protocol):
    """probe ごとの検索結果を返す。"""

    def fetch(
        self, *, since: datetime, probes: tuple[str, ...]
    ) -> dict[str, list[InboundMessage]]: ...


class JsonFileMessageSource:
    """Claude が書いた JSON を読む(Track A)。

    期待する形:
        {"probes": {"い": [ {message_id, chat_id, sender_email,
                             sender_name, body, created_at}, ... ], ...}}

    `since` は Claude 側が検索時に使う値であり、この実装では絞り込みに使わない
    (すでに絞られたものが渡る)。protocol の互換のために受け取る。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def fetch(self, *, since: datetime, probes: tuple[str, ...]) -> dict[str, list[InboundMessage]]:
        if not self._path.is_file():
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"inbox ファイルがありません: {self._path}",
                hint="MCP 検索の結果を JSON で書き出してから実行してください。",
            )
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            probe_map = raw.get("probes", {})
            return {
                probe: [InboundMessage.model_validate(item) for item in probe_map.get(probe, [])]
                for probe in probes
            }
        except (json.JSONDecodeError, ValidationError, AttributeError) as exc:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                f"inbox ファイルの内容が不正です: {self._path}",
                details={"cause_type": type(exc).__name__},
            ) from exc
