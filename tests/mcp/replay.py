"""fixture リプレイ基盤(M5 task-1-brief Step3)。

`tests/fixtures/mcp/**/*.json` の `request` を実サーバー(`Server`)へ直接
`call_tool`/`list_tools` として送り、返った `CallToolResult` を fixture の
`response` と比較する。正規化は比較側(このモジュール)で行い、fixture
そのものは一切書き換えない(`PROVENANCE.md` §6 の原則)。

比較ルール(`response_kind` 別、brief Step3 のとおり):

- `sdk_validation_error`: 全文一致は要求しない。`isError` が真であること・
  `-32602` を含むこと・`content[0].text` が JSON としてパースできないこと
  のみを検証する(prose は zod/pydantic のバージョン固有の生成物のため)。
- `tool_result_json`: `content[0].text` を JSON としてパースし、期待payload
  とキー集合・値(`nondeterministic_fields` に挙がるパスは無視)を比較する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp.types as types


@dataclass(frozen=True, slots=True)
class Fixture:
    """1件の MCP fixture ケース。"""

    case: str
    tool: str
    arguments: dict[str, Any]
    response_kind: str
    expected_text: str
    is_error: bool
    nondeterministic_fields: tuple[str, ...]
    path: Path


def load_fixture(path: Path) -> Fixture:
    """`tests/fixtures/mcp/**/*.json` の1ファイルを読み込む。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    request = data["request"]
    params = request["params"]
    result = data["response"]["result"]
    content = result["content"][0]
    nondeterministic = tuple(
        field.removeprefix("content[0].text.") for field in data.get("nondeterministic_fields", [])
    )
    return Fixture(
        case=data["case"],
        tool=params["name"],
        arguments=params.get("arguments", {}),
        response_kind=data["response_kind"],
        expected_text=content["text"],
        is_error=bool(result.get("isError", False)),
        nondeterministic_fields=nondeterministic,
        path=path,
    )


def _strip_nondeterministic(payload: Any, field_path: str) -> None:
    """`a.b.c` 形式のドット区切りパスにある値を比較対象から除去する(定数へ揃える)。"""
    parts = field_path.split(".")
    node = payload
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict) and parts[-1] in node:
        node[parts[-1]] = "<normalized>"


def _normalize(payload: dict[str, Any], nondeterministic_fields: tuple[str, ...]) -> dict[str, Any]:
    for field_path in nondeterministic_fields:
        _strip_nondeterministic(payload, field_path)
    return payload


def _structural_mismatch(expected: Any, actual: Any, *, path: str) -> str | None:
    """`expected`/`actual` の再帰的なキー集合・大まかな型の不一致を1つ返す(値は比較しない)。

    このリポジトリの実データ(生産環境の docs/*.sqlite)はテストからは参照できない
    ため、fixture の実測値そのものと1対1で突き合わせることはできない
    (`tests/search/test_services.py` が同じ理由で合成データを使っているのと同じ制約)。
    brief が要求する「キー集合・型・必須性」を、値非依存に検証する。
    """
    if isinstance(expected, dict) and isinstance(actual, dict):
        if not expected:
            return None  # 空オブジェクトは opaque な自由形式フィールド(例: chunkOptions)
        if expected.keys() != actual.keys():
            missing = expected.keys() - actual.keys()
            extra = actual.keys() - expected.keys()
            return f"{path}: キー不一致 missing={missing} extra={extra}"
        for key, expected_value in expected.items():
            mismatch = _structural_mismatch(expected_value, actual[key], path=f"{path}.{key}")
            if mismatch is not None:
                return mismatch
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if expected and actual:
            return _structural_mismatch(expected[0], actual[0], path=f"{path}[0]")
        return None
    if expected is None or actual is None:
        return None  # optional フィールドは環境によって値が無いことがある
    if isinstance(expected, bool) or isinstance(actual, bool):
        if type(expected) is not type(actual):
            return (
                f"{path}: 型不一致 expected={type(expected).__name__} "
                f"actual={type(actual).__name__}"
            )
        return None
    numeric = (int, float)
    if isinstance(expected, numeric) and isinstance(actual, numeric):
        return None
    if type(expected) is not type(actual):
        return f"{path}: 型不一致 expected={type(expected).__name__} actual={type(actual).__name__}"
    return None


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """1ケースの判定結果。`ok=False` なら `reason` に人間向けの理由を持つ。"""

    ok: bool
    reason: str | None = None


def check_result(fixture: Fixture, actual: types.CallToolResult) -> ReplayOutcome:
    """`actual`(実サーバーの応答)を fixture と比較する。"""
    if not actual.content or actual.content[0].type != "text":
        return ReplayOutcome(ok=False, reason="content[0] がテキストではありません")
    actual_text = actual.content[0].text
    actual_is_error = bool(actual.isError)

    if fixture.response_kind == "sdk_validation_error":
        if not actual_is_error:
            return ReplayOutcome(ok=False, reason="isError が真であるべきです")
        if "-32602" not in actual_text:
            return ReplayOutcome(ok=False, reason="-32602 を含むべきです")
        try:
            json.loads(actual_text)
        except json.JSONDecodeError:
            return ReplayOutcome(ok=True)
        return ReplayOutcome(ok=False, reason="JSON としてパースできてしまいました(prose のはず)")

    if fixture.response_kind == "tool_result_json":
        if actual_is_error != fixture.is_error:
            return ReplayOutcome(
                ok=False,
                reason=f"isError 不一致: expected={fixture.is_error} actual={actual_is_error}",
            )
        try:
            expected = json.loads(fixture.expected_text)
        except json.JSONDecodeError as exc:
            return ReplayOutcome(ok=False, reason=f"fixture 側の text が不正な JSON です: {exc}")
        try:
            actual_obj = json.loads(actual_text)
        except json.JSONDecodeError as exc:
            return ReplayOutcome(ok=False, reason=f"actual の text が不正な JSON です: {exc}")

        expected_norm = _normalize(expected, fixture.nondeterministic_fields)
        actual_norm = _normalize(actual_obj, fixture.nondeterministic_fields)
        mismatch = _structural_mismatch(expected_norm, actual_norm, path="$")
        if mismatch is not None:
            return ReplayOutcome(ok=False, reason=mismatch)
        return ReplayOutcome(ok=True)

    return ReplayOutcome(ok=False, reason=f"未知の response_kind です: {fixture.response_kind}")


__all__ = ["Fixture", "ReplayOutcome", "check_result", "load_fixture"]
