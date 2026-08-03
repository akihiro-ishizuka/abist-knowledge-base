"""チャンクの埋め込み入力構築(見出し文脈を含めて意味を補強する)。

旧実装 `tools/lib/embeddings.js` の移植。モデルごとの入力文字数上限・
接頭辞(passage/query)を保持し、旧実装とビット一致する `embedding_input` /
`query_input` / `input_hash` を提供する。

**モデル名は旧実装の文字列(`Xenova/multilingual-e5-small` 等)をそのまま
辞書キーとして使う。** `input_hash` はモデル名の文字列そのものをハッシュに
含めるため、Python 側で別名(例: `intfloat/...`)に変えると
`tests/fixtures/embedding/gate-samples.json` との照合(M8 の埋め込み再利用
ゲートの前提条件)が壊れる。

**二重身分の注意(M4 実装者向け)。** M4 は実際にモデルを読み込む際、
sentence-transformers 経由で `intfloat/multilingual-e5-small`(非量子化)を
使う想定である。つまり「ハッシュに刻む識別子」は `Xenova/...`、
「実際にロードするモデル名」は `intfloat/...` であり、この2つは同じモデルを
指しながら文字列としては別物のまま共存させる。`EMBEDDING_MODELS` のキーを
ロード先の名前に合わせて統一しようとしないこと(詳細は
`tests/fixtures/PROVENANCE.md` の「モデル名の二重身分」節を参照)。

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


def _truncate_utf16_units(text: str, max_units: int) -> str:
    """JavaScript の `String.prototype.slice(0, n)` と同じ切り詰めを行う。

    JS の文字列インデックスは UTF-16 コード単位であり、Python の文字列
    インデックス(コードポイント単位)とは、絵文字や CJK拡張B等の
    「サロゲートペア(astral)」文字が上限より手前にあると異なる位置を指す。
    `str.encode("utf-16-le")` へ変換すれば1コード単位=2バイトになるため、
    そこでバイト数として切り詰めてから戻すことで同じ切り詰め位置を再現する。

    さらに JS 特有の副作用も再現する: 切り詰め位置がサロゲートペアの
    真ん中に来ると、JS は対になっていない上位サロゲート(lone high
    surrogate)を1コード単位として保持したままにする。この文字列を後段で
    `Buffer.from(str, 'utf8')` のように UTF-8 バイト列へ変換すると、
    対になっていないサロゲートは U+FFFD (REPLACEMENT CHARACTER) に化ける。
    Python は元々コードポイント単位でしか切り詰めないためこの現象が起こらず、
    `input_hash` (SHA-256 は UTF-8 バイト列に対して計算する) が一致しなくなる。
    ここでは `errors="surrogatepass"` で対になっていないサロゲートをいったん
    保持し、返す前に UTF-8 化した上で不正シーケンスを U+FFFD に正規化する
    ことで、後段のハッシュ計算がJSと同じバイト列になるようにする。
    """
    truncated_units = text.encode("utf-16-le")[: 2 * max_units]
    decoded = truncated_units.decode("utf-16-le", errors="surrogatepass")
    # Node の Buffer.from(str, 'utf8') は対になっていないサロゲートを
    # U+FFFD に置換して UTF-8 化する。Python の既定 "utf-8" エンコーダは
    # 対になっていないサロゲートで UnicodeEncodeError を送出するため、
    # ここで明示的に同じ置換を行い、以降どちらの言語でエンコードしても
    # 同じ UTF-8 バイト列になるようにしておく。
    return decoded.encode("utf-8", errors="replace").decode("utf-8")


def embedding_input(chunk: EmbeddingInputChunk, model: str) -> str:
    """埋め込みに与える入力テキストを作る。

    `title`/`heading_path` は偽値でない場合だけ含めるが、`text` は
    `embeddings.js:75-77` の `if (chunk.title) parts.push(...); ...;
    parts.push(chunk.text);` と同じく**常に**含める(偽値でも除外しない)。
    `text` だけ無条件フィルタから除外するのは、チャンカーが生成するチャンクの
    `text` が空文字列になることは無いため通常は表面化しないが、DB行から
    直接組み立てる場合(M4)は `{title:'T', text:''}` のようなケースがあり、
    `text` を除外すると結合結果が末尾の改行1つ分だけ短くなり `input_hash` が
    ズレる。`max_input_chars` で切り詰め、その後に接頭辞を付ける(接頭辞は
    切り詰めに数えない)。切り詰めは JS の UTF-16 コード単位に合わせる
    (`_truncate_utf16_units` 参照) — Python のコードポイント単位の切り詰めは、
    絵文字等の astral 文字が上限手前にあると異なる文字列・異なる `input_hash`
    を生む。
    """
    config = model_config(model)
    parts = [part for part in (chunk.title, chunk.heading_path) if part]
    parts.append(chunk.text)
    body = _truncate_utf16_units("\n".join(parts), config.max_input_chars)
    return config.passage_prefix + body


def query_input(query: str, model: str) -> str:
    """検索クエリ側の入力を作る(e5 系は文書と接頭辞が異なる)。"""
    config = model_config(model)
    return config.query_prefix + _truncate_utf16_units(str(query), config.max_input_chars)


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
