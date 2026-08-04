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
  と**キー集合・型のみ**を比較する(`nondeterministic_fields` に挙がるパスは
  比較前に除去)。**値そのものは基本的に比較しない** — CI はこのリポジトリの
  テストから生産環境の `docs/`・`data/*.sqlite` を参照できず(`tests/search/
  test_services.py` と同じ制約)、テストは合成した小さなコーパスに対して実行
  するため、fixture が記録した実測値(パス・件数・スコア・tokenizer 判定等)
  と一致するはずがない。したがって `tool_result_json` の replay は「キー集合・
  必須性・大まかな型が変わっていないこと」を保証するのみで、「返す値が正しい
  こと」は保証しない(値の正しさは `tests/search/test_services.py` 等の単体
  テストが別途担う)。この限界は `tests/fixtures/PROVENANCE.md` §3にも記録
  してある — replay が「ビット互換」と呼ぶのは JSON-RPC 応答の**形**であって、
  中身の実測値ではない。
  例外として `_STATIC_VALUE_FIELDS` に列挙したフィールドだけは値まで比較する。
  これは「入力データや索引の中身に一切依存しない、コード上のリテラル文字列や
  設定値」であることを個別に確認した上で追加したホワイトリストであり、
  fixture 側を書き換えずに(`fixture そのものは一切書き換えない`原則を保った
  まま)値検証を上乗せする唯一の手段。対象を広げる際は、その値が本当に
  データ非依存(索引の中身にもテスト実行環境にも左右されない)であることを
  確認すること — 大半のフィールド(パス・件数・tokenizer 判定結果・
  vectorAvailable 等の bool フラグを含む)は実際には索引/環境依存であり、
  安易にこの一覧へ加えると CI で偽陽性の失敗を招く。
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


#: `{case} 相対パス (例: "kb-search/get_document/path_traversal_attempt")` ->
#: 値まで完全一致を要求するドット区切りフィールドパスのタプル。
#: ここに載せてよいのは、引数・索引・実行環境の値に一切依存しない、コード上の
#: リテラル文字列/設定値だけ(モジュール docstring 参照)。現時点で確認できた
#: のは以下の1件のみ: `get_document` のパス封じ込め拒否メッセージ
#: (`kb_search.py` の `"docs/ の外は参照できません"`)は引数のパス文字列を
#: 含まない固定文言であり、値まで安全に比較できる。他の error 系フィールド
#: (`nonexistent_path`/`nonexistent_chunk_id`/`invalid_url_format`/
#: `unknown_batch_name` 等)はいずれも引数値やサンドボックスパスをメッセージに
#: 埋め込んでおり、fixture の実測値とテスト実行時の値が一致しないため対象外。
_STATIC_VALUE_FIELDS: dict[str, tuple[str, ...]] = {
    "kb-search/get_document/path_traversal_attempt": ("error",),
}


def _value_mismatch(expected: Any, actual: Any, *, field_path: str) -> str | None:
    node_expected, node_actual = expected, actual
    for part in field_path.split("."):
        if not isinstance(node_expected, dict) or part not in node_expected:
            return None  # フィールド自体が無ければ構造比較側が既に検出済み
        if not isinstance(node_actual, dict) or part not in node_actual:
            return None
        node_expected = node_expected[part]
        node_actual = node_actual[part]
    if node_expected != node_actual:
        return (
            f"$.{field_path}: 値不一致(静的フィールド) "
            f"expected={node_expected!r} actual={node_actual!r}"
        )
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

        static_key = "/".join(
            fixture.path.parts[fixture.path.parts.index("mcp") + 1 :]
        ).removesuffix(".json")
        for field_path in _STATIC_VALUE_FIELDS.get(static_key, ()):
            value_mismatch = _value_mismatch(expected, actual_obj, field_path=field_path)
            if value_mismatch is not None:
                return ReplayOutcome(ok=False, reason=value_mismatch)

        return ReplayOutcome(ok=True)

    return ReplayOutcome(ok=False, reason=f"未知の response_kind です: {fixture.response_kind}")


__all__ = ["Fixture", "ReplayOutcome", "check_result", "load_fixture"]
