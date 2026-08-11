"""kb-visualize MCP 契約テスト(M7 task-4)。

`tests/fixtures/mcp/kb-visualize/**/*.json` を実測ゴールデンとして読み、
`KbVisualizeTools` の応答が同じ形(キー集合・型・エラー種別)を持つことを
検証する。`render_scene` の fixture は検証エラーのみ(`INVALID_SCENE_SPEC`/
`SOURCE_HASH_MISMATCH`)であり、実レンダリングは対象外
(`tests/visualize/test_real_render.py` が別途 `KB_RUN_MANIM_TESTS=1` で検証する)。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from replay import check_result, load_fixture

from abist_kb.domain.line_range import range_hash
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.presentation.mcp.kb_visualize import KbVisualizeTools, list_tools

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "kb-visualize"
TOOLS_LIST_FIXTURE = FIXTURES_DIR.parent / "tools-list.json"

_DOC_TEXT = "行1\n行2\n行3\n行4\n行5\n"


@dataclasses.dataclass(frozen=True, slots=True)
class Env:
    tools: KbVisualizeTools
    docs_dir: Path
    valid_hash: str
    conn: sqlite3.Connection


def _fake_check_deps(*, root):
    return {
        "ok": True,
        "ready": True,
        "python": {"found": True, "version": "3.11.9", "path": "fake-python"},
        "manim": {"found": True, "version": "0.19.0"},
        "ffmpeg": {"found": True, "version": "ffmpeg version 8.0.1", "path": "fake-ffmpeg"},
        "fonts": {"requested": "Yu Gothic UI", "available": True},
        "messages": [],
    }


@pytest.fixture
def env(tmp_root: Path) -> Iterator[Env]:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir()
    (docs_dir / "doc.md").write_text(_DOC_TEXT, encoding="utf-8")

    app_db_path = tmp_root / "app.sqlite"
    conn = connect(app_db_path)
    ensure_app_schema(conn)

    hashed = range_hash(_DOC_TEXT, 1, 2)
    assert hashed.ok and hashed.hash is not None

    tools = KbVisualizeTools(
        conn,
        docs_dir=docs_dir,
        reports_dir=tmp_root / "reports" / "visualizations",
        repo_root=tmp_root,
        check_deps_fn=_fake_check_deps,
    )
    yield Env(tools=tools, docs_dir=docs_dir, valid_hash=hashed.hash, conn=conn)
    conn.close()


def _fixture_files(subdir: str) -> list[Path]:
    return sorted((FIXTURES_DIR / subdir).glob("*.json"))


@pytest.mark.parametrize("fixture_path", _fixture_files("list_scene_kinds"), ids=lambda p: p.stem)
def test_list_scene_kinds_matches_fixture_shape(env: Env, fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    result = env.tools.list_scene_kinds({})
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


@pytest.mark.parametrize(
    "fixture_path", _fixture_files("check_visualize_deps"), ids=lambda p: p.stem
)
def test_check_visualize_deps_matches_fixture_shape(env: Env, fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    result = env.tools.check_visualize_deps({})
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_render_scene_invalid_scene_spec_matches_fixture_shape(env: Env) -> None:
    fixture = load_fixture(FIXTURES_DIR / "render_scene" / "invalid_scene_spec.json")
    result = env.tools.render_scene(fixture.arguments)
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


def test_render_scene_source_hash_mismatch_matches_fixture_shape(env: Env) -> None:
    scene_spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "mp4",
        "template": "step_explanation",
        "title": "テスト用シーン",
        "sources": [
            {
                "id": "s1",
                "path": "doc.md",
                "start_line": 1,
                "end_line": 2,
                "content_hash": "f" * 64,
            }
        ],
        "beats": [{"type": "metric", "label": "テスト指標", "value": "1", "source_refs": ["s1"]}],
    }
    fixture = load_fixture(FIXTURES_DIR / "render_scene" / "source_hash_mismatch.json")
    result = env.tools.render_scene({"sceneSpec": scene_spec})
    outcome = check_result(fixture, result)
    assert outcome.ok, outcome.reason


# `render_scene` の検証エラー経路(INVALID_SCENE_SPEC/SOURCE_HASH_MISMATCH)が
# Python サブプロセスに一切到達しないことは `tests/visualize/test_renderer.py`
# (`resolve_python_fn`/`run_process` を DI で差し替えて未呼び出しを検証)が担う。


# ---------------------------------------------------------------------------
# tools/list: description/schema の一字一句一致を要求する
# (`test_contract_search.py`/`test_contract_download.py` と同じ水準)。
# ---------------------------------------------------------------------------


#: 旧 Node 実装に無く、このリポジトリで追加した kb-visualize のツール名。
#:
#: `tests/fixtures/mcp/tools-list.json` は旧実装からの実測キャプチャで、SHA-256 が
#: `tests/fixtures/capture-manifest.json` に固定されている(`tests/fixtures_check/
#: test_capture_manifest.py` が照合)。`tests/fixtures/PROVENANCE.md` も fixture の
#: 手編集を禁じている。したがって新ツールは fixture を書き換えるのではなく、
#: `tests/mcp/test_all_server_tools_list_diff.py` の `_NEW_TOOL_NAMES` と同じ
#: 「純増分の明示宣言」で表現する。
_NEW_VISUALIZE_TOOL_NAMES: set[str] = set()

#: 旧実装に無い機能を追加したことに伴う、fixture の description からの意図的な逸脱。
#: fixture は手編集できないので、「fixture と違ってよい理由」を1件ずつ人間が
#: 書き下すことで許可する(`replay.py` の `_STATIC_VALUE_FIELDS` と同じ発想の逆向き)。
#: キーはツール名、値は現行の期待 description。
_INTENTIONAL_DESCRIPTION_DIVERGENCE: dict[str, str] = {
    # 予約 kind(timeline / comparison / domain)をすべて実装したため、
    # 「予約済み（未実装）: ...」の一文が消えた。文言は RESERVED_KINDS から
    # 自動生成される(kb_visualize._RESERVED_NOTE)ので、将来 kind を予約すれば
    # 自動的に復活する。
    "list_scene_kinds": (
        "利用可能なシーン種別（テンプレート・必須フィールド・beat 種別）を JSON で返す。"
        "render_scene の前に必ず呼び、SceneSpec の組み立てに使うこと。"
    ),
}


def test_tools_list_matches_fixture_descriptions_and_schemas() -> None:
    """旧3ツールの契約は不変。新ツールは allowlist で純増分として許可する。

    fixture の `count` や配列要素が増えても replay が壊れないのは、
    `tests/mcp/replay.py::_structural_mismatch` がキー集合と型だけを比較し、
    値を見ず、list は先頭要素だけを見るため(同ファイルの docstring 参照)。
    したがって `list_scene_kinds` の応答に scene_kind を足しても
    fixture の更新は不要。**反射的に fixture を書き換えないこと。**
    """
    tools_list_fixture = json.loads(TOOLS_LIST_FIXTURE.read_text(encoding="utf-8"))
    expected_tools = {
        tool["name"]: tool for tool in tools_list_fixture["servers"]["kb-visualize"]["tools"]
    }
    actual_tools = {tool.name: tool for tool in list_tools()}

    assert set(actual_tools) == set(expected_tools) | _NEW_VISUALIZE_TOOL_NAMES
    # allowlist が「純増分」であること(既存ツール名を紛れ込ませていないこと)。
    assert _NEW_VISUALIZE_TOOL_NAMES.isdisjoint(expected_tools)
    for name, expected in expected_tools.items():
        actual = actual_tools[name]
        want_description = _INTENTIONAL_DESCRIPTION_DIVERGENCE.get(name, expected["description"])
        assert actual.description == want_description, name
        assert actual.inputSchema == expected["inputSchema"], name
        expected_task_support = expected.get("execution", {}).get("taskSupport")
        actual_task_support = actual.execution.taskSupport if actual.execution else None
        assert actual_task_support == expected_task_support, name
