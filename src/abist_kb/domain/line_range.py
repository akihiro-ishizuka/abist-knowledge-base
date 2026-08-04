"""行範囲の切り出しと range_hash(可視化の出典検証用)。

旧実装 `tools/lib/line-range.js` の移植。SceneSpec `sources[].content_hash` の
定義はここに一本化する:

  1. 先頭の U+FEFF(BOM)を1個だけ除去
  2. `\n` で分割し、各行末の `\r` を除去(kb-search `get_document` と同じ挙動)
  3. `lines[start-1 .. end-1]`(1始まり・両端含む)を `\n` で join(trim なし・末尾改行なし)
  4. その SHA-256(UTF-8 バイト列)

front matter は剥がさない(start/end は front matter 込みの物理行番号のため)。
chunker のチャンクハッシュは正規化が強く行範囲から再現できないので使わない。

挙動の正しさは `tests/fixtures/kernel/line-range.json`(旧実装を実行して得た
ゴールデン値)で判定する。
"""

from __future__ import annotations

from dataclasses import dataclass

from abist_kb.domain.frontmatter import BOM, sha256_hex

RANGE_OUT_OF_BOUNDS = "RANGE_OUT_OF_BOUNDS"


@dataclass(frozen=True, slots=True)
class SliceResult:
    """`slice_range` の戻り値。範囲が不正なら `ok=False` で `reason` を持つ。"""

    ok: bool
    total_lines: int
    text: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RangeHashResult:
    """`range_hash` の戻り値。範囲が不正なら `ok=False` で `reason` を持つ。"""

    ok: bool
    total_lines: int
    hash: str | None = None
    reason: str | None = None


def split_doc_lines(text: str) -> list[str]:
    """BOM 除去・CRLF 正規化済みの行配列を返す。"""
    without_bom = text[len(BOM) :] if text.startswith(BOM) else text
    return [line[:-1] if line.endswith("\r") else line for line in without_bom.split("\n")]


def _is_integer(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    return False


def slice_range(text: str, start_line: object, end_line: object) -> SliceResult:
    """1始まり・両端含みで行範囲を切り出す。"""
    lines = split_doc_lines(text)
    total_lines = len(lines)
    if (
        not _is_integer(start_line)
        or not _is_integer(end_line)
        or start_line < 1
        or end_line < start_line
        or end_line > total_lines
    ):
        return SliceResult(ok=False, total_lines=total_lines, reason=RANGE_OUT_OF_BOUNDS)
    start = int(start_line)
    end = int(end_line)
    return SliceResult(ok=True, total_lines=total_lines, text="\n".join(lines[start - 1 : end]))


def range_hash(text: str, start_line: object, end_line: object) -> RangeHashResult:
    """行範囲の SHA-256(SceneSpec `sources[].content_hash` の正規計算)。"""
    sliced = slice_range(text, start_line, end_line)
    if not sliced.ok:
        return RangeHashResult(ok=False, total_lines=sliced.total_lines, reason=sliced.reason)
    assert sliced.text is not None
    return RangeHashResult(ok=True, total_lines=sliced.total_lines, hash=sha256_hex(sliced.text))


__all__ = [
    "RANGE_OUT_OF_BOUNDS",
    "RangeHashResult",
    "SliceResult",
    "range_hash",
    "slice_range",
    "split_doc_lines",
]
