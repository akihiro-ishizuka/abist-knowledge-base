"""ジョブ指向の新規 MCP ツール(M5 task-4: `start_*`/`job_status`/`cancel_job`/
`get_batch`/`list_corpora`/`system_status`)。

**旧 Node 実装に前例が無い**ため fixture は存在しない。応答の形は本タスクで
新規に設計し、既存15ツール(`kb_search.py`/`kb_download.py`)が確立した規約
(`{"ok": bool, ...}` の JSON 応答、`payloads.dumps_tool_json` と同じ整形、
エラーは `{"ok": false, "error": <文言>, "code": <ErrorCode>}`)をそのまま
踏襲する。`.superpowers/sdd/M5-compat-mcp/task-4-report.md` に応答形を
記録すること。

**`start_*` の設計**: 旧15ツールの `run_batch`/`download_esa_*`/`download_web`/
`download_git` はプロセス内でブロックして完了まで待つ(`kb_download.py`)。
本モジュールの `start_*` はその非同期版で、`JobService.detach()`
(`application/job_service.py`)へキュー投入するだけで即座に返す。
`JobService.detach()` は `leases.has_live_worker()` が偽なら
`AppError(code=WORKER_UNAVAILABLE)` を送出する(CLI `jobs submit --detach`
と同じ契約)ので、ここではその契約をそのまま再利用する — 生きた worker が
いないのに実行されないジョブをキューへ積まない。

`start_render_scene` は `render_scene` ジョブ種別(`application.visualization.
render_job`)へ投入する。他の `start_*` と同じく `detach()` の
`WORKER_UNAVAILABLE` 契約をそのまま再利用する(検証は投入時ではなくジョブ
ハンドラ内で行う — `render_scene()` が `INVALID_SCENE_SPEC`/
`SOURCE_HASH_MISMATCH` 等を判定する)。

job 種別文字列(`kind`)は `kb_download.py` のブロッキング実装が `run_inline`
に渡す `kind`(`kb_download_batch`/`kb_download_esa_post`/...)とそろえてある。
これは意図的な選択: 将来 M9 以降で `worker run` 側にこれらの `kind` の
`JobHandler` を登録すれば、ブロッキング版と非ブロッキング版が同じジョブ種別・
同じ `params` 形状を共有できる(クライアントが2つの語彙を覚えずに済む)。

**`job_status` の PARTIAL 契約**: M3 はジョブ実行層で部分失敗を `JobState.PARTIAL`
として区別するよう修正した(全消費者が成功と誤読しないように)。ここでは
`Job.state` を `str()` でそのまま返すだけに留め、`succeeded`/`partial` を
どちらも「成功」へ丸めるような bool 合成は行わない。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import mcp.types as types

from abist_kb.application.batch_service import BatchService
from abist_kb.application.index_service import CORPUS_LABELS, IndexService
from abist_kb.application.job_service import JobService
from abist_kb.application.visualization.render_job import RENDER_JOB_KIND
from abist_kb.domain.errors import AppError
from abist_kb.domain.job import Job
from abist_kb.domain.job import JobState as _JobState
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.sources.base import with_docs_prefix
from abist_kb.presentation.mcp.kb_download import _camelize_batch
from abist_kb.presentation.mcp.payloads import app_error_result, error_result, ok_result

TOOL_DESCRIPTIONS: dict[str, str] = {
    "start_run_batch": (
        "定義済みバッチの実行をキューに投入し、即座に job_id を返す(ブロックしない)。"
        "生きた worker(`worker run`)が居ない場合は WORKER_UNAVAILABLE で失敗する。"
        "進捗・結果は job_status で確認する。ブロックして結果を待ちたい場合は "
        "run_batch(同期版)を使うこと。"
    ),
    "start_download_esa_post": (
        "esa 記事1件のダウンロードをキューに投入する(非ブロック版)。同期版は download_esa_post。"
    ),
    "start_download_esa_category": (
        "esa カテゴリ配下の一括ダウンロードをキューに投入する(非ブロック版)。"
        "同期版は download_esa_category。"
    ),
    "start_download_esa_search": (
        "esa 検索ヒットの一括ダウンロードをキューに投入する(非ブロック版)。"
        "同期版は download_esa_search。"
    ),
    "start_download_web": ("Web クロールをキューに投入する(非ブロック版)。同期版は download_web。"),
    "start_download_git": (
        "Git リポジトリ同期をキューに投入する(非ブロック版)。同期版は download_git。"
    ),
    "start_render_scene": (
        "SceneSpec のレンダリングをキューに投入し、即座に job_id を返す(ブロックしない)。"
        "生きた worker(`worker run`)が居ない場合は WORKER_UNAVAILABLE で失敗する。"
        "検証(INVALID_SCENE_SPEC/SOURCE_HASH_MISMATCH 等)はジョブ実行時に行われる。"
        "進捗・結果は job_status で確認する。ブロックして結果を待ちたい場合は "
        "render_scene(同期版、kb-visualize)を使うこと。"
    ),
    "job_status": (
        "job_id の現在状態(queued/running/succeeded/partial/failed/cancelled/"
        "interrupted)・結果・エラーを返す。partial は「一部失敗を含む完了」であり "
        "succeeded とは区別する(部分失敗を成功と読み違えないこと)。"
    ),
    "cancel_job": (
        "job_id のキャンセルを要求する。queued は即座に cancelled、running は "
        "cancel_requested フラグを立てるのみ(強制終了はしない)。既に終了した"
        "ジョブへの要求はエラーになる。"
    ),
    "get_batch": "batch-config の1件を名前で取得する(list_batches の1件相当)。",
    "list_corpora": ("work/reference 各コーパスの索引可用性の概要を返す(index_status の要約版)。"),
    "system_status": (
        "worker の生存状況・状態別ジョブ件数・コーパス概要をまとめて返す(ヘルスチェック用)。"
    ),
}

_JOB_KIND_FOR_TOOL: dict[str, str] = {
    "start_run_batch": "kb_download_batch",
    "start_download_esa_post": "kb_download_esa_post",
    "start_download_esa_category": "kb_download_esa_category",
    "start_download_esa_search": "kb_download_esa_search",
    "start_download_web": "kb_download_web",
    "start_download_git": "kb_download_git",
    "start_render_scene": RENDER_JOB_KIND,
}


def list_tools() -> list[types.Tool]:
    """`tools/list` に返す新規12ツールのスキーマ(fixture 前例なし、本タスクで新規設計)。"""
    return [
        types.Tool(
            name="start_run_batch",
            description=TOOL_DESCRIPTIONS["start_run_batch"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "batch": {
                        "description": "batch-config のバッチ名",
                        "minLength": 1,
                        "type": "string",
                    }
                },
                "required": ["batch"],
                "type": "object",
            },
        ),
        types.Tool(
            name="start_download_esa_post",
            description=TOOL_DESCRIPTIONS["start_download_esa_post"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "outputDir": {"description": "出力先(既定 docs)", "type": "string"},
                    "post": {
                        "description": "esa 記事番号",
                        "exclusiveMinimum": 0,
                        "type": "integer",
                    },
                },
                "required": ["post"],
                "type": "object",
            },
        ),
        types.Tool(
            name="start_download_esa_category",
            description=TOOL_DESCRIPTIONS["start_download_esa_category"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "category": {
                        "description": "esa カテゴリパス",
                        "minLength": 1,
                        "type": "string",
                    },
                    "outputDir": {"description": "出力先(既定 docs)", "type": "string"},
                },
                "required": ["category"],
                "type": "object",
            },
        ),
        types.Tool(
            name="start_download_esa_search",
            description=TOOL_DESCRIPTIONS["start_download_esa_search"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "outputDir": {"description": "出力先(既定 docs)", "type": "string"},
                    "query": {
                        "description": "esa 検索クエリ",
                        "minLength": 1,
                        "type": "string",
                    },
                },
                "required": ["query"],
                "type": "object",
            },
        ),
        types.Tool(
            name="start_download_web",
            description=TOOL_DESCRIPTIONS["start_download_web"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "concurrency": {
                        "description": "同時リクエスト数(既定 5)",
                        "maximum": 10,
                        "minimum": 1,
                        "type": "integer",
                    },
                    "delay": {
                        "description": "リクエスト間隔 ms(既定 1000)",
                        "minimum": 0,
                        "type": "integer",
                    },
                    "maxDepth": {
                        "description": "クロール深度(既定 3)",
                        "maximum": 10,
                        "minimum": 1,
                        "type": "integer",
                    },
                    "outputDir": {"description": "出力先(既定 docs)", "type": "string"},
                    "url": {
                        "description": "開始 URL(http/https)",
                        "minLength": 1,
                        "type": "string",
                    },
                },
                "required": ["url"],
                "type": "object",
            },
        ),
        types.Tool(
            name="start_download_git",
            description=TOOL_DESCRIPTIONS["start_download_git"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "branch": {
                        "description": "ブランチ名(既定はデフォルトブランチ)",
                        "type": "string",
                    },
                    "outputDir": {
                        "description": "出力先(既定 docs/<リポジトリ名>)",
                        "type": "string",
                    },
                    "repository": {
                        "description": "リポジトリ URL",
                        "minLength": 1,
                        "type": "string",
                    },
                },
                "required": ["repository"],
                "type": "object",
            },
        ),
        types.Tool(
            name="start_render_scene",
            description=TOOL_DESCRIPTIONS["start_render_scene"],
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
                    },
                    "slug": {
                        "description": "出力ディレクトリ名の候補(省略可)。",
                        "type": "string",
                    },
                },
                "required": ["sceneSpec"],
                "type": "object",
            },
        ),
        types.Tool(
            name="job_status",
            description=TOOL_DESCRIPTIONS["job_status"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "job_id": {
                        "description": "start_* / jobs submit --detach が返した job_id",
                        "minLength": 1,
                        "type": "string",
                    }
                },
                "required": ["job_id"],
                "type": "object",
            },
        ),
        types.Tool(
            name="cancel_job",
            description=TOOL_DESCRIPTIONS["cancel_job"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "job_id": {
                        "description": "キャンセル対象の job_id",
                        "minLength": 1,
                        "type": "string",
                    }
                },
                "required": ["job_id"],
                "type": "object",
            },
        ),
        types.Tool(
            name="get_batch",
            description=TOOL_DESCRIPTIONS["get_batch"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "batch": {
                        "description": "batch-config のバッチ名",
                        "minLength": 1,
                        "type": "string",
                    }
                },
                "required": ["batch"],
                "type": "object",
            },
        ),
        types.Tool(
            name="list_corpora",
            description=TOOL_DESCRIPTIONS["list_corpora"],
            inputSchema={"properties": {}, "type": "object"},
        ),
        types.Tool(
            name="system_status",
            description=TOOL_DESCRIPTIONS["system_status"],
            inputSchema={"properties": {}, "type": "object"},
        ),
    ]


_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {tool.name: tool.inputSchema for tool in list_tools()}


def validate_arguments(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult | None:
    """`kb_download.validate_arguments` と同じ手動 JSON Schema 検証。"""
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
            content=[types.TextContent(type="text", text=text)],
            isError=True,
        )
    return None


def _app_error_result(exc: AppError) -> types.CallToolResult:
    return app_error_result(exc)


def _job_to_status_payload(job: Job) -> dict[str, Any]:
    return {
        "ok": True,
        "job_id": job.id,
        "kind": job.kind,
        "state": str(job.state),
        "params": job.params,
        "result": job.result,
        "error": job.error,
        "cancel_requested": job.cancel_requested,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _parse_scene_spec_argument(
    raw: Any,
) -> tuple[dict[str, Any] | None, types.CallToolResult | None]:
    """`sceneSpec` 引数(dict または JSON 文字列)をパースする(`kb_visualize.py`
    の `_parse_scene_spec` と同じ許容形。深い構造検証はジョブハンドラ側で行う
    ため、ここでは「JSON オブジェクトとして解釈できるか」だけを見る)。
    """
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, error_result(
                f"sceneSpec の JSON パースに失敗しました: {exc}",
                extra={"code": "INVALID_SCENE_SPEC"},
            )
        raw = parsed
    if not isinstance(raw, dict):
        return None, error_result(
            "SceneSpec は JSON オブジェクトで指定してください",
            extra={"code": "INVALID_SCENE_SPEC"},
        )
    return raw, None


def _corpus_summary(corpus: str, status: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": corpus,
        "label": status["label"],
        "available": status["available"],
        "documents": status["documents"],
        "chunks": status["chunks"],
        "embeddedChunks": status["embedded_chunks"],
        "lastIndexedAt": status["last_indexed_at"],
        "vectorSearchAvailable": status["vector_search_available"],
    }


class JobTools:
    """`start_*`/`job_status`/`cancel_job`/`get_batch`/`list_corpora`/`system_status` の実装。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        docs_dir: Path,
        app_db_path: Path,
        work_index_path: Path,
        reference_index_path: Path,
        reports_dir: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self._conn = conn
        self._docs_dir = docs_dir
        self._repo_root = repo_root if repo_root is not None else Path.cwd()
        self._reports_dir = (
            reports_dir if reports_dir is not None else (self._repo_root / "reports")
        )
        self._batch_service = BatchService(conn)
        self._batches_repo = BatchRepository(conn)
        self._index_service = IndexService(
            docs_dir=docs_dir,
            app_db_path=app_db_path,
            work_index_path=work_index_path,
            reference_index_path=reference_index_path,
        )

    def _job_service(self) -> JobService:
        # `handlers`/`resource_for_kind` は空でよい: start_* は detach()(キュー投入
        # のみ)しか呼ばず、job_status/cancel_job も run_inline を呼ばないため、
        # このプロセス自身がジョブを実行することはない(実行するのは
        # 別プロセスの `worker run`)。
        return JobService(self._conn, owner_id=str(uuid.uuid4()))

    # -- start_* ------------------------------------------------------------

    def _start(self, tool_name: str, params: dict[str, Any]) -> types.CallToolResult:
        kind = _JOB_KIND_FOR_TOOL[tool_name]
        try:
            job = self._job_service().detach(kind, params)
        except AppError as exc:
            return _app_error_result(exc)
        payload = {"ok": True, "job_id": job.id, "kind": job.kind, "state": str(job.state)}
        return ok_result(payload)

    def start_run_batch(self, arguments: dict[str, Any]) -> types.CallToolResult:
        name = arguments["batch"]
        # 存在確認は先に行う(未知バッチ名を worker に投げてから失敗させない)。
        try:
            self._batch_service.list_by_name(name)
        except AppError as exc:
            return _app_error_result(exc)
        return self._start("start_run_batch", {"batch": name})

    def start_download_esa_post(self, arguments: dict[str, Any]) -> types.CallToolResult:
        output_dir = with_docs_prefix(arguments.get("outputDir") or "docs")
        params = {"post": arguments["post"], "outputDir": output_dir}
        return self._start("start_download_esa_post", params)

    def start_download_esa_category(self, arguments: dict[str, Any]) -> types.CallToolResult:
        output_dir = with_docs_prefix(arguments.get("outputDir") or "docs")
        params = {"category": arguments["category"], "outputDir": output_dir}
        return self._start("start_download_esa_category", params)

    def start_download_esa_search(self, arguments: dict[str, Any]) -> types.CallToolResult:
        output_dir = with_docs_prefix(arguments.get("outputDir") or "docs")
        params = {"query": arguments["query"], "outputDir": output_dir}
        return self._start("start_download_esa_search", params)

    def start_download_web(self, arguments: dict[str, Any]) -> types.CallToolResult:
        output_dir = with_docs_prefix(arguments.get("outputDir") or "docs")
        params = {
            "url": arguments["url"],
            "outputDir": output_dir,
            "maxDepth": arguments.get("maxDepth", 3),
            "delay": arguments.get("delay", 1000),
            "concurrency": arguments.get("concurrency", 5),
        }
        return self._start("start_download_web", params)

    def start_download_git(self, arguments: dict[str, Any]) -> types.CallToolResult:
        params = {
            "repository": arguments["repository"],
            "branch": arguments.get("branch"),
            "outputDir": arguments.get("outputDir"),
        }
        return self._start("start_download_git", params)

    def start_render_scene(self, arguments: dict[str, Any]) -> types.CallToolResult:
        spec, parse_error = _parse_scene_spec_argument(arguments.get("sceneSpec"))
        if parse_error is not None:
            return parse_error
        params: dict[str, Any] = {
            "scene_spec": spec,
            "docs_dir": str(self._docs_dir),
            "reports_dir": str(self._reports_dir / "visualizations"),
            "repo_root": str(self._repo_root),
        }
        slug = arguments.get("slug")
        if slug:
            params["slug"] = slug
        return self._start("start_render_scene", params)

    # -- job_status -----------------------------------------------------------

    def job_status(self, arguments: dict[str, Any]) -> types.CallToolResult:
        job_id = arguments["job_id"]
        try:
            job = self._job_service().get(job_id)
        except AppError as exc:
            return _app_error_result(exc)
        return ok_result(_job_to_status_payload(job))

    # -- cancel_job -------------------------------------------------------------

    def cancel_job(self, arguments: dict[str, Any]) -> types.CallToolResult:
        job_id = arguments["job_id"]
        service = self._job_service()
        try:
            service.get(job_id)  # NOT_FOUND を先に出す
            service.cancel(job_id)
        except AppError as exc:
            return _app_error_result(exc)
        job = service.get(job_id)
        payload = {
            "ok": True,
            "job_id": job.id,
            "state": str(job.state),
            "cancel_requested": job.cancel_requested,
        }
        return ok_result(payload)

    # -- get_batch --------------------------------------------------------------

    def get_batch(self, arguments: dict[str, Any]) -> types.CallToolResult:
        name = arguments["batch"]
        try:
            batch = self._batch_service.list_by_name(name)
        except AppError as exc:
            return _app_error_result(exc)
        return ok_result({"ok": True, "batch": _camelize_batch(batch)})

    # -- list_corpora -------------------------------------------------------------

    def list_corpora(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        status = self._index_service.status()
        corpora = [_corpus_summary(corpus, status["corpora"][corpus]) for corpus in CORPUS_LABELS]
        return ok_result({"ok": True, "corpora": corpora})

    # -- system_status ----------------------------------------------------------

    def system_status(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        worker_live = leases.has_live_worker(self._conn)
        service = self._job_service()
        jobs_by_state = {str(state): 0 for state in _ALL_JOB_STATES}
        for job in service.list():
            jobs_by_state[str(job.state)] = jobs_by_state.get(str(job.state), 0) + 1
        status = self._index_service.status()
        corpora = [_corpus_summary(corpus, status["corpora"][corpus]) for corpus in CORPUS_LABELS]
        payload = {
            "ok": True,
            "worker": {"live": worker_live},
            "jobs": jobs_by_state,
            "corpora": corpora,
        }
        return ok_result(payload)


_ALL_JOB_STATES = list(_JobState)


__all__ = ["JobTools", "list_tools", "validate_arguments"]
