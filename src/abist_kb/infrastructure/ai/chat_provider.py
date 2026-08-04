"""チャット応答生成のプロバイダ境界(設計書 §7.1, M7 task-1)。

`ChatService` は本モジュールの `ChatProvider` プロトコルにのみ依存し、実装
(OpenAI 等)を意識しない。ベンダーを差し替え可能にするための境界であり、
`infrastructure.ai.embedding_provider` と同じ設計(httpx 直叩き・429/5xxのみ
再試行)を踏襲する。

引用の検証はここでは行わない(モデルが返した `path`/`start_line`/`end_line` を
そのまま `RawCitation` として返すだけ)。`range_hash` による検証は
`application.chat_service.ChatService` の責務(このモジュールに検証ロジックを
混ぜると、プロバイダ実装ごとに検証の抜け漏れが起きうるため、境界の外側=
アプリケーション層に一本化する)。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: モデルへの指示に含める、引用の出力形式についての取り決め。
#: 応答の最後に ```citations フェンスで JSON 配列(`path`/`start_line`/`end_line`)を
#: 出力させ、表示テキストからは取り除く(`_split_citations_block` 参照)。
CITATION_INSTRUCTION = (
    "回答の根拠となった箇所は、応答の最後に ```citations のフェンスで囲んだ JSON配列として "
    '(例: [{"path": "docs/a.md", "start_line": 1, "end_line": 3}]) 出力してください。'
    "本文には含めないでください。"
)

_CITATION_BLOCK_RE = re.compile(r"```citations\s*(\[.*?\])\s*```", re.DOTALL)


@dataclass(frozen=True, slots=True)
class RawCitation:
    """プロバイダが応答に含めた、未検証の引用。"""

    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """ストリーミング応答の1片。最後のチャンクだけ `done=True` かつ `citations` を持つ。"""

    delta: str = ""
    done: bool = False
    citations: tuple[RawCitation, ...] = field(default_factory=tuple)


class ChatProvider(Protocol):
    """`ChatService` が依存するプロバイダの境界。"""

    def stream(
        self, *, system: str, messages: Sequence[dict[str, str]], context: str
    ) -> Iterator[ChatChunk]: ...


def split_citations_block(text: str) -> tuple[str, list[RawCitation]]:
    """応答本文から ```citations フェンスを取り除き、引用一覧と一緒に返す。

    JSON として壊れている・型が合わない要素は黙って無視する(構文エラーで
    チャット自体が失敗しては本末転倒。引用の正当性そのものは
    `ChatService` が `range_hash` で別途検証する)。
    """
    match = _CITATION_BLOCK_RE.search(text)
    if match is None:
        return text.strip(), []

    display_text = (text[: match.start()] + text[match.end() :]).strip()
    citations: list[RawCitation] = []
    try:
        raw_items = json.loads(match.group(1))
    except json.JSONDecodeError:
        return display_text, []

    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            start = item.get("start_line")
            end = item.get("end_line")
            if isinstance(path, str) and isinstance(start, int) and isinstance(end, int):
                citations.append(RawCitation(path=path, start_line=start, end_line=end))

    return display_text, citations


#: 429/5xx のみ再試行(`OpenAIEmbeddingProvider` と同じ方針)。
DEFAULT_MAX_ATTEMPTS = 4


class OpenAIChatProvider:
    """OpenAI Chat Completions(ストリーミング)を叩くプロバイダ。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        import httpx

        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=120.0)
        self._sleep = sleep
        self._max_attempts = max_attempts

    def stream(
        self, *, system: str, messages: Sequence[dict[str, str]], context: str
    ) -> Iterator[ChatChunk]:
        payload_messages = [
            {"role": "system", "content": system + "\n\n" + CITATION_INSTRUCTION},
            {"role": "system", "content": f"参考文献:\n{context}"},
            *messages,
        ]

        full_text = ""
        for attempt in range(self._max_attempts):
            try:
                with self._client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._model, "messages": payload_messages, "stream": True},
                ) as response:
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt + 1 >= self._max_attempts:
                            response.raise_for_status()
                        self._sleep(2**attempt)
                        continue
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content") or ""
                        if delta:
                            full_text += delta
                            yield ChatChunk(delta=delta)
                break
            except Exception:
                if attempt + 1 >= self._max_attempts:
                    raise
                self._sleep(2**attempt)
                continue

        display_text, citations = split_citations_block(full_text)
        # 表示済みの delta には citations ブロックの断片も含まれてしまっているため、
        # 最終チャンクとして「フェンスを除いた全文との差分は出さず」引用だけを渡す。
        # 呼び出し側(ChatService)は蓄積した表示テキストを `display_text` で置き換える。
        yield ChatChunk(done=True, citations=tuple(citations))


__all__ = [
    "CITATION_INSTRUCTION",
    "ChatChunk",
    "ChatProvider",
    "OpenAIChatProvider",
    "RawCitation",
    "split_citations_block",
]
