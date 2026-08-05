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
        *kb_admin_list_tools(),
    ]
    actual = {tool.name: tool for tool in combined}

    assert len(combined) == len(actual), "ツール名の重複(既存/新規の衝突)がある"
    assert set(actual) == set(expected) | _NEW_TOOL_NAMES

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
