"""M1 Task 3 で採取した MCP 契約 fixture (`tests/fixtures/mcp/**`) の健全性検証。

`capture-mcp.mjs` は旧リポジトリの3つの kb-* MCP stdio サーバー(kb-download 8ツール /
kb-search 4ツール / kb-visualize 3ツール、計15ツール)を子プロセスで起動し、
JSON-RPC 2.0 の生レスポンスをそのまま `tests/fixtures/mcp/<server>/<tool>/<case>.json`
へ記録したものである。ここでのテスト対象は Python 実装ではなく、採取された fixture 自体
(設計書 §7.4.1 が要求する「ツール名 / 入出力 / サーバー構成」の3層の証拠)。

検証する性質:
  - tools-list.json に15ツールすべてが揃い、サーバー内訳が kb-download 8 /
    kb-search 4 / kb-visualize 3 であること
  - 各ツールに fixture ケースが1件以上あること(add_web_batch は skipped_reason 付きで可)
  - 副作用の無いツール(list_batches / search_kb / get_document / get_chunk /
    index_status / list_scene_kinds / check_visualize_deps)は2件以上あること
  - 各ケースの response_raw_line が単一の JSON-RPC レスポンスとしてパースでき、
    ANSI制御文字や埋め込み改行を含まないこと
  - content[0].type == "text" であり、text が JSON としてパースできること。
    ただし response_kind == "sdk_validation_error"(zod スキーマ検証失敗を
    McpServer が横取りして返す平文プロース。実際に採取して初めて判明した挙動)は
    JSON ではなく "MCP error -32602: Input validation error:" で始まる文字列で
    あることを別途確認する(brief の「JSONとしてパースできる」という前提は
    正常系・ハンドラ内エラーの応答形状であり、SDKレベルの検証エラーには
    そもそも当てはまらない。この区別自体がM5のPython移植が再現すべき契約)
  - isError フィールドの有無と値が記録されていること
  - 宣言された nondeterministic_fields が、run1(response)とrun2の応答を
    同一アルゴリズムで再 diff した結果と一致すること(オフラインで再現可能)
  - 採取済み内容に本物の mask_secrets を適用しても変化が無いこと
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from abist_kb.domain.redaction import mask_secrets

FIXTURES_MCP_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "mcp"
TOOLS_LIST_PATH = FIXTURES_MCP_DIR / "tools-list.json"

EXPECTED_SERVER_TOOL_COUNTS = {"kb-download": 8, "kb-search": 4, "kb-visualize": 3}

EXPECTED_TOOLS_BY_SERVER = {
    "kb-download": {
        "list_batches",
        "run_batch",
        "add_web_batch",
        "download_esa_post",
        "download_esa_category",
        "download_esa_search",
        "download_web",
        "download_git",
    },
    "kb-search": {"search_kb", "get_document", "get_chunk", "index_status"},
    "kb-visualize": {"list_scene_kinds", "check_visualize_deps", "render_scene"},
}

# brief: 副作用の無いツール(正常系を採る)は2件以上のケースを持つこと。
SIDE_EFFECT_FREE_TOOLS = {
    "list_batches",
    "search_kb",
    "get_document",
    "get_chunk",
    "index_status",
    "list_scene_kinds",
    "check_visualize_deps",
}

_ANSI_ESCAPE = "\x1b"


def _load_tools_list() -> dict[str, Any]:
    data = json.loads(TOOLS_LIST_PATH.read_text(encoding="utf-8"))
    assert data, f"tools-list.json が空: {TOOLS_LIST_PATH}"
    return data


def _case_files() -> list[Path]:
    paths = sorted(FIXTURES_MCP_DIR.glob("*/*/*.json"))
    assert paths, f"MCP ケース fixture が1つも無い: {FIXTURES_MCP_DIR}"
    return paths


def _load_case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# 決定性 diff(capture-mcp.mjs の同名関数と同じアルゴリズム。
# オフラインで独立に再現できることが nondeterministic_fields の主張の根拠になる)
# ===========================================================================


def _classify_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    return "other"  # pragma: no cover - JSON にはこの分岐に来る値は無い


def _comparable_view(result_or_error: Any) -> Any:
    """content[].text が JSON としてパースできれば、その中身まで再帰的に diff 対象にする。"""
    if not isinstance(result_or_error, dict):
        return result_or_error
    clone = json.loads(json.dumps(result_or_error))
    content = clone.get("content")
    if isinstance(content, list):
        new_content = []
        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ):
                try:
                    parsed = json.loads(item["text"])
                except (json.JSONDecodeError, ValueError):
                    new_content.append(item)
                else:
                    new_item = dict(item)
                    new_item["text"] = parsed
                    new_content.append(new_item)
            else:
                new_content.append(item)
        clone["content"] = new_content
    return clone


def _diff_paths(a: Any, b: Any, path: str, out: list[str]) -> None:
    if a == b:
        return
    ta, tb = _classify_type(a), _classify_type(b)
    if ta != tb:
        out.append(path or "$")
        return
    if ta == "null":
        return
    if ta == "array":
        for i in range(max(len(a), len(b))):
            av = a[i] if i < len(a) else None
            bv = b[i] if i < len(b) else None
            _diff_paths(av, bv, f"{path}[{i}]", out)
        return
    if ta == "object":
        for k in sorted(set(a.keys()) | set(b.keys())):
            _diff_paths(a.get(k), b.get(k), f"{path}.{k}" if path else k, out)
        return
    out.append(path or "$")


def _compute_nondeterministic_fields(
    response1: dict[str, Any], response2: dict[str, Any]
) -> list[str]:
    payload1 = response1.get("result", response1.get("error"))
    payload2 = response2.get("result", response2.get("error"))
    out: list[str] = []
    _diff_paths(_comparable_view(payload1), _comparable_view(payload2), "", out)
    return out


# ===========================================================================
# tools-list.json
# ===========================================================================


def test_tools_list_has_schema_and_all_three_servers() -> None:
    data = _load_tools_list()
    assert data["schema"] == 1
    assert set(data["servers"].keys()) == set(EXPECTED_SERVER_TOOL_COUNTS.keys())


@pytest.mark.parametrize("server", sorted(EXPECTED_SERVER_TOOL_COUNTS))
def test_server_tool_count_and_names(server: str) -> None:
    data = _load_tools_list()
    entry = data["servers"][server]
    assert entry["tool_count"] == EXPECTED_SERVER_TOOL_COUNTS[server]
    tool_names = {t["name"] for t in entry["tools"]}
    assert tool_names == EXPECTED_TOOLS_BY_SERVER[server]
    assert entry["protocol_version"], f"{server}: protocol_version が記録されていない"
    assert entry["server_info"]["name"] == server


def test_all_fifteen_tools_are_covered_exactly_once() -> None:
    data = _load_tools_list()
    all_tools = {(s, t["name"]) for s, entry in data["servers"].items() for t in entry["tools"]}
    assert len(all_tools) == 15, f"15ツールではない: {sorted(all_tools)}"


# ===========================================================================
# ケースファイル: 基本構造
# ===========================================================================


@pytest.mark.parametrize("path", _case_files(), ids=lambda p: str(p.relative_to(FIXTURES_MCP_DIR)))
def test_case_has_valid_schema(path: Path) -> None:
    data = _load_case(path)
    assert data["schema"] == 1
    assert data["server"] in EXPECTED_SERVER_TOOL_COUNTS
    assert data["tool"] in EXPECTED_TOOLS_BY_SERVER[data["server"]]
    assert data["case"] == path.stem


def test_every_tool_has_at_least_one_case() -> None:
    cases_by_tool: dict[tuple[str, str], list[Path]] = {}
    for path in _case_files():
        data = _load_case(path)
        cases_by_tool.setdefault((data["server"], data["tool"]), []).append(path)

    missing = []
    for server, tools in EXPECTED_TOOLS_BY_SERVER.items():
        for tool in tools:
            if (server, tool) not in cases_by_tool:
                missing.append(f"{server}/{tool}")
    assert not missing, f"ケースが1件も無いツールがある: {missing}"


def test_side_effect_free_tools_have_at_least_two_cases() -> None:
    cases_by_tool: dict[tuple[str, str], list[Path]] = {}
    for path in _case_files():
        data = _load_case(path)
        cases_by_tool.setdefault((data["server"], data["tool"]), []).append(path)

    shortfalls = []
    for (server, tool), paths in cases_by_tool.items():
        if tool in SIDE_EFFECT_FREE_TOOLS and len(paths) < 2:
            shortfalls.append(f"{server}/{tool} ({len(paths)}件)")
    assert not shortfalls, f"副作用の無いツールなのに2件未満: {shortfalls}"


def test_add_web_batch_is_schema_only_with_skipped_reason() -> None:
    path = FIXTURES_MCP_DIR / "kb-download" / "add_web_batch" / "schema_only.json"
    assert path.exists(), "add_web_batch のケースファイルが無い"
    data = _load_case(path)
    assert isinstance(data.get("skipped_reason"), str) and data["skipped_reason"]
    assert data.get("tool_schema_from_tools_list") is not None
    assert "response" not in data, "add_web_batch は呼び出していないため response を持たないはず"


# ===========================================================================
# ケースファイル: 実際に呼び出したケース(add_web_batch 以外)の応答形状
# ===========================================================================


def _called_case_files() -> list[Path]:
    return [p for p in _case_files() if "response" in _load_case(p)]


@pytest.mark.parametrize(
    "path", _called_case_files(), ids=lambda p: str(p.relative_to(FIXTURES_MCP_DIR))
)
def test_response_raw_line_is_single_valid_jsonrpc_response_without_ansi(path: Path) -> None:
    data = _load_case(path)
    raw = data["response_raw_line"]
    assert isinstance(raw, str) and raw, f"{path.name}: response_raw_line が空"
    assert _ANSI_ESCAPE not in raw, (
        f"{path.name}: response_raw_line に ANSI エスケープが混入している"
    )
    assert "\n" not in raw and "\r" not in raw, (
        f"{path.name}: response_raw_line に生の改行が含まれている(行分割の契約違反)"
    )
    parsed = json.loads(raw)  # 単一の JSON として一発でパースできること
    assert parsed == data["response"], (
        f"{path.name}: response_raw_line のパース結果が response と一致しない"
    )
    assert parsed.get("jsonrpc") == "2.0"
    assert parsed.get("id") == data["request"]["id"]


@pytest.mark.parametrize(
    "path", _called_case_files(), ids=lambda p: str(p.relative_to(FIXTURES_MCP_DIR))
)
def test_content_text_shape_matches_declared_response_kind(path: Path) -> None:
    data = _load_case(path)
    result = data["response"]["result"]
    content0 = result["content"][0]
    assert content0["type"] == "text"
    text = content0["text"]
    assert isinstance(text, str) and text

    kind = data["response_kind"]
    if kind == "tool_result_json":
        parsed = json.loads(text)  # 例外を出さずにパースできること自体がアサーション
        assert isinstance(parsed, dict)
    elif kind == "sdk_validation_error":
        prefix = "MCP error -32602: Input validation error:"
        msg = f"{path.name}: 想定プレフィックスで始まらない: {text[:80]!r}"
        assert text.startswith(prefix), msg
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)
    else:
        pytest.fail(f"{path.name}: 未知の response_kind: {kind!r}")


@pytest.mark.parametrize(
    "path", _called_case_files(), ids=lambda p: str(p.relative_to(FIXTURES_MCP_DIR))
)
def test_is_error_presence_and_value_are_recorded_correctly(path: Path) -> None:
    data = _load_case(path)
    result = data["response"]["result"]
    actually_present = "isError" in result
    assert data["is_error_present"] == actually_present
    if actually_present:
        assert data["is_error_value"] == result["isError"]
        assert isinstance(result["isError"], bool)
    else:
        assert data["is_error_value"] is None


@pytest.mark.parametrize(
    "path", _called_case_files(), ids=lambda p: str(p.relative_to(FIXTURES_MCP_DIR))
)
def test_nondeterministic_fields_match_independent_recomputation(path: Path) -> None:
    """宣言された nondeterministic_fields が、response と run2.response を
    同一アルゴリズムで再 diff した結果と(順序を除いて)一致すること。

    これにより「宣言したフィールドが run1/run2 間で実際に異なる唯一のフィールドである」
    という主張を、fixture 自体から独立に検証できる(オフライン再現可能)。
    """
    data = _load_case(path)
    recomputed = _compute_nondeterministic_fields(data["response"], data["run2"]["response"])
    assert set(recomputed) == set(data["nondeterministic_fields"]), (
        f"{path.name}: 宣言された nondeterministic_fields が再計算結果と一致しない\n"
        f"宣言: {sorted(data['nondeterministic_fields'])}\n再計算: {sorted(recomputed)}"
    )
    # run2 の応答自体も基本形状(単一JSON-RPC・id一致)を満たすこと
    run2_raw = data["run2"]["response_raw_line"]
    assert json.loads(run2_raw) == data["run2"]["response"]
    assert data["run2"]["response"]["id"] == data["run2"]["request"]["id"]


def test_search_kb_uses_real_eval_queries() -> None:
    """brief: search_kb の正常系は eval/queries.jsonl の実クエリを使うこと
    (日本語自然文クエリ q07・識別子クエリ q01)。Task 4 のベースラインと突き合わせられる。
    """
    japanese = _load_case(
        FIXTURES_MCP_DIR / "kb-search" / "search_kb" / "japanese_natural_query.json"
    )
    identifier = _load_case(FIXTURES_MCP_DIR / "kb-search" / "search_kb" / "identifier_query.json")
    assert (
        japanese["request"]["params"]["arguments"]["query"]
        == "CATIAの起動時間を短縮するにはどうすればよいか"
    )
    assert identifier["request"]["params"]["arguments"]["query"] == "shrink_clamp_bellow_overlap_mm"


def test_zero_hit_search_kb_case_actually_has_zero_results() -> None:
    data = _load_case(FIXTURES_MCP_DIR / "kb-search" / "search_kb" / "zero_hit_query.json")
    payload = json.loads(data["response"]["result"]["content"][0]["text"])
    assert payload["count"] == 0
    assert payload["results"] == []


def test_render_scene_error_codes_match_brief() -> None:
    invalid = _load_case(
        FIXTURES_MCP_DIR / "kb-visualize" / "render_scene" / "invalid_scene_spec.json"
    )
    mismatch = _load_case(
        FIXTURES_MCP_DIR / "kb-visualize" / "render_scene" / "source_hash_mismatch.json"
    )
    invalid_payload = json.loads(invalid["response"]["result"]["content"][0]["text"])
    mismatch_payload = json.loads(mismatch["response"]["result"]["content"][0]["text"])
    assert invalid_payload["code"] == "INVALID_SCENE_SPEC"
    assert mismatch_payload["code"] == "SOURCE_HASH_MISMATCH"


def test_download_git_deviation_is_documented() -> None:
    """download_git は brief 指定の「不正リポジトリURLエラー」ではなく、空文字列による
    zod スキーマ検証エラーを採用した(理由: ハンドラに URL 形式チェックが無く、非空文字列を
    渡すと data/git-cache/ への実 git clone が spawn されるため)。この逸脱が
    fixture 自体に記録されていることを確認する。
    """
    data = _load_case(
        FIXTURES_MCP_DIR
        / "kb-download"
        / "download_git"
        / "empty_repository_schema_validation.json"
    )
    assert isinstance(data.get("deviation_from_brief"), str) and data["deviation_from_brief"]


# ===========================================================================
# 秘密情報スキャン(Task 2 と同じ方式: 本物の mask_secrets を全ファイルへ適用)
# ===========================================================================


def test_no_secrets_leak_into_mcp_fixtures() -> None:
    checked = 0
    for path in [*_case_files(), TOOLS_LIST_PATH]:
        text = path.read_text(encoding="utf-8")
        masked = mask_secrets(text)
        assert masked == text, (
            f"{path}: mask_secrets が反応する内容が残っている(秘密情報漏洩の疑い)"
        )
        checked += 1
    assert checked > 0
