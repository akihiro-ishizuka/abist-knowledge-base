"""kb-download MCP 契約テスト(M5 task-3a: 契約駆動の半分のみ)。

`tests/fixtures/mcp/kb-download/**/*.json` を実測ゴールデンとして読み、
`KbDownloadTools` の応答が同じ形(キー集合・型・エラー種別/`-32602`)を
持つことを検証する。このタスクの範囲は fixture がビット契約を固定している
ケースだけ(`list_batches` 正常系・`run_batch` 未知バッチ名エラー・
`download_esa_*`/`download_git` のスキーマ検証エラー・`download_web` の
URL 形式エラー・8ツール分の `tools/list`)であり、実処理(esa API 呼び出し・
`git clone`・Web クロール・`add_web_batch` の実書き込み)は対象外
(`kb_download.py` モジュール docstring 参照)。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from replay import check_result, load_fixture

from abist_kb.application.batch_service import BatchService
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.presentation.mcp.kb_download import KbDownloadTools, list_tools

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "kb-download"


@dataclasses.dataclass(frozen=True, slots=True)
class Env:
    tools: KbDownloadTools
    known_batch_names: list[str]


@pytest.fixture
def env(tmp_root: Path) -> Iterator[Env]:
    app_db_path = tmp_root / "app.sqlite"
    conn = connect(app_db_path)
    ensure_app_schema(conn)
    service = BatchService(conn)

    # アルファベット順で esa バッチが先頭に来るよう命名する
    # (`BatchRepository.list()` は name 昇順、fixture の先頭要素は esa 型なので
    # 構造比較 `_structural_mismatch` がリストの先頭要素だけを見る性質を利用する)。
    service.add(
        name="aaa_esa_batch",
        type="esa",
        output_dir="docs/aaa_esa_batch",
        items=[{"target": "設計効率化/テスト"}, {"target": "議事録/テスト"}],
    )
    service.add(
        name="bbb_web_batch",
        type="web",
        output_dir="docs/bbb_web_batch",
        items=[
            {
                "options": {
                    "url": "https://example.com/",
                    "output_dir": "docs/bbb_web_batch",
                    "max_depth": 3,
                    "delay": 1000,
                }
            }
        ],
    )
    service.add(
        name="ccc_git_batch",
        type="git",
        output_dir="docs/ccc_git_batch",
        items=[
            {
                "options": {
                    "repository": "https://github.com/example/repo",
                    "branch": "main",
                    "output_dir": "docs/ccc_git_batch",
                }
            }
        ],
    )

    tools = KbDownloadTools(conn)
    yield Env(tools=tools, known_batch_names=["aaa_esa_batch", "bbb_web_batch", "ccc_git_batch"])
    conn.close()


def _fixture_files(subdir: str) -> list[Path]:
    return sorted((FIXTURES_DIR / subdir).glob("*.json"))


# ---------------------------------------------------------------------------
# list_batches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_path", _fixture_files("list_batches"), ids=lambda p: p.stem)
def test_list_batches_matches_fixture_shape(env: Env, fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    result = env.tools.list_batches({})
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_list_batches_camelizes_each_batch_type(env: Env) -> None:
    """3つの型(esa/web/git)それぞれで期待する camelCase キーが出ること。"""
    payload = json.loads(env.tools.list_batches({}).content[0].text)
    by_name = {b["name"]: b for b in payload["batches"]}
    assert by_name["aaa_esa_batch"]["categories"] == ["設計効率化/テスト", "議事録/テスト"]
    assert by_name["bbb_web_batch"]["url"] == "https://example.com/"
    assert by_name["bbb_web_batch"]["maxDepth"] == 3
    assert by_name["ccc_git_batch"]["repository"] == "https://github.com/example/repo"
    assert by_name["ccc_git_batch"]["branch"] == "main"


# ---------------------------------------------------------------------------
# run_batch(未知バッチ名エラーのみ本タスクの範囲)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_path", _fixture_files("run_batch"), ids=lambda p: p.stem)
def test_run_batch_unknown_name_matches_fixture_shape(env: Env, fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    result = env.tools.run_batch(fixture.arguments)
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_run_batch_known_name_no_longer_raises_not_implemented(env: Env) -> None:
    """M5 task-3b: 実バッチ実行が配線された(esa は認証情報が無く FAILURE 応答になる)。"""
    result = env.tools.run_batch({"batch": "aaa_esa_batch"})
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert payload["batchType"] == "esa"
    assert payload["error"]


# ---------------------------------------------------------------------------
# download_esa_post / download_esa_category / download_esa_search / download_git
# (すべてスキーマ検証エラーのみが fixture の対象 — sdk_validation_error)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    ["download_esa_post", "download_esa_category", "download_esa_search", "download_git"],
)
def test_schema_validation_error_matches_fixture_shape(tool_name: str) -> None:
    from abist_kb.presentation.mcp.kb_download import validate_arguments

    fixture_files = _fixture_files(tool_name)
    assert fixture_files, f"{tool_name} の fixture が見つかりません"
    for fixture_path in fixture_files:
        fixture = load_fixture(fixture_path)
        result = validate_arguments(fixture.tool, fixture.arguments)
        assert result is not None, "スキーマ検証で弾かれるはずが None(妥当)扱いになった"
        outcome = check_result(fixture, result)
        assert outcome.ok, outcome.reason


def test_download_esa_post_handler_reached_fails_without_credentials(tmp_root: Path) -> None:
    """スキーマを通過した(=ハンドラに到達する)場合、esa 認証情報が無ければ失敗応答になる。"""
    app_db_path = tmp_root / "app2.sqlite"
    conn = connect(app_db_path)
    ensure_app_schema(conn)
    tools = KbDownloadTools(conn, root_dir=tmp_root)
    result = tools.download_esa_post({"post": 123})
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert result.isError
    conn.close()


# ---------------------------------------------------------------------------
# download_web(URL 形式エラーのみ fixture の対象 — tool_result_json)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_path", _fixture_files("download_web"), ids=lambda p: p.stem)
def test_download_web_invalid_url_matches_fixture_shape(env: Env, fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    result = env.tools.download_web(fixture.arguments)
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_download_web_valid_url_no_longer_raises_not_implemented(tmp_root: Path) -> None:
    """M5 task-3b: 実クロールが配線された(到達不能ホストなので error アクションで失敗)。"""
    app_db_path = tmp_root / "app3.sqlite"
    conn = connect(app_db_path)
    ensure_app_schema(conn)
    tools = KbDownloadTools(conn, root_dir=tmp_root)
    result = tools.download_web({"url": "http://127.0.0.1:1/unreachable"})
    payload = json.loads(result.content[0].text)
    assert payload["batchType"] == "web"
    assert "sync" in payload
    conn.close()


# ---------------------------------------------------------------------------
# add_web_batch(schema-only fixture — tools/list のみが対象)
# ---------------------------------------------------------------------------


def test_add_web_batch_creates_new_batch(env: Env) -> None:
    """M5 task-3b: add_web_batch は新しい batches 行を作成し、list_batches に反映される。"""
    result = env.tools.add_web_batch({"name": "新規webバッチ", "url": "https://example.com/"})
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["batch"]["name"] == "新規webバッチ"
    assert payload["batch"]["url"] == "https://example.com/"

    listed = json.loads(env.tools.list_batches({}).content[0].text)
    names = {b["name"] for b in listed["batches"]}
    assert "新規webバッチ" in names


# ---------------------------------------------------------------------------
# tools/list(8ツール全部、schema_only fixture の add_web_batch を含む)
# ---------------------------------------------------------------------------


def test_tools_list_matches_fixture_schemas() -> None:
    import json as _json

    tools_list_fixture = _json.loads(
        (FIXTURES_DIR.parent / "tools-list.json").read_text(encoding="utf-8")
    )
    expected_tools = {
        tool["name"]: tool for tool in tools_list_fixture["servers"]["kb-download"]["tools"]
    }
    add_web_batch_fixture = _json.loads(
        (FIXTURES_DIR / "add_web_batch" / "schema_only.json").read_text(encoding="utf-8")
    )
    expected_tools["add_web_batch"] = add_web_batch_fixture["tool_schema_from_tools_list"]

    actual_tools = {tool.name: tool for tool in list_tools()}

    assert set(actual_tools) == set(expected_tools)
    for name, expected in expected_tools.items():
        actual = actual_tools[name]
        assert actual.description == expected["description"], name
        assert actual.inputSchema == expected["inputSchema"], name
        expected_task_support = expected.get("execution", {}).get("taskSupport")
        actual_task_support = actual.execution.taskSupport if actual.execution else None
        assert actual_task_support == expected_task_support, name
