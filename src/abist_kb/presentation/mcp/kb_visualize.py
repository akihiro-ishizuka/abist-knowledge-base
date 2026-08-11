"""kb-visualize MCP サーバーの3ツール(M7 task-3/4)。

`domain.scene_spec`/`application.visualization.*` をそのまま呼び、fixture
(`tests/fixtures/mcp/kb-visualize/**/*.json`)と同じ形の応答を組み立てる。

`render_scene` は `render` リソースリース(設計書 §10.2)を `wait=False` で
取得する — 旧実装のプロセス内 1 本制限(`CONCURRENT_RENDER`)と同じ即時busy
意味論を、SQLite ベースのリースへ置き換えたもの(全プロセス横断で効く)。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import jsonschema
import mcp.types as types

from abist_kb.application.visualization import catalog
from abist_kb.application.visualization.renderer import render_scene as run_render_scene
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.job import ResourceKind
from abist_kb.domain.scene_spec import RESERVED_KINDS
from abist_kb.infrastructure.db.visualizations_repo import VisualizationRepository
from abist_kb.infrastructure.jobs.execution import held_resource_lease
from abist_kb.infrastructure.visualization.artifact_store import sha256_file
from abist_kb.infrastructure.visualization.manim_runner import (
    check_visualize_deps as run_check_visualize_deps,
)
from abist_kb.infrastructure.visualization.manim_runner import (
    default_lease_ttl_seconds,
    default_timeout_seconds,
)
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
    "list_visualizations": (
        "過去のレンダリング成果物を新しい順に一覧する。render_scene の応答が"
        "クライアント側タイムアウトで切れた場合の確認や、過去成果物の再利用に使う。"
        "state / scene_kind / output_format / query / source_path で絞り込める。"
        "全文（SceneSpec・出典・stderr）は get_visualization で取得すること。"
    ),
    "get_visualization": (
        "visualization_id の詳細（状態・出典検証結果・成果物パス・manifest）を返す。"
        "manifest.json がディスク上で変化していれば manifest_drift: true を返す。"
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
        types.Tool(
            name="list_visualizations",
            description=TOOL_DESCRIPTIONS["list_visualizations"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "offset": {"type": "integer", "minimum": 0},
                    "state": {"type": "string", "enum": ["succeeded", "failed"]},
                    "scene_kind": {"type": "string"},
                    "output_format": {"type": "string", "enum": ["mp4", "png"]},
                    "query": {"type": "string"},
                    "source_path": {"type": "string"},
                },
                "type": "object",
            },
            execution=forbidden,
        ),
        types.Tool(
            name="get_visualization",
            description=TOOL_DESCRIPTIONS["get_visualization"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "visualization_id": {"type": "string", "minLength": 1},
                    "include_manifest": {"type": "boolean"},
                    "include_spec": {"type": "boolean"},
                },
                "required": ["visualization_id"],
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

    # -- カタログ ----------------------------------------------------------------

    def _record_catalog(self, outcome: Any) -> None:
        """成果物をカタログへ記録する（失敗しても描画結果は返す）。

        カタログは索引であって正本ではないので、ここで落ちてもレンダリングの
        成否には影響させない。取りこぼしは次の `list_visualizations` の
        自己修復（ディスクとの差分検出）で回復する。
        """
        with contextlib.suppress(sqlite3.Error):
            catalog.record_render(self._conn, outcome, root_dir=self._repo_root)

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
        ttl_seconds = self._lease_ttl_seconds or default_lease_ttl_seconds()
        owner_id = str(uuid.uuid4())
        outcome = None

        try:
            # 保持中は TTL の 1/3 ごとに自動更新される。単発取得のままでは
            # TTL を短くできず(保持中に横取りされて Manim が2本走る)、
            # 異常終了時に次のレンダリングが TTL 分ブロックされていた。
            with held_resource_lease(
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
            if exc.code is not ErrorCode.CONFLICT:
                raise
            if outcome is None:
                # リースを取得できなかった = 本来の busy。
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
            # 描画は完了したがその後リースを喪失した。成果物は既に書かれているので
            # 捨てずに返す(捨てると「レンダリングは成功したのに busy と報告する」
            # という最悪の挙動になる)。
            outcome = replace(
                outcome,
                warnings=[
                    *outcome.warnings,
                    "レンダリング中に render リースを喪失しました（成果物は生成済み）。",
                ],
            )

        self._record_catalog(outcome)

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

    # -- list_visualizations ------------------------------------------------------

    def _visualizations_dir(self) -> Path:
        # KbVisualizeTools は解決済みの <reports>/visualizations を受け取る。
        return self._reports_dir

    def _self_heal(self) -> list[str]:
        """ディスクと DB の差分があるときだけ整合を取る。

        クライアント側タイムアウトで応答が切れ、レンダリングは完走したのに
        DB 行だけ書かれなかったケースを、次の一覧参照で自動回復させる。
        以前はこれが無いため SKILL.md に「reports/ を目で確認する」という
        運用回避策が常駐していた。
        """
        repo = VisualizationRepository(self._conn)
        on_disk = catalog.disk_ids(self._visualizations_dir())
        if on_disk == repo.all_ids():
            return []
        if len(on_disk) > catalog.MAX_AUTO_RECONCILE_DIRS:
            return [
                f"成果物が {len(on_disk)} 件あるため自動整合をスキップしました"
                "（多すぎる場合は手動で整合してください）"
            ]
        catalog.reconcile_from_disk(
            self._conn, self._visualizations_dir(), root_dir=self._repo_root
        )
        return []

    def list_visualizations(self, arguments: dict[str, Any]) -> types.CallToolResult:
        warnings = self._self_heal()
        repo = VisualizationRepository(self._conn)
        rows, total = repo.list(
            state=arguments.get("state"),
            scene_kind=arguments.get("scene_kind"),
            output_format=arguments.get("output_format"),
            query=arguments.get("query"),
            source_path=arguments.get("source_path"),
            limit=int(arguments.get("limit") or 20),
            offset=int(arguments.get("offset") or 0),
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            source_count, bad_count = repo.source_counts(row["id"])
            output_abs = (
                self._repo_root / row["output_dir"] / row["output_path"]
                if row["output_dir"] and row["output_path"]
                else None
            )
            items.append(
                {
                    "visualization_id": row["id"],
                    "state": row["state"],
                    "code": row["code"],
                    "scene_kind": row["scene_kind"],
                    "template": row["template"],
                    "output_format": row["output_format"],
                    "title": row["title"],
                    "query": row["query"],
                    "created_at": row["created_at"],
                    "duration_ms": row["duration_ms"],
                    "output_dir": str(self._repo_root / row["output_dir"])
                    if row["output_dir"]
                    else None,
                    "output_path": str(output_abs) if output_abs else None,
                    "output_exists": bool(output_abs and output_abs.exists()),
                    "warnings_count": len(row["warnings"]),
                    "source_count": source_count,
                    "bad_source_count": bad_count,
                    "job_id": row["job_id"],
                }
            )
        return ok_result(
            {
                "ok": True,
                "count": len(items),
                "total": total,
                "visualizations": items,
                "warnings": warnings,
            }
        )

    # -- get_visualization --------------------------------------------------------

    def get_visualization(self, arguments: dict[str, Any]) -> types.CallToolResult:
        visualization_id = arguments["visualization_id"]
        repo = VisualizationRepository(self._conn)
        record = repo.get(visualization_id)
        if record is None:
            self._self_heal()
            record = repo.get(visualization_id)
        if record is None:
            return tool_result(
                {
                    "ok": False,
                    "code": "NOT_FOUND",
                    "errors": [
                        {
                            "path": "visualization_id",
                            "code": "not_found",
                            "message": f"可視化 {visualization_id} は見つかりません",
                        }
                    ],
                },
                is_error=True,
            )

        drifted, manifest = catalog.manifest_drifted(record, self._repo_root)
        out_dir = self._repo_root / record["output_dir"] if record["output_dir"] else None
        artifacts: dict[str, Any] = {
            "output_dir": str(out_dir) if out_dir else None,
            "manifest_path": str(self._repo_root / record["manifest_path"])
            if record["manifest_path"]
            else None,
            "outputs": [],
        }
        if out_dir and record["output_path"]:
            produced = out_dir / record["output_path"]
            exists = produced.exists()
            artifacts["outputs"] = [
                {
                    "path": record["output_path"],
                    "abs_path": str(produced),
                    "sha256": record["output_sha256"],
                    "size_bytes": record["output_size_bytes"],
                    "exists": exists,
                    "sha256_matches": bool(
                        exists
                        and record["output_sha256"]
                        and sha256_file(produced) == record["output_sha256"]
                    ),
                }
            ]

        payload: dict[str, Any] = {
            "ok": True,
            "visualization": record,
            "sources": repo.get_sources(visualization_id),
            "artifacts": artifacts,
            "manifest": manifest if arguments.get("include_manifest", True) else None,
            "manifest_drift": drifted,
            "spec": (manifest or {}).get("spec") if arguments.get("include_spec") else None,
        }
        return ok_result(payload)


__all__ = ["KbVisualizeTools", "list_tools", "validate_arguments"]
