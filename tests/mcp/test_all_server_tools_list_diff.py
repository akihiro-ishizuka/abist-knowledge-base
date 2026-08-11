"""M5 task-4: `all` サーバーの `tools/list` が既存15ツールを一切変えていないことの証跡。

`tests/fixtures/mcp/tools-list.json`(kb-download/kb-search の契約 fixture)と
`add_web_batch` の schema_only fixture(`test_contract_download.py` が使うのと
同じ組)を突き合わせ、`all` に12個の新規ツールを足しても、既存ツールの
名前・入力スキーマ・`execution.taskSupport` が1つも変わっていないことを
確認する(「additions only」の直接の証拠)。
"""

from __future__ import annotations

import json
from pathlib import Path

from abist_kb.presentation.mcp import jobs_tools
from abist_kb.presentation.mcp.kb_admin import list_tools as kb_admin_list_tools
from abist_kb.presentation.mcp.kb_download import list_tools as kb_download_list_tools
from abist_kb.presentation.mcp.kb_search import list_tools as kb_search_list_tools
from abist_kb.presentation.mcp.kb_video import list_tools as kb_video_list_tools

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "mcp"

_NEW_TOOL_NAMES = {
    "start_run_batch",
    "start_download_esa_post",
    "start_download_esa_category",
    "start_download_esa_search",
    "start_download_web",
    "start_download_git",
    "start_render_scene",
    "job_status",
    "cancel_job",
    "get_batch",
    "list_corpora",
    "system_status",
} | {tool.name for tool in kb_admin_list_tools()}

#: 動画ツール（video Phase 10）の純増分。
#:
#: fixture(`tests/fixtures/mcp/tools-list.json`)は旧 Node 実装の実測キャプチャで、
#: SHA-256 が `tests/fixtures/capture-manifest.json` に固定されている。新機能の
#: ツールは fixture を書き換えるのではなく、ここへ**純増分として宣言**して許可する
#: (`test_contract_visualize.py::_NEW_VISUALIZE_TOOL_NAMES` と同じ方式)。
_NEW_VIDEO_TOOL_NAMES = {
    "plan_video",
    "create_video_project",
    "start_render_video",
    "video_status",
    "get_video",
    "list_videos",
    "run_video_qa",
    "approve_video",
    "set_distribution",
    "request_public_review",
    "list_capture_profiles",
}


def _expected_compat_tools() -> dict[str, dict]:
    tools_list_fixture = json.loads((FIXTURES_DIR / "tools-list.json").read_text(encoding="utf-8"))
    expected = {
        tool["name"]: tool for tool in tools_list_fixture["servers"]["kb-download"]["tools"]
    }
    expected.update(
        {tool["name"]: tool for tool in tools_list_fixture["servers"]["kb-search"]["tools"]}
    )
    add_web_batch_fixture = json.loads(
        (FIXTURES_DIR / "kb-download" / "add_web_batch" / "schema_only.json").read_text(
            encoding="utf-8"
        )
    )
    expected["add_web_batch"] = add_web_batch_fixture["tool_schema_from_tools_list"]
    return expected


def test_all_server_tools_list_is_additions_only() -> None:
    """kb-download 8 + kb-search 4 の既存12ツールはそのまま、jobs+kb-admin が純増する。"""
    expected = _expected_compat_tools()
    combined = [
        *kb_search_list_tools(),
        *kb_download_list_tools(),
        *jobs_tools.list_tools(),
        *kb_video_list_tools(),
        *kb_admin_list_tools(),
    ]
    actual = {tool.name: tool for tool in combined}

    assert len(combined) == len(actual), "ツール名の重複(既存/新規の衝突)がある"
    assert set(actual) == set(expected) | _NEW_TOOL_NAMES | _NEW_VIDEO_TOOL_NAMES

    # kb-download 8ツール(brief の主対象): 入力スキーマ・execution を fixture と
    # 一字一句突き合わせる(`test_contract_download.py` が既に担っているのと同じ
    # チェックを `all` 経由の関数でも再確認する)。
    kb_download_names = {tool.name for tool in kb_download_list_tools()}
    for name in kb_download_names:
        expected_tool = expected[name]
        actual_tool = actual[name]
        assert actual_tool.inputSchema == expected_tool["inputSchema"], name
        expected_task_support = expected_tool.get("execution", {}).get("taskSupport")
        actual_task_support = actual_tool.execution.taskSupport if actual_tool.execution else None
        assert actual_task_support == expected_task_support, name

    # kb-search 4ツール: `all` に組み込んでも `kb_search.list_tools()` 単体の
    # 出力とバイト同一であること(=結合そのものが何も変えていないこと)を確認し、
    # かつ fixture(tools-list.json)とも一字一句突き合わせる(kb-download 側と
    # 同じ水準。以前は全角/半角括弧の description drift を素通りさせていたが、
    # `test_contract_search.py::test_tools_list_matches_fixture_descriptions_and_schemas`
    # で drift を検出・修正済みなので、ここでも fixture 一致を要求してよい)。
    kb_search_standalone = {tool.name: tool for tool in kb_search_list_tools()}
    for name, standalone_tool in kb_search_standalone.items():
        assert actual[name].inputSchema == standalone_tool.inputSchema, name
        assert actual[name].description == standalone_tool.description, name
        expected_tool = expected[name]
        assert actual[name].description == expected_tool["description"], name
        assert actual[name].inputSchema == expected_tool["inputSchema"], name
        expected_task_support = expected_tool.get("execution", {}).get("taskSupport")
        actual_task_support = actual[name].execution.taskSupport if actual[name].execution else None
        assert actual_task_support == expected_task_support, name

    # 新規ツールは compat ツールと一切名前が衝突していない(diff の主眼)。
    assert _NEW_TOOL_NAMES.isdisjoint(expected)
    assert _NEW_VIDEO_TOOL_NAMES.isdisjoint(expected)
    assert _NEW_VIDEO_TOOL_NAMES.isdisjoint(_NEW_TOOL_NAMES)


def test_video_tools_are_declared_exactly() -> None:
    """`kb_video.list_tools()` と allowlist の宣言が食い違わないこと。

    ツールを足したのに allowlist へ書き忘れる（あるいはその逆）と、
    「fixture を触らずに純増させた」という主張が実態と合わなくなる。
    """
    assert {tool.name for tool in kb_video_list_tools()} == _NEW_VIDEO_TOOL_NAMES


def test_video_tools_do_not_accept_raw_launch_commands() -> None:
    """MCP から画面キャプチャの起動コマンドを渡せないこと。

    受け付けてよいのは登録済みの `capture_profile`（名前）だけ。inputSchema に
    `command` / `launch` / `repo` / `url` が現れたら、その時点で任意コード実行の
    入口になる。
    """
    forbidden = {"command", "launch", "repo", "url", "args", "shell"}
    for tool in kb_video_list_tools():
        properties = set((tool.inputSchema or {}).get("properties") or {})
        assert not (properties & forbidden), (
            f"{tool.name} が起動コマンド相当の引数を受け付けている: {properties & forbidden}"
        )
