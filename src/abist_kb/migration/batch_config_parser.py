"""旧 `batch-config.js` を安全に読み書きするための移植モジュール。

旧実装 `tools/lib/batch-config-store.js` の `formatConfig`/`loadBatchConfigsFresh` の
移植。挙動の正しさは `tests/fixtures/kernel/batch-config.json`(旧実装を実行して
得たゴールデン値)で判定する。

**JavaScript を実行しない。** `loadBatchConfigsFresh` は Node の動的 `import()` で
`batch-config.js` を実行して `batchConfigs` を取り出していたが、移行対象は
「そのうち触れなくなる旧リポジトリの設定ファイル」であり、内容を信頼できない
前提で扱う必要がある(設計書 §11.2)。そのため Python 側は `export const
batchConfigs = {...};` の右辺を**リテラルのみ受け付ける再帰下降パーサ**で読む。
`eval` 相当の手段、`json5` などの緩いパーサ、正規表現での場当たり的抽出はいずれも
使わない — リテラル文法の外側にあるもの(式・関数呼び出し・テンプレートリテラル・
変数参照)を確実に拒否できることが目的であり、緩いパーサはそれを保証しない。

受け付ける値: オブジェクト・配列・文字列(単引用符/二重引用符、`\\'` `\\"` `\\\\`
`\\n` 等のエスケープ、`\\uXXXX`/`\\xXX`)・数値・真偽値・`null`。それ以外のトークンに
出会った時点で `AppError(ErrorCode.UNSUPPORTED_BATCH_CONFIG)` を送出して停止する
(行・列と、修正済み JSON5/JSON を指定して再実行できる旨の `hint` を添える)。

`format_batch_config`/`serialize_batch_config_file` は逆方向(Python の dict を
`batch-config.js` と同じバイト列へ戻す)を担う。`formatConfig` とバイト互換でなければ
ならない: 両 UI サーバ(`batch-ui-server.js`/`chat-server.js`)の正規表現フォールバック
(`/export const batchConfigs = ({[\\s\\S]*?});/`)が壊れるため、ヘッダ・キーの
引用形式・インデントを変えてはいけない(旧実装のコメントそのまま)。fixtureの
`format_config_synthetic_roundtrip` ケースは、空配列・`0`(falsy値)・キー/値に
単引用符を含む合成設定を使っており、falsy値やクォートのエスケープを誤って
特別扱いする実装だけが引っかかるよう意図的に作られている。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from abist_kb.domain.errors import AppError, ErrorCode

_ASSIGNMENT_MARKER = "batchConfigs"

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_HEX4_RE = re.compile(r"[0-9a-fA-F]{4}")
_HEX2_RE = re.compile(r"[0-9a-fA-F]{2}")

_SIMPLE_ESCAPES: dict[str, str] = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
}

_FILE_HEADER = (
    "#!/usr/bin/env node\n"
    "\n"
    "// バッチダウンロード用の設定ファイル\n"
    "// 複数のカテゴリパスを配列で定義してください\n"
    "\n"
    "export const batchConfigs = "
)

_RECOVERY_HINT = "修正済みの JSON5/JSON ファイルを指定して再実行してください"


@dataclass(frozen=True, slots=True)
class _Position:
    """1始まりの行・列。"""

    line: int
    column: int


def _position_at(text: str, index: int) -> _Position:
    line = text.count("\n", 0, index) + 1
    last_newline = text.rfind("\n", 0, index)
    column = index - last_newline
    return _Position(line=line, column=column)


class _BatchConfigParser:
    """`export const batchConfigs = ` の右辺だけを解釈するリテラル限定パーサ。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.length = len(text)
        self.index = 0

    def _fail(self, message: str, index: int | None = None) -> None:
        pos = _position_at(self.text, self.index if index is None else index)
        raise AppError(
            ErrorCode.UNSUPPORTED_BATCH_CONFIG,
            f"{message}（{pos.line}行{pos.column}列目）",
            hint=_RECOVERY_HINT,
            details={"line": pos.line, "column": pos.column},
        )

    def _skip_trivia(self) -> None:
        while self.index < self.length:
            ch = self.text[self.index]
            if ch in " \t\r\n":
                self.index += 1
                continue
            if self.text.startswith("//", self.index):
                newline = self.text.find("\n", self.index)
                self.index = self.length if newline == -1 else newline
                continue
            if self.text.startswith("/*", self.index):
                end = self.text.find("*/", self.index + 2)
                if end == -1:
                    self._fail("ブロックコメントが閉じられていません")
                self.index = end + 2
                continue
            break

    def parse_top_level_object(self) -> dict[str, Any]:
        """`batchConfigs` 代入の右辺をオブジェクトとして解釈する。"""
        value = self.parse_value()
        if not isinstance(value, dict):
            self._fail("batchConfigs はオブジェクトリテラルである必要があります")
        self._skip_trivia()
        if self.index < self.length and self.text[self.index] == ";":
            self.index += 1
        return value

    def parse_value(self) -> Any:
        self._skip_trivia()
        if self.index >= self.length:
            self._fail("値が必要ですが入力が終端しました")

        ch = self.text[self.index]
        if ch == "{":
            return self._parse_object()
        if ch == "[":
            return self._parse_array()
        if ch in ("'", '"'):
            return self._parse_string(ch)
        if ch == "`":
            self._fail("テンプレートリテラルはサポートしていません")
        if ch == "-" or ch.isdigit():
            return self._parse_number()

        for keyword, literal in (("true", True), ("false", False), ("null", None)):
            if self.text.startswith(keyword, self.index):
                end = self.index + len(keyword)
                trailing_ident = end < self.length and (
                    self.text[end].isalnum() or self.text[end] in "_$"
                )
                if not trailing_ident:
                    self.index = end
                    return literal

        match = _IDENTIFIER_RE.match(self.text, self.index)
        if match:
            self._fail(f"変数参照・関数呼び出しはサポートしていません: {match.group(0)!r}")
        self._fail(f"サポートしていない文字です: {ch!r}")
        raise AssertionError("unreachable")  # pragma: no cover - _fail は必ず例外を送出する

    def _parse_key(self) -> str:
        self._skip_trivia()
        if self.index >= self.length:
            self._fail("キーが必要です")
        ch = self.text[self.index]
        if ch in ("'", '"'):
            return self._parse_string(ch)
        if ch == "`":
            self._fail("テンプレートリテラルのキーはサポートしていません")
        match = _IDENTIFIER_RE.match(self.text, self.index)
        if match:
            self.index = match.end()
            return match.group(0)
        self._fail("キーが必要です(文字列または識別子)")
        raise AssertionError("unreachable")  # pragma: no cover

    def _parse_object(self) -> dict[str, Any]:
        self.index += 1  # '{'
        obj: dict[str, Any] = {}
        self._skip_trivia()
        if self.index < self.length and self.text[self.index] == "}":
            self.index += 1
            return obj

        while True:
            key = self._parse_key()
            self._skip_trivia()
            if self.index >= self.length or self.text[self.index] != ":":
                self._fail("':' が必要です")
            self.index += 1
            obj[key] = self.parse_value()
            self._skip_trivia()

            if self.index < self.length and self.text[self.index] == ",":
                self.index += 1
                self._skip_trivia()
                if self.index < self.length and self.text[self.index] == "}":
                    self.index += 1
                    return obj
                continue
            if self.index < self.length and self.text[self.index] == "}":
                self.index += 1
                return obj
            self._fail("',' または '}' が必要です")

    def _parse_array(self) -> list[Any]:
        self.index += 1  # '['
        arr: list[Any] = []
        self._skip_trivia()
        if self.index < self.length and self.text[self.index] == "]":
            self.index += 1
            return arr

        while True:
            arr.append(self.parse_value())
            self._skip_trivia()

            if self.index < self.length and self.text[self.index] == ",":
                self.index += 1
                self._skip_trivia()
                if self.index < self.length and self.text[self.index] == "]":
                    self.index += 1
                    return arr
                continue
            if self.index < self.length and self.text[self.index] == "]":
                self.index += 1
                return arr
            self._fail("',' または ']' が必要です")

    def _parse_string(self, quote: str) -> str:
        start = self.index
        self.index += 1
        parts: list[str] = []
        while True:
            if self.index >= self.length:
                self._fail("文字列が閉じられていません", start)
            ch = self.text[self.index]
            if ch == quote:
                self.index += 1
                return "".join(parts)
            if ch == "\n":
                self._fail("文字列中に改行を含めることはできません", start)
            if ch == "\\":
                self.index += 1
                if self.index >= self.length:
                    self._fail("文字列が閉じられていません", start)
                esc = self.text[self.index]
                if esc == "\n":
                    self.index += 1  # 行継続(\<改行>は何も追加しない)
                    continue
                if esc == "u":
                    digits = self.text[self.index + 1 : self.index + 5]
                    if not _HEX4_RE.fullmatch(digits):
                        self._fail("不正な \\u エスケープです", self.index)
                    parts.append(chr(int(digits, 16)))
                    self.index += 5
                    continue
                if esc == "x":
                    digits = self.text[self.index + 1 : self.index + 3]
                    if not _HEX2_RE.fullmatch(digits):
                        self._fail("不正な \\x エスケープです", self.index)
                    parts.append(chr(int(digits, 16)))
                    self.index += 3
                    continue
                parts.append(_SIMPLE_ESCAPES.get(esc, esc))
                self.index += 1
                continue
            parts.append(ch)
            self.index += 1

    def _parse_number(self) -> int | float:
        match = _NUMBER_RE.match(self.text, self.index)
        if not match:
            self._fail("数値が不正です")
        text = match.group(0)
        self.index = match.end()
        if "." in text or "e" in text or "E" in text:
            return float(text)
        return int(text)


def parse_batch_config(text: str) -> dict[str, Any]:
    """`export const batchConfigs = {...};` の右辺をオブジェクトとして解釈する。

    リテラル(オブジェクト・配列・文字列・数値・真偽値・null)以外のトークンに
    出会った時点で `AppError(ErrorCode.UNSUPPORTED_BATCH_CONFIG)` を送出する。
    JavaScript を実行しない(モジュール docstring 参照)。
    """
    marker_index = text.find(_ASSIGNMENT_MARKER)
    if marker_index == -1:
        raise AppError(
            ErrorCode.UNSUPPORTED_BATCH_CONFIG,
            "'export const batchConfigs = ...' が見つかりません",
            hint=_RECOVERY_HINT,
            details={"line": 1, "column": 1},
        )

    equals_index = text.find("=", marker_index)
    if equals_index == -1:
        raise AppError(
            ErrorCode.UNSUPPORTED_BATCH_CONFIG,
            "'batchConfigs' への代入('=')が見つかりません",
            hint=_RECOVERY_HINT,
            details={"line": 1, "column": 1},
        )

    parser = _BatchConfigParser(text)
    parser.index = equals_index + 1
    return parser.parse_top_level_object()


def _escape_single_quote(value: str) -> str:
    return value.replace("'", "\\'")


def format_batch_config(value: Any, indent: int = 0) -> str:
    """`batch-ui-server.js`/`batch-config-store.js` の `formatConfig` とバイト互換の直列化。

    配列の要素は(元の型に関係なく)`String(item)` 相当で単引用符の文字列として出す
    (JS の `formatConfig` がそうしているため)。オブジェクトのキーも単引用符。
    値がオブジェクト・配列でなければ `JSON.stringify` 相当(文字列は二重引用符)。
    """
    spaces = "  " * indent
    if isinstance(value, list):
        if not value:
            return "[]"
        items = ",\n".join(f"{spaces}  '{_escape_single_quote(str(item))}'" for item in value)
        return f"[\n{items}\n{spaces}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key, entry in value.items():
            if isinstance(entry, (list, dict)):
                formatted = format_batch_config(entry, indent + 1)
            else:
                formatted = _json_stringify(entry)
            lines.append(f"{spaces}  '{_escape_single_quote(str(key))}': {formatted}")
        items = ",\n".join(lines)
        return f"{{\n{items}\n{spaces}}}"
    return _json_stringify(value)


def _json_stringify(value: Any) -> str:
    """`JSON.stringify` 相当(非ASCII文字はエスケープしない、UTF-8のまま保つ)。"""
    return json.dumps(value, ensure_ascii=False)


def serialize_batch_config_file(config: dict[str, Any]) -> str:
    """`saveBatchConfigs` が書き出す `batch-config.js` 全体をバイト互換で組み立てる。"""
    return f"{_FILE_HEADER}{format_batch_config(config)};\n"


__all__ = [
    "format_batch_config",
    "parse_batch_config",
    "serialize_batch_config_file",
]
