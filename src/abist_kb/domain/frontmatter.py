"""front matter のバイト保存パース(設計原則6準拠、YAMLライブラリ不使用)。

旧実装 `tools/lib/frontmatter.js` の行単位パーサをそのまま Python へ移植したもの。
挙動の正しさは `tests/fixtures/kernel/frontmatter.json`(旧実装を実行して得たゴールデン値)
で判定する。YAML パーサを使わないのは意図的で、以下を1バイトも変えずに保持するため:

  1. 更新対象キー以外(本文・既存キー行・行末・BOM)
  2. 改行コードは行単位で元のまま(esa 由来は front matter=LF・本文=CRLF の混在、
     catiadoc/B32doc は全CRLF)
  3. 解釈できない値(ブロック配列・ネスト)は読むだけで書き換えない(block_keys に記録)

既知の制限(意図的、旧実装から継承):
  - 行末コメント(`key: value # comment`)は値の一部として扱う。
  - 複数行スカラー(`|` / `>`)とブロック配列は `block_keys` に記録し `data` には出さない。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

BOM = "﻿"

# JS の `String.prototype.trim()` が空白とみなす文字集合(ECMA-262 の
# WhiteSpace + LineTerminator 生成規則)。Python の `str.strip()`(引数無し)は
# `str.isspace()` 相当の別の集合を使うため、この2つは一致しない:
#   - JS だけが U+FEFF(BOM/ZWNBSP)を空白として trim する
#     (`chunk_markdown('﻿')` が Python では1チャンク・JS では0チャンクに
#     なる、という実際の乖離の原因)。
#   - Python の既定 `strip()`/`\s` だけが U+0085(NEL)・U+001C〜U+001F
#     (制御文字)を空白とみなす(JS はみなさない)。
# `chunker.py`/`frontmatter.py`/`metadata_schema.py` に散在する「JSの trim() と
# 同じであるべき」空白判定は、素の `str.strip()` ではなくこのヘルパーへ寄せる。
# コードポイントで明示することで、エディタ上で見分けが付かない空白文字
# (NBSP・全角スペース等)の混入・取り違えを避ける。
_JS_WHITESPACE_CODEPOINTS = (
    0x09,  # TAB
    0x0B,  # VT
    0x0C,  # FF
    0x20,  # SPACE
    0xA0,  # NBSP
    0xFEFF,  # ZWNBSP / BOM
    0x1680,  # OGHAM SPACE MARK
    *range(0x2000, 0x200B),  # EN QUAD .. HAIR SPACE (U+2000-200A)
    0x202F,  # NARROW NO-BREAK SPACE
    0x205F,  # MEDIUM MATHEMATICAL SPACE
    0x3000,  # IDEOGRAPHIC SPACE
    0x0A,  # LF
    0x0D,  # CR
    0x2028,  # LINE SEPARATOR
    0x2029,  # PARAGRAPH SEPARATOR
)
_JS_WHITESPACE_CHARS = "".join(chr(c) for c in _JS_WHITESPACE_CODEPOINTS)

#: 正規表現の文字クラスとして使える形(`[...]` の中身)。JS の `\s`(RegExp)は
#: `.trim()` と同じ WhiteSpace/LineTerminator 集合にマッチするため、
#: `\s+` を JS 互換にしたい箇所(例: `metadata_schema.py` の空白圧縮)は
#: `re.compile(f"[{JS_WHITESPACE_CLASS}]+")` のようにこれを使う。
JS_WHITESPACE_CLASS = re.escape(_JS_WHITESPACE_CHARS)


def js_trim(text: str) -> str:
    """JS の `String.prototype.trim()` と同じ集合で前後の空白を除去する。"""
    return text.strip(_JS_WHITESPACE_CHARS)


def js_is_blank(text: str) -> bool:
    """JS の `trim() === ''` に相当する「空白のみ、または空」の判定。"""
    return js_trim(text) == ""


def js_trim_start(text: str) -> str:
    """JS の `String.prototype.trimStart()` と同じ集合で先頭の空白を除去する。"""
    return text.lstrip(_JS_WHITESPACE_CHARS)


_DELIMITER_RE = re.compile(r"^---[ \t]*$")
# JS の `$`(非multiline)は文字列の絶対末尾にしかマッチしないが、Python の `$`
# (非MULTILINE)は「絶対末尾」に加えて「末尾の改行の直前」にもマッチする
# (Python 特有の挙動)。`serialize_scalar('abc\n')` のような値でこの差が
# 表面化する: JS 側は `$` が末尾の `\n` の後ろまで届かず不一致になり
# クォートされるが、Python 側はここでマッチしてしまいクォートされない
# (front matter 行に生の改行が混入し、ブロックの構造が壊れる)。`\Z` は
# 常に「絶対末尾」だけを意味するため、ここで JS の `$` と揃える。
_PLAIN_SAFE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_./-]*\Z")
_YAML_RESERVED = frozenset(
    {
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "null",
        "nil",
        "~",
        "True",
        "False",
        "Yes",
        "No",
        "On",
        "Off",
        "Null",
        "NULL",
        "TRUE",
        "FALSE",
    }
)
#  Python の `\d` は Unicode 全角数字などにもマッチするが、JS の `\d` は
#  ASCII の `[0-9]` のみにマッチする(`tools/lib/frontmatter.js` 側)。
#  全角数字を Python 側だけが数値に変換してしまうと(例: '２０２４' が
#  `2024` になる)、`classify_document` の `source`/`managed_by` 判定が
#  JS 側と食い違いうる。ここは明示的に `[0-9]` を使う。
_INT_RE = re.compile(r"-?[0-9]+")
_FLOAT_RE = re.compile(r"-?[0-9]*\.[0-9]+")
_BLOCK_SCALAR_RE = re.compile(r"^[|>]")
_LEADING_WHITESPACE_RE = re.compile(r"^\s")


@dataclass(frozen=True, slots=True)
class FrontmatterResult:
    """`parse_frontmatter` の戻り値。"""

    has_frontmatter: bool
    bom: str
    eol: str
    """front matter ブロック内で支配的な EOL(追記時に使う)。"""
    data: dict[str, Any]
    """単一行スカラーのキーだけ(ブロック値のキーは含まない)。"""
    keys: tuple[str, ...]
    """front matter 内のキー出現順(ブロックキーも含む)。"""
    block_keys: tuple[str, ...]
    """ブロック値のため書き換え禁止のキー(順序は保証しない)。"""
    body: str
    """front matter を除いた本文(BOM を含まない、元の改行を保持)。"""
    raw: str
    """入力そのもの。"""


@dataclass(frozen=True, slots=True)
class SkippedKey:
    """`set_frontmatter_values` が書き換えを見送ったキーとその理由。"""

    key: str
    reason: str


@dataclass(frozen=True, slots=True)
class SetFrontmatterValuesResult:
    """`set_frontmatter_values` の戻り値。"""

    text: str
    changed: bool
    created: bool
    added: tuple[str, ...]
    updated: tuple[str, ...]
    skipped: tuple[SkippedKey, ...]


@dataclass
class _Line:
    """`_split_lines` の内部表現(行本体と行末)。set_frontmatter_values で書き換えるため可変。"""

    content: str
    eol: str


def _split_lines(text: str) -> list[_Line]:
    """文字列を行に分割し、各行の本体と行末(EOL)を保持する。

    末尾に EOL が無い行の eol は ''。
    """
    lines: list[_Line] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            has_cr = i > start and text[i - 1] == "\r"
            content = text[start : i - 1] if has_cr else text[start:i]
            lines.append(_Line(content=content, eol="\r\n" if has_cr else "\n"))
            start = i + 1
    if start < len(text):
        lines.append(_Line(content=text[start:], eol=""))
    return lines


def _join_lines(lines: list[_Line]) -> str:
    return "".join(line.content + line.eol for line in lines)


def _decode_scalar(raw: str) -> Any:
    """スカラー値の文字列表現をデコードする(クォート・インライン配列・数値・真偽値)。"""
    value = js_trim(raw)
    if value == "":
        return ""

    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")
    ):
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner

    if value.startswith("[") and value.endswith("]"):
        inner = js_trim(value[1:-1])
        if inner == "":
            return []
        return [_decode_scalar(item) for item in inner.split(",")]

    if value == "true":
        return True
    if value == "false":
        return False
    if value in ("null", "~"):
        return None
    if _INT_RE.fullmatch(value):
        return int(value)
    if _FLOAT_RE.fullmatch(value):
        return float(value)

    return value


def serialize_scalar(value: Any) -> str:
    """値を YAML スカラーへ直列化する。

    列挙値(esa / esa-sync / active …)は素の文字列で、それ以外の文字列はダブルクォートで出す。
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_quote(v) if isinstance(v, str) else serialize_scalar(v) for v in value]
        return "[" + ", ".join(parts) + "]"

    text = str(value)
    if _PLAIN_SAFE_RE.match(text) and text not in _YAML_RESERVED:
        return text
    return _quote(text)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _detect_eol(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _dominant_eol(lines: list[_Line], fallback: str) -> str:
    crlf = sum(1 for line in lines if line.eol == "\r\n")
    lf = sum(1 for line in lines if line.eol == "\n")
    if crlf == 0 and lf == 0:
        return fallback or "\n"
    return "\r\n" if crlf > lf else "\n"


def parse_frontmatter(text: str) -> FrontmatterResult:
    """front matter を解析する(YAML パーサ不使用、行単位)。"""
    source = text if isinstance(text, str) else ""
    bom = BOM if source.startswith(BOM) else ""
    rest = source[len(BOM) :] if bom else source

    def empty() -> FrontmatterResult:
        return FrontmatterResult(
            has_frontmatter=False,
            bom=bom,
            eol=_detect_eol(rest),
            data={},
            keys=(),
            block_keys=(),
            body=rest,
            raw=source,
        )

    lines = _split_lines(rest)
    if not lines or not _DELIMITER_RE.match(lines[0].content) or lines[0].eol == "":
        return empty()

    close_index = -1
    for i in range(1, len(lines)):
        if _DELIMITER_RE.match(lines[i].content):
            close_index = i
            break
    if close_index == -1:
        return empty()  # 閉じ区切りが無ければ frontmatter とみなさない

    block_lines = lines[1:close_index]
    data: dict[str, Any] = {}
    keys: list[str] = []
    block_keys: list[str] = []
    block_keys_seen: set[str] = set()
    last_key: str | None = None

    def _mark_block_key(key: str) -> None:
        if key not in block_keys_seen:
            block_keys_seen.add(key)
            block_keys.append(key)

    for line in block_lines:
        content = line.content
        if js_is_blank(content):
            continue

        if _LEADING_WHITESPACE_RE.match(content) or js_trim_start(content).startswith("- "):
            # 継続行 -> 直前のキーはブロック値
            if last_key is not None:
                _mark_block_key(last_key)
            continue
        if js_trim_start(content).startswith("#"):
            continue  # コメント行

        colon = content.find(":")
        if colon == -1:
            continue

        key = js_trim(content[:colon])
        if not key:
            continue
        raw_value = content[colon + 1 :]

        if key not in keys:
            keys.append(key)
        last_key = key

        stripped_value = js_trim(raw_value)
        if stripped_value == "":
            # 値が空 -> 次行がブロックの可能性。継続行を見つけるまでは空文字として扱う。
            data[key] = ""
        elif _BLOCK_SCALAR_RE.match(stripped_value):
            _mark_block_key(key)  # 複数行スカラー
        else:
            data[key] = _decode_scalar(raw_value)

    # ブロックキーの除去はループ完了後にまとめて行う(JS実装と同じタイミング)。
    # 「ブロック値 -> 同名キーの単一行スカラーで再宣言」(例: `a: |` の後に
    # `a: 5`)が起きると、ループ中に都度 pop する実装ではブロック判定より
    # 後に来た単一行代入が生き残ってしまい、JS(ループ完了後に blockKeys を
    # まとめて削除)と食い違う({'a': 5} vs {})。ここで一括削除することで
    # 「同じキーがどんな順序で再宣言されても、最終的にブロックキーなら
    # data から除く」という JS の挙動に揃える。
    for key in block_keys:
        data.pop(key, None)

    eol = _dominant_eol(block_lines, lines[0].eol)
    body_start = sum(len(line.content) + len(line.eol) for line in lines[: close_index + 1])

    return FrontmatterResult(
        has_frontmatter=True,
        bom=bom,
        eol=eol,
        data=data,
        keys=tuple(keys),
        block_keys=tuple(block_keys),
        body=rest[body_start:],
        raw=source,
    )


def body_of(text: str) -> str:
    """front matter を除いた本文を返す。"""
    return parse_frontmatter(text).body


def sha256_hex(text: str) -> str:
    """文字列全体の SHA-256(16進)。"""
    source = text if isinstance(text, str) else ""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def hash_body(text: str) -> str:
    """本文の SHA-256(front matter の変更に影響されない。backfill の非破壊検証に使う)。"""
    return sha256_hex(body_of(text))


def _same_value(a: Any, b: Any) -> bool:
    """値が等価か(配列は要素ごとに比較)。"""
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_value(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _find_key_line(lines: list[_Line], from_index: int, to_index: int, key: str) -> int:
    for i in range(from_index, to_index):
        content = lines[i].content
        if _LEADING_WHITESPACE_RE.match(content):
            continue
        colon = content.find(":")
        if colon == -1:
            continue
        if js_trim(content[:colon]) == key:
            return i
    return -1


def set_frontmatter_values(text: str, values: dict[str, Any]) -> SetFrontmatterValuesResult:
    """front matter のキーを設定する。値が既存と同じキーは行に触れない。

    front matter が存在しない場合は新規作成する。閉じ区切りの後に空行を入れない
    (受入条件「apply前後で本文ハッシュが一致する」を新規作成時にも満たすため)。
    """
    source = text if isinstance(text, str) else ""
    # JS 版は `Object.entries(updates).filter(([, v]) => v !== undefined)` で
    # 「値を渡さない(=このキーには触れない)」を表現する。Python には
    # `undefined` が無く、呼び出し側はその意図を `None` で表すしかないため、
    # ここで `None` を同じ「このキーはスキップする」の意味として扱う
    # (フィルタしないと `a: null` という行を書き込んでしまい、JS 側の
    # 「そもそも触れない」という意図と食い違う)。
    entries = [(key, value) for key, value in values.items() if value is not None]

    added: list[str] = []
    updated: list[str] = []
    skipped: list[SkippedKey] = []

    if not entries:
        return SetFrontmatterValuesResult(
            text=source,
            changed=False,
            created=False,
            added=(),
            updated=(),
            skipped=(),
        )

    parsed = parse_frontmatter(source)
    bom = parsed.bom
    rest = source[len(BOM) :] if bom else source

    if not parsed.has_frontmatter:
        eol = parsed.eol
        block_parts = ["---" + eol]
        for key, value in entries:
            block_parts.append(f"{key}: {serialize_scalar(value)}{eol}")
            added.append(key)
        block_parts.append("---" + eol)
        return SetFrontmatterValuesResult(
            text=bom + "".join(block_parts) + rest,
            changed=True,
            created=True,
            added=tuple(added),
            updated=tuple(updated),
            skipped=tuple(skipped),
        )

    lines = _split_lines(rest)
    close_index = -1
    for i in range(1, len(lines)):
        if _DELIMITER_RE.match(lines[i].content):
            close_index = i
            break

    pending: list[tuple[str, Any]] = []
    for key, value in entries:
        if key in parsed.block_keys:
            skipped.append(SkippedKey(key=key, reason="block-value"))
            continue
        if key in parsed.data:
            if _same_value(parsed.data[key], value):
                continue  # 同値 -> 行に触れない
            line_index = _find_key_line(lines, 1, close_index, key)
            if line_index == -1:
                skipped.append(SkippedKey(key=key, reason="line-not-found"))
                continue
            lines[line_index] = _Line(
                content=f"{key}: {serialize_scalar(value)}", eol=lines[line_index].eol
            )
            updated.append(key)
            continue
        pending.append((key, value))

    if pending:
        eol = parsed.eol
        insert_lines = [
            _Line(content=f"{key}: {serialize_scalar(value)}", eol=eol) for key, value in pending
        ]
        lines[close_index:close_index] = insert_lines
        added.extend(key for key, _ in pending)

    changed = bool(added) or bool(updated)
    return SetFrontmatterValuesResult(
        text=(bom + _join_lines(lines)) if changed else source,
        changed=changed,
        created=False,
        added=tuple(added),
        updated=tuple(updated),
        skipped=tuple(skipped),
    )


__all__ = [
    "BOM",
    "FrontmatterResult",
    "SetFrontmatterValuesResult",
    "SkippedKey",
    "body_of",
    "hash_body",
    "JS_WHITESPACE_CLASS",
    "js_is_blank",
    "js_trim",
    "js_trim_start",
    "parse_frontmatter",
    "serialize_scalar",
    "set_frontmatter_values",
    "sha256_hex",
]
