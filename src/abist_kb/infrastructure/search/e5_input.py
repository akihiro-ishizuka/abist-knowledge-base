"""チャンクの埋め込み入力構築(見出し文脈を含めて意味を補強する)。

旧実装 `tools/lib/embeddings.js` の移植。モデルごとの入力文字数上限・
接頭辞(passage/query)を保持し、旧実装とビット一致する `embedding_input` /
`query_input` / `input_hash` を提供する。

**モデル名は旧実装の文字列(`Xenova/multilingual-e5-small` 等)をそのまま
辞書キーとして使う。** `input_hash` はモデル名の文字列そのものをハッシュに
含めるため、Python 側で別名(例: `intfloat/...`)に変えると
`tests/fixtures/embedding/gate-samples.json` との照合(M8 の埋め込み再利用
ゲートの前提条件)が壊れる。

順序が命: `[title, heading_path, text]` のうち偽値でないものを改行で結合して
から `max_input_chars` で切り詰め、その後に接頭辞を付ける。接頭辞は文字数
制限に数えない。逆順にすると512文字境界をまたぐチャンクで異なる文字列・
異なる `input_hash` になる(`tests/kernel/test_e5_input.py` の
`test_boundary_straddle_512_breaks_if_prefix_applied_before_truncation` で
明示的に固定している)。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingModelSpec:
    """埋め込みモデル1件の設定。"""

    provider: str
    dimensions: int | None
    passage_prefix: str
    query_prefix: str
    max_input_chars: int


@dataclass(frozen=True, slots=True)
class EmbeddingInputChunk:
    """`embedding_input` へ渡すチャンクの最小表現。"""

    text: str
    title: str | None = None
    heading_path: str | None = None


# e5 系はモデルの入力窓が512トークン。日本語はほぼ1文字1トークンなので
# それ以上渡してもモデル側で切り捨てられ、処理時間だけが伸びる。
EMBEDDING_MODELS: dict[str, EmbeddingModelSpec] = {
    "Xenova/multilingual-e5-small": EmbeddingModelSpec(
        provider="local",
        dimensions=384,
        passage_prefix="passage: ",
        query_prefix="query: ",
        max_input_chars=512,
    ),
    "Xenova/multilingual-e5-base": EmbeddingModelSpec(
        provider="local",
        dimensions=768,
        passage_prefix="passage: ",
        query_prefix="query: ",
        max_input_chars=512,
    ),
    "text-embedding-3-small": EmbeddingModelSpec(
        provider="openai",
        dimensions=1536,
        passage_prefix="",
        query_prefix="",
        max_input_chars=16000,
    ),
    "text-embedding-3-large": EmbeddingModelSpec(
        provider="openai",
        dimensions=3072,
        passage_prefix="",
        query_prefix="",
        max_input_chars=16000,
    ),
}

_UNKNOWN_MAX_INPUT_CHARS = 2000


def model_config(model: str) -> EmbeddingModelSpec:
    """モデル設定を返す。未知のモデル名はプレフィックスから推定してフォールバックする。"""
    known = EMBEDDING_MODELS.get(model)
    if known is not None:
        return known
    provider = "openai" if model.startswith("text-embedding-") else "local"
    return EmbeddingModelSpec(
        provider=provider,
        dimensions=None,
        passage_prefix="",
        query_prefix="",
        max_input_chars=_UNKNOWN_MAX_INPUT_CHARS,
    )


def embedding_input(chunk: EmbeddingInputChunk, model: str) -> str:
    """埋め込みに与える入力テキストを作る。

    `[title, heading_path, text]` のうち偽値でないものを改行で結合してから
    `max_input_chars` で切り詰め、その後に接頭辞を付ける(接頭辞は切り詰めに
    数えない)。
    """
    config = model_config(model)
    parts = [part for part in (chunk.title, chunk.heading_path, chunk.text) if part]
    body = "\n".join(parts)[: config.max_input_chars]
    return config.passage_prefix + body


def query_input(query: str, model: str) -> str:
    """検索クエリ側の入力を作る(e5 系は文書と接頭辞が異なる)。"""
    config = model_config(model)
    return config.query_prefix + str(query)[: config.max_input_chars]


def input_hash(model: str, text: str) -> str:
    """再埋め込みの要否を決めるためのハッシュ(モデルが変われば作り直す)。"""
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()


__all__ = [
    "EMBEDDING_MODELS",
    "EmbeddingInputChunk",
    "EmbeddingModelSpec",
    "embedding_input",
    "input_hash",
    "model_config",
    "query_input",
]
