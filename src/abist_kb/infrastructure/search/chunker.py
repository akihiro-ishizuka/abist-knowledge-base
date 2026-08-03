"""Markdown チャンカー(見出しベース分割)。

旧実装 `tools/lib/chunker.js` の移植。検索結果に「出典・行番号」を添えるため、
チャンクは必ず元ファイルの行範囲(`start_line` / `end_line`、front matter を含む
物理行番号)を持つ。

方針(旧実装のコメントをそのまま踏襲):
  - 見出し構造で切る。`heading_path` で文書内の位置が分かるようにする。
  - 表とコードブロックは途中で切らない(切ると意味を失うため)。
  - サイズは目安であって上限ではない。分割不能な塊(巨大な表など)は超過して
    でも1チャンクに保つ。
  - 行番号は front matter を含むファイル全体での1始まり。

挙動の正しさは `tests/fixtures/kernel/chunker.json`(旧実装を実行して得た
ゴールデン値)と `tests/fixtures/real-docs/samples.json`(実データ40件)で
判定する。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from abist_kb.domain.frontmatter import js_is_blank, js_trim, sha256_hex

_DELIMITER_RE = re.compile(r"^---[ \t]*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
# JS の正規表現エンジンは `\r` を(`\n` 等と並ぶ)行終端文字として扱うため、
# multiline でなくても `.` は `\r` を跨がない。Python の `.` は既定で `\r` を
# 跨ぐ(除外するのは `\n` だけ)ため、`(.*)$` のままだと単純化された1回だけの
# CR除去(下記 `chunk_markdown` 参照)の後に残る内部CR(二重CR等、実データに
# 実在する)を title に取り込んでしまい、JS では見出しとして認識されない行を
# Python だけ見出しとして扱ってしまう(構造そのものが分岐し `content_hash` も
# 変わる)。`[^\r\n]*` で明示的に `\r` を除外して揃える。
_HEADING_RE = re.compile(r"^(#{1,6})\s+([^\r\n]*)$")
_LEADING_HEADING_MARKER_RE = re.compile(r"^#{1,6}\s")
_TABLE_ROW_RE = re.compile(r"^\s*\|")
# 同じ理由(JS は `\r` も行終端として扱う)で、Python の `re.MULTILINE` の `$`
# (`\n` の直前にしか反応しない)ではなく、`\r\n`/`\r`/`\n`/文字列末尾のいずれの
# 直前にも反応する明示的な先読みを使う。
_TRAILING_WS_RE = re.compile(r"[ \t]+(?=\r\n|\r|\n|\Z)")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class ChunkOptions:
    """チャンク分割のしきい値。`min_tokens` は意図的に未使用(下記参照)。"""

    max_tokens: int = 800
    # 埋め込みモデルの入力上限を超えないための強制分割しきい値。
    hard_max_tokens: int = 4000
    # 見出し直下が短すぎる場合に次の見出しと結合する下限。
    #
    # 旧実装はこれを受け取るが結合には使わない(no-op)。短いセクションを
    # 併合すると heading_path が壊れ、見出し構造を保つという目的そのものが
    # 崩れるため。旧実装のコメントをそのまま踏襲し、ここで「修正」しない。
    min_tokens: int = 40


DEFAULT_CHUNK_OPTIONS = ChunkOptions()


@dataclass(frozen=True, slots=True)
class Chunk:
    """チャンク1件。`text` は front matter を含まない本文の一部。"""

    index: int
    heading_path: str
    text: str
    start_line: int
    end_line: int
    token_estimate: int
    content_hash: str
    split_by_size: bool = False


def estimate_tokens(text: str) -> int:
    """トークン数を推定する(コードポイント単位)。

    正確なトークナイザは埋め込みモデル依存なので、ここでは「日本語=ほぼ1文字
    1トークン」「英数字=約4文字1トークン」の近似で足りる。チャンク分割の
    目安にしか使わない。CJK 範囲は U+3000-30FF・U+3400-4DBF・U+4E00-9FFF・
    U+F900-FAFF・U+FF00-FFEF。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        code = ord(ch)
        if (
            (0x3000 <= code <= 0x30FF)
            or (0x3400 <= code <= 0x4DBF)
            or (0x4E00 <= code <= 0x9FFF)
            or (0xF900 <= code <= 0xFAFF)
            or (0xFF00 <= code <= 0xFFEF)
        ):
            cjk += 1
        else:
            other += 1

    return math.ceil(cjk + other / 4)


@dataclass(slots=True)
class _Section:
    """分割中間表現。`headings` は見出しスタックのタイトル列(ルート→葉)。"""

    headings: list[str]
    lines: list[str]
    start_line: int


@dataclass(slots=True)
class _Part:
    lines: list[str]
    start_line: int
    truncated_block: bool = False


@dataclass(slots=True)
class _HeadingFrame:
    level: int
    title: str


def _split_frontmatter(lines: list[str]) -> tuple[int, list[str]]:
    """front matter を除いた本文と、その開始行(1始まり)を返す。"""
    if not lines or not _DELIMITER_RE.match(lines[0]):
        return 1, lines
    for i in range(1, len(lines)):
        if _DELIMITER_RE.match(lines[i]):
            return i + 2, lines[i + 1 :]
    return 1, lines


def _split_into_sections(body_lines: list[str], body_start_line: int) -> list[_Section]:
    """本文を「見出しで始まる節」に分ける。コードブロック内の `#` は見出しとして扱わない。"""
    sections: list[_Section] = []
    current = _Section(headings=[], lines=[], start_line=body_start_line)
    heading_stack: list[_HeadingFrame] = []
    in_fence = False
    fence_marker: str | None = None

    def push_current() -> None:
        if current.lines or current.headings:
            sections.append(current)

    for i, line in enumerate(body_lines):
        line_number = body_start_line + i

        fence = _FENCE_RE.match(line)
        if fence:
            if not in_fence:
                in_fence = True
                fence_marker = fence.group(1)[0]
            elif fence.group(1)[0] == fence_marker:
                in_fence = False
                fence_marker = None

        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            push_current()
            level = len(heading.group(1))
            title = js_trim(heading.group(2))

            while heading_stack and heading_stack[-1].level >= level:
                heading_stack.pop()
            heading_stack.append(_HeadingFrame(level=level, title=title))

            current = _Section(
                headings=[h.title for h in heading_stack],
                lines=[line],
                start_line=line_number,
            )
            continue

        current.lines.append(line)

    push_current()

    return [s for s in sections if not js_is_blank("".join(s.lines))]


def _is_table_row(line: str) -> bool:
    return bool(_TABLE_ROW_RE.match(line))


def _split_section(section: _Section, max_tokens: int) -> list[_Part]:
    """節が `max_tokens` を超える場合に、意味の塊を壊さない位置で分割する。"""
    lines = section.lines
    start_line = section.start_line

    if estimate_tokens("\n".join(lines)) <= max_tokens:
        return [_Part(lines=lines, start_line=start_line)]

    breakable: set[int] = set()
    in_fence = False
    fence_marker: str | None = None

    for i, line in enumerate(lines):
        fence = _FENCE_RE.match(line)
        if fence:
            if not in_fence:
                in_fence = True
                fence_marker = fence.group(1)[0]
            elif fence.group(1)[0] == fence_marker:
                in_fence = False
                fence_marker = None
            continue
        if in_fence:
            continue

        if (
            i > 0
            and js_is_blank(lines[i - 1])
            and not js_is_blank(line)
            and not _is_table_row(line)
        ):
            breakable.add(i)

    parts: list[_Part] = []
    part_start = 0

    for i in range(1, len(lines)):
        if i not in breakable:
            continue
        candidate = lines[part_start:i]
        if estimate_tokens("\n".join(candidate)) >= max_tokens:
            parts.append(_Part(lines=candidate, start_line=start_line + part_start))
            part_start = i

    parts.append(_Part(lines=lines[part_start:], start_line=start_line + part_start))

    return [p for p in parts if not js_is_blank("".join(p.lines))]


def _enforce_hard_limit(part: _Part, hard_max_tokens: int) -> list[_Part]:
    """意味の塊を保っても収まらないチャンクを、行単位で強制的に切る。"""
    if estimate_tokens("\n".join(part.lines)) <= hard_max_tokens:
        return [part]

    result: list[_Part] = []
    buffer: list[str] = []
    buffer_start = part.start_line

    for i, line in enumerate(part.lines):
        buffer.append(line)
        if estimate_tokens("\n".join(buffer)) >= hard_max_tokens:
            result.append(_Part(lines=buffer, start_line=buffer_start, truncated_block=True))
            buffer_start = part.start_line + i + 1
            buffer = []

    if buffer:
        result.append(_Part(lines=buffer, start_line=buffer_start, truncated_block=True))

    return result


def _content_hash(text: str) -> str:
    """重複チャンク抑制に使うハッシュ。先頭の見出し行を除いた本文を正規化して取る。"""
    lines = text.split("\n")
    if lines and _LEADING_HEADING_MARKER_RE.match(lines[0]):
        lines = lines[1:]
    normalized = "\n".join(lines)
    normalized = _TRAILING_WS_RE.sub("", normalized)
    normalized = _MULTI_BLANK_RE.sub("\n\n", normalized)
    # `js_trim` は JS の String.prototype.trim() と同じ集合(U+FEFF/BOM を含む)
    # で前後の空白を除去する(BOM が本文冒頭に残る実データ(bom_prefixed 層)で
    # ハッシュがズレないようにするため)。
    normalized = js_trim(normalized)
    return sha256_hex(normalized)


def _trim_trailing_blank(lines: list[str]) -> list[str]:
    """末尾の空行を落として行範囲を締める。"""
    end = len(lines)
    while end > 0 and js_is_blank(lines[end - 1]):
        end -= 1
    return lines[:end]


def chunk_markdown(content: str, options: ChunkOptions | None = None) -> list[Chunk]:
    """Markdown をチャンクに分割する。

    `content` は front matter を含むファイル全体。戻り値の `start_line` /
    `end_line` はそのファイル全体での1始まりの物理行番号。
    """
    opts = options if options is not None else DEFAULT_CHUNK_OPTIONS

    if not isinstance(content, str) or js_is_blank(content):
        return []

    # 改行コードを正規化する(チャンク本文は LF 統一。行番号は元ファイルのまま)。
    lines = [line[:-1] if line.endswith("\r") else line for line in content.split("\n")]
    body_start_line, body_lines = _split_frontmatter(lines)
    if js_is_blank("".join(body_lines)):
        return []

    sections = _split_into_sections(body_lines, body_start_line)

    # 「見出し行だけで中身が無い節」を次の節に前置きとして送る。
    #
    # 結合はここだけに限定する。短いという理由で結合すると heading_path が
    # 失われ、見出し構造を保つという目的が壊れるため。連続する見出しだけの
    # 節(# A → ## B → ### C と続く形)は全て保持する。先頭だけを残すと途中の
    # 行が欠けて start_line / end_line が本文とずれる。
    merged: list[_Section] = []
    pending_heading_only: list[_Section] = []

    for section in sections:
        has_content = not js_is_blank("".join(section.lines[1:]))

        if not has_content:
            pending_heading_only.append(section)
            continue

        entry_lines = list(section.lines)
        entry_start_line = section.start_line
        if pending_heading_only:
            # 節は行方向に連続しているので、そのまま前に連結すれば行範囲が保たれる。
            entry_lines = [
                line for pending in pending_heading_only for line in pending.lines
            ] + entry_lines
            entry_start_line = pending_heading_only[0].start_line
            pending_heading_only = []

        merged.append(
            _Section(headings=section.headings, lines=entry_lines, start_line=entry_start_line)
        )

    # 最後まで中身が無い見出しが残った場合はまとめて1チャンクにする。
    if pending_heading_only:
        merged.append(
            _Section(
                headings=pending_heading_only[-1].headings,
                lines=[line for pending in pending_heading_only for line in pending.lines],
                start_line=pending_heading_only[0].start_line,
            )
        )

    # min_tokens は将来のチューニング用に受け取るが、構造保持を優先して結合には使わない。
    _ = opts.min_tokens

    chunks: list[Chunk] = []
    for section in merged:
        heading_path = " > ".join(section.headings)
        for rough in _split_section(section, opts.max_tokens):
            for part in _enforce_hard_limit(rough, opts.hard_max_tokens):
                kept = _trim_trailing_blank(part.lines)
                if js_is_blank("".join(kept)):
                    continue

                text = "\n".join(kept)
                chunks.append(
                    Chunk(
                        index=len(chunks),
                        heading_path=heading_path,
                        text=text,
                        start_line=part.start_line,
                        end_line=part.start_line + len(kept) - 1,
                        token_estimate=estimate_tokens(text),
                        content_hash=_content_hash(text),
                        split_by_size=part.truncated_block,
                    )
                )

    return chunks


__all__ = [
    "DEFAULT_CHUNK_OPTIONS",
    "Chunk",
    "ChunkOptions",
    "chunk_markdown",
    "estimate_tokens",
]
