"""kb-visualize MCP サーバーの3ツール(M7 task-3/4)。

`domain.scene_spec`/`application.visualization.*` をそのまま呼び、fixture
(`tests/fixtures/mcp/kb-visualize/**/*.json`)と同じ形の応答を組み立てる。

`render_scene` は `render` リソースリース(設計書 §10.2)を `wait=False` で
取得する — 旧実装のプロセス内 1 本制限(`CONCURRENT_RENDER`)と同じ即時busy
意味論を、SQLite ベースのリースへ置き換えたもの(全プロセス横断で効く)。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import mcp.types as types

from abist_kb.application.visualization.renderer import render_scene as run_render_scene
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import ResourceKind
from abist_kb.domain.scene_spec import RESERVED_KINDS
from abist_kb.infrastructure.jobs.leases import acquire_resource_lease
from abist_kb.infrastructure.visualization.manim_runner import (
    check_visualize_deps as run_check_visualize_deps,
)
from abist_kb.infrastructure.visualization.manim_runner import default_timeout_seconds
from abist_kb.presentation.mcp.payloads import ok_result, tool_result

#: 予約済み(未実装)kind の案内文。`RESERVED_KINDS` から自動生成するので、
#: kind を実装して `SCENE_KINDS` へ移すたびに文言が自動で追随する。
#: 全て実装し終えたら空文字になる。
_RESERVED_NOTE = f"予約済み（未実装）: {' / '.join(RESERVED_KINDS)}。" if RESERVED_KINDS else ""

#: `list_scene_kinds`/`check_visualize_deps`/`render_scene` の docstring は
#: `tools/list` fixture(`tests/fixtures/mcp/tools-list.json`)から一字一句転記する。
#: 例外は `list_scene_kinds` の予約 kind の一文で、旧実装に無い kind を実装した
#: ことによる意図的な逸脱。fixture は手編集できないため、
#: `tests/mcp/test_contract_visualize.py` の
#: `_INTENTIONAL_DESCRIPTION_DIVERGENCE` に理由付きで登録して許可する。
TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_scene_kinds": (
        "利用可能なシーン種別（テンプレート・必須フィールド・beat 種別）を JSON で返す。"
        "render_scene の前に必ず呼び、SceneSpec の組み立てに使うこと。" + _RESERVED_NOTE
    ),
    "check_visualize_deps": (
        "Python / Manim / ffmpeg / 日本語フォントの有無とバージョンを診断する。初回利用時や "
        "PYTHON_NOT_FOUND / MANIM_NOT_FOUND 等のエラー時に呼ぶ。依存が欠けていても ok:true"
        "（診断自体は成功）。描画可否は ready で判定する。"
    ),
    "render_scene": (
        "SceneSpec を検証し、テンプレート Scene のみで Manim レンダリングして "
        "reports/visualizations/<id>/ に output.mp4|png・scene-spec.json・scene.py・"
        "manifest.json を保存する。各 beat は sources[].id への source_refs を持つこと"
        "（出典のない数値は描画されない）。sources[].content_hash には kb-search "
        "get_document の range_hash を使う。レンダリングは数分かかることがある"
        "（最長10分ブロック。KB_VISUALIZE_TIMEOUT_MS で変更可）。失敗時は code"
        "（INVALID_SCENE_SPEC / SOURCE_NOT_FOUND / SOURCE_HASH_MISMATCH / PYTHON_NOT_FOUND / "
        "MANIM_NOT_FOUND / RENDER_TIMEOUT / RENDER_FAILED / OUTPUT_NOT_FOUND / "
        "OUTPUT_PATH_VIOLATION / CONCURRENT_RENDER）で分岐すること。"
    ),
}


def list_tools() -> list[types.Tool]:
    """`tools/list` に返す3ツールのスキーマ(`tests/fixtures/mcp/tools-list.json` 準拠)。"""
    forbidden = types.ToolExecution(taskSupport="forbidden")
    return [
        types.Tool(
            name="list_scene_kinds",
            description=TOOL_DESCRIPTIONS["list_scene_kinds"],
            inputSchema={"properties": {}, "type": "object"},
            execution=forbidden,
        ),
        types.Tool(
            name="check_visualize_deps",
            description=TOOL_DESCRIPTIONS["check_visualize_deps"],
            inputSchema={"properties": {}, "type": "object"},
            execution=forbidden,
        ),
        types.Tool(
            name="render_scene",
            description=TOOL_DESCRIPTIONS["render_scene"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "sceneSpec": {
                        "anyOf": [
                            {"additionalProperties": {}, "type": "object"},
                            {"type": "string"},
                        ],
                        "description": (
                            "SceneSpec（JSON オブジェクト推奨。JSON 文字列も可）。"
                            "list_scene_kinds のテンプレートに従うこと"
                        ),
                    }
                },
                "required": ["sceneSpec"],
                "type": "object",
            },
            execution=forbidden,
        ),
    ]


#: `name` -> `inputSchema` の対応表。手動スキーマ検証に使う。
_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {tool.name: tool.inputSchema for tool in list_tools()}


def validate_arguments(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult | None:
    """`tool_name` の `inputSchema` に対し `arguments` を検証する(kb-download と同じ規約)。"""
    schema = _INPUT_SCHEMAS.get(tool_name)
    if schema is None:
        return None
    try:
        jsonschema.validate(instance=arguments, schema=schema)
    except jsonschema.ValidationError as exc:
        text = (
            f"MCP error -32602: Input validation error: "
            f"Invalid arguments for tool {tool_name}: {exc.message}"
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], isError=True
        )
    return None


class KbVisualizeTools:
    """kb-visualize の3ツールを実装するアダプタ。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        docs_dir: Path,
        reports_dir: Path,
        repo_root: Path,
        lease_ttl_seconds: float | None = None,
        check_deps_fn: Any = run_check_visualize_deps,
        render_scene_fn: Any = run_render_scene,
    ) -> None:
        self._conn = conn
        self._docs_dir = docs_dir
        self._reports_dir = reports_dir
        self._repo_root = repo_root
        self._lease_ttl_seconds = lease_ttl_seconds
        self._check_deps_fn = check_deps_fn
        self._render_scene_fn = render_scene_fn

    # -- list_scene_kinds -----------------------------------------------------

    def list_scene_kinds(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        from abist_kb.domain.scene_spec import SCENE_KINDS

        payload = {
            "ok": True,
            "count": len(SCENE_KINDS),
            "scene_kinds": [
                {
                    "kind": info["kind"],
                    "description": info["description"],
                    "template": info["template"],
                    "required": info["required"],
                    "beat_types": info["beat_types"],
                }
                for info in SCENE_KINDS
            ],
        }
        return ok_result(payload)

    # -- check_visualize_deps ---------------------------------------------------

    def check_visualize_deps(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        payload = self._check_deps_fn(root=self._repo_root)
        return ok_result(payload)

    # -- render_scene -----------------------------------------------------------

    def _parse_scene_spec(
        self, raw: Any
    ) -> tuple[dict[str, Any] | None, types.CallToolResult | None]:
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                return None, tool_result(
                    {
                        "ok": False,
                        "code": "INVALID_SCENE_SPEC",
                        "errors": [
                            {
                                "path": "",
                                "code": "invalid",
                                "message": f"sceneSpec の JSON パースに失敗しました: {exc}",
                            }
                        ],
                        "warnings": [],
                    },
                    is_error=True,
                )
            if not isinstance(parsed, dict):
                return None, tool_result(
                    {
                        "ok": False,
                        "code": "INVALID_SCENE_SPEC",
                        "errors": [
                            {
                                "path": "",
                                "code": "invalid",
                                "message": "SceneSpec は JSON オブジェクトで指定してください",
                            }
                        ],
                        "warnings": [],
                    },
                    is_error=True,
                )
            return parsed, None
        if isinstance(raw, dict):
            return raw, None
        return None, tool_result(
            {
                "ok": False,
                "code": "INVALID_SCENE_SPEC",
                "errors": [
                    {
                        "path": "",
                        "code": "invalid",
                        "message": "SceneSpec は JSON オブジェクトで指定してください",
                    }
                ],
                "warnings": [],
            },
            is_error=True,
        )

    def render_scene(self, arguments: dict[str, Any]) -> types.CallToolResult:
        spec, parse_error = self._parse_scene_spec(arguments.get("sceneSpec"))
        if parse_error is not None:
            return parse_error
        assert spec is not None

        timeout_seconds = default_timeout_seconds()
        ttl_seconds = self._lease_ttl_seconds or (timeout_seconds + 60.0)
        owner_id = str(uuid.uuid4())

        try:
            with acquire_resource_lease(
                self._conn,
                ResourceKind.RENDER,
                owner_id=owner_id,
                ttl_seconds=ttl_seconds,
                wait=False,
            ):
                outcome = self._render_scene_fn(
                    spec,
                    docs_dir=self._docs_dir,
                    reports_dir=self._reports_dir,
                    repo_root=self._repo_root,
                    timeout_seconds=timeout_seconds,
                )
        except AppError as exc:
            if exc.code is ErrorCode.CONFLICT:
                return tool_result(
                    {
                        "ok": False,
                        "code": "CONCURRENT_RENDER",
                        "errors": [
                            {
                                "path": "",
                                "code": "busy",
                                "message": (
                                    "別のレンダリングが実行中です。完了を待って再試行してください"
                                ),
                            }
                        ],
                        "warnings": [],
                    },
                    is_error=True,
                )
            raise

        if not outcome.ok:
            payload: dict[str, Any] = {
                "ok": False,
                "code": outcome.code,
                "errors": outcome.errors,
                "warnings": outcome.warnings,
            }
            return tool_result(payload, is_error=True)

        payload = {
            "ok": True,
            "visualization_id": outcome.visualization_id,
            "output_dir": str(outcome.output_dir),
            "outputs": outcome.outputs,
            "manifest_path": str(outcome.manifest_path),
            "warnings": outcome.warnings,
            "duration_ms": outcome.duration_ms,
        }
        return ok_result(payload)


__all__ = ["KbVisualizeTools", "list_tools", "validate_arguments"]
