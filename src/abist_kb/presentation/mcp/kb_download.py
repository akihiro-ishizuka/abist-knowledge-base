"""kb-download MCP サーバーの8ツール(M5 task-3b: ブロッキング実処理)。

**前タスク(task-3a)からの引き継ぎ**: `list_batches`(正常系)・`run_batch`
(未知バッチ名エラー)・`download_esa_*`/`download_git`(スキーマ検証エラー)・
`download_web`(URL形式エラー)は fixture がビット契約を固定しており、
task-3a で実装済み。本タスクはその続き — ハンドラに到達した後の実処理
(`run_batch` 既知名・`download_esa_post`/`download_esa_category`/
`download_esa_search`/`download_web` 正常系・`download_git`・`add_web_batch`)
を実装する。これらは fixture が存在しない(旧実装は子プロセス spawn を伴う
ブロッキング処理であり、契約採取時に実行しなかった)ため、応答の形は
brief(design §7.4.2)の記述: `ok, exitCode, command, outputDir, batchType,
sync{totals, conflicts<=20, missingCandidates<=20, errors<=20, reports},
stdoutTail, stderrTail` に従い、既存の `list_batches`/エラー応答と同じ
JSON 整形規約(`payloads.dumps_tool_json`)で組み立てる。

**`stdoutTail`/`stderrTail` の決定(task-3b の判断事項)**: 旧 Node 実装は
子プロセス(`download-batch.js` 等)を spawn し、その stdout/stderr の末尾を
そのままここに入れていた。Python 版は `BatchService`/`SyncService` を
プロセス内で直接呼ぶため、キャプチャする子プロセス出力そのものが存在しない。
空文字列のまま放置すると「何も起きなかった」のか「配線し忘れた」のか
後から見分けられなくなるため、`JobRunContext.emit()` が発行する進捗
イベント(`phase`/`message`)をそのまま人間可読な行として蓄積し、
`stdoutTail` は INFO/WARNING 相当、`stderrTail` は ERROR 相当のセグメント
として返す(旧実装の「進捗ログが子プロセスの標準出力に流れていた」実態に
最も近い代替)。末尾 `_TAIL_MAX_CHARS` 文字に切り詰める(旧実装がテール
表示だったことを踏襲)。

**docs-write リースの single-flight(task-3b の判断事項)**: 各ブロッキング
ツールは実処理の前に `leases.acquire_resource_lease(..., wait=False)` で
一度だけ即座に取得を試み、失敗すれば(=他のバッチ/同期処理が実行中)
待たずに busy 応答を返す(旧サーバーの busy 即時応答の意味論を維持)。
取得に成功したら直ちに解放し、実処理自体は別スレッド上で新しい DB 接続と
新しい `owner_id` を使って `JobService.run_inline` 経由の本来の(既定
`wait=True` で直列化する)リース取得へ委ねる。probe の解放から実処理の
取得までの間に理論上の競合窓が残る(完全にアトミックではない)が、同一
プロセス内の直後の呼び出しであり実用上の影響は小さいと判断した
(将来 probe をそのまま実処理へ引き継ぐ形に強化する余地はある)。

実処理を別スレッド + 別 SQLite 接続で行うのは、`timeout_seconds`
(既定: batch 60分 / web 30分 / git 15分 / esa 10分、環境変数で上書き可)を
壁時計のタイムアウトとして機能させるため。Python はハンドラを安全に強制
中断できない(`infrastructure.jobs.execution` のモジュール docstring 参照)
ため、タイムアウト到達時はスレッドの完了を待たずに `ok:false` のタイムアウト
応答を返す — 万一その後もバックグラウンドで処理が継続していても、
`docs-write` リースを保持し続けるのは実処理側のジョブ基盤(`run_job` の
自動更新)であり、他プロセスはリースが解放されるまで待たされる/busy に
なるという既存の直列化契約は壊れない。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jsonschema
import mcp.types as types

from abist_kb.application.batch_service import BatchService
from abist_kb.application.job_service import JobService
from abist_kb.application.sync_service import (
    SyncService,
    new_sync_summary,
    record_sync_result,
    write_sync_report,
)
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.job import JobState, ResourceKind, Severity
from abist_kb.domain.metadata_schema import safe_batch_name
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.sources_repo import SourceRepository
from abist_kb.infrastructure.jobs import leases
from abist_kb.infrastructure.sources.base import with_docs_prefix
from abist_kb.infrastructure.sources.esa import DEFAULT_MISSING_THRESHOLD, EsaClient, EsaSyncRunner
from abist_kb.presentation.mcp.payloads import dumps_tool_json, error_result, ok_result

TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_batches": (
        "batch-config.js の定義済みダウンロードバッチ一覧（名前・型 esa/web/git・"
        "出力先）を JSON で返す。run_batch の前に必ず呼び、正確なバッチ名（日本語・"
        "記号を含む）を確認すること。"
    ),
    "run_batch": (
        "定義済みバッチを実行する（esa/web/git を自動判別し download-batch.js に"
        "委譲）。バッチ名は list_batches の name をそのまま渡す（推測で打たない）。"
        "最長60分ブロックする。"
    ),
    "add_web_batch": (
        'Web サイト収集バッチを batch-config.js に登録する（type:"web"）。登録後は'
        "再起動なしで list_batches に反映され、run_batch で繰り返し実行できる。"
        "同名バッチがある場合は overwrite:true が無い限りエラー（esa/git バッチは"
        '上書き不可）。outputDir が "docs" 始まりでない場合は docs/ が前置される'
        "（download_git と同じ規約）。"
    ),
    "download_esa_post": (
        "esa 記事1件を記事番号指定でローカルにダウンロードする"
        "（download-article.js --post）。.env の ESA_TEAM_NAME / ESA_ACCESS_TOKEN "
        "が必要。"
    ),
    "download_esa_category": (
        "esa のカテゴリ配下の記事を一括ダウンロードする"
        "（download-article.js --category）。.env の ESA_TEAM_NAME / "
        "ESA_ACCESS_TOKEN が必要。差分同期: 未変更の記事は上書きしない。"
        "ローカル編集があれば conflict として上書きを見送る。件数は応答の "
        "sync.totals（added/updated/skipped/conflict/missing/error）で確認する"
        "こと（ヒット0件でも ok:true / exitCode 0 になる）。"
    ),
    "download_esa_search": (
        "esa を検索クエリでヒットした記事を一括ダウンロードする"
        "（download-article.js --search）。.env の ESA_TEAM_NAME / "
        "ESA_ACCESS_TOKEN が必要。差分同期: 未変更の記事は上書きしない。"
        "ローカル編集があれば conflict として上書きを見送る。件数は応答の "
        "sync.totals（added/updated/skipped/conflict/missing/error）で確認する"
        "こと（ヒット0件でも ok:true / exitCode 0 になる）。"
    ),
    "download_web": (
        "Web ページを再帰クロールして Markdown 化し保存する（download-web.js）。"
        "最長30分ブロックする。差分同期: ETag / Last-Modified による条件付きGETで"
        "未変更ページは取得も上書きもしない。ローカルで編集されたファイルは"
        "上書きせず conflict として応答の sync に報告する。"
    ),
}


def list_tools() -> list[types.Tool]:
    """`tools/list` に返す8ツールのスキーマ(`tests/fixtures/mcp/tools-list.json` 準拠)。"""
    forbidden = types.ToolExecution(taskSupport="forbidden")
    return [
        types.Tool(
            name="list_batches",
            description=TOOL_DESCRIPTIONS["list_batches"],
            inputSchema={"properties": {}, "type": "object"},
            execution=forbidden,
        ),
        types.Tool(
            name="run_batch",
            description=TOOL_DESCRIPTIONS["run_batch"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "batch": {
                        "description": "batch-config.js のバッチ名（日本語・記号そのまま）",
                        "minLength": 1,
                        "type": "string",
                    }
                },
                "required": ["batch"],
                "type": "object",
            },
            execution=forbidden,
        ),
        types.Tool(
            name="add_web_batch",
            description=TOOL_DESCRIPTIONS["add_web_batch"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "delay": {
                        "description": "リクエスト間隔 ms（既定 1000）",
                        "minimum": 0,
                        "type": "integer",
                    },
                    "maxDepth": {
                        "description": "クロール深度（既定 3）",
                        "maximum": 10,
                        "minimum": 1,
                        "type": "integer",
                    },
                    "name": {
                        "description": (
                            "バッチ名（日本語・記号可。list_batches / run_batch で使う名前）"
                        ),
                        "minLength": 1,
                        "type": "string",
                    },
                    "outputDir": {
                        "description": "出力先（既定 docs/<バッチ名を安全化した名前>）",
                        "type": "string",
                    },
                    "overwrite": {
                        "description": "同名の既存 web バッチを上書きする（既定 false）",
                        "type": "boolean",
                    },
                    "url": {
                        "description": "クロール開始 URL（http/https）",
                        "minLength": 1,
                        "type": "string",
                    },
                },
                "required": ["name", "url"],
                "type": "object",
            },
            execution=forbidden,
        ),
        types.Tool(
            name="download_esa_post",
            description=TOOL_DESCRIPTIONS["download_esa_post"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "outputDir": {
                        "description": "出力先ディレクトリ（既定 docs）",
                        "type": "string",
                    },
                    "post": {
                        "description": "esa 記事番号",
                        "exclusiveMinimum": 0,
                        "type": "integer",
                    },
                },
                "required": ["post"],
                "type": "object",
            },
            execution=forbidden,
        ),
        types.Tool(
            name="download_esa_category",
            description=TOOL_DESCRIPTIONS["download_esa_category"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "category": {
                        "description": (
                            "esa カテゴリパス（例: 設計効率化/三桜工業様/蛇腹形状の自動設計）"
                        ),
                        "minLength": 1,
                        "type": "string",
                    },
                    "outputDir": {
                        "description": "出力先ディレクトリ（既定 docs）",
                        "type": "string",
                    },
                },
                "required": ["category"],
                "type": "object",
            },
            execution=forbidden,
        ),
        types.Tool(
            name="download_esa_search",
            description=TOOL_DESCRIPTIONS["download_esa_search"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "outputDir": {
                        "description": "出力先ディレクトリ（既定 docs）",
                        "type": "string",
                    },
                    "query": {
                        "description": "esa 検索クエリ",
                        "minLength": 1,
                        "type": "string",
                    },
                },
                "required": ["query"],
                "type": "object",
            },
            execution=forbidden,
        ),
        types.Tool(
            name="download_web",
            description=TOOL_DESCRIPTIONS["download_web"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "concurrency": {
                        "description": "同時リクエスト数（既定 5）",
                        "maximum": 10,
                        "minimum": 1,
                        "type": "integer",
                    },
                    "delay": {
                        "description": "リクエスト間隔 ms（既定 1000）",
                        "minimum": 0,
                        "type": "integer",
                    },
                    "maxDepth": {
                        "description": "クロール深度（既定 3）",
                        "maximum": 10,
                        "minimum": 1,
                        "type": "integer",
                    },
                    "outputDir": {
                        "description": "出力先ディレクトリ（既定 docs）",
                        "type": "string",
                    },
                    "url": {
                        "description": "開始 URL（http/https）",
                        "minLength": 1,
                        "type": "string",
                    },
                },
                "required": ["url"],
                "type": "object",
            },
            execution=forbidden,
        ),
    ]


#: `name` -> `inputSchema` の対応表。手動スキーマ検証(下記)に使う。
_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {tool.name: tool.inputSchema for tool in list_tools()}


def validate_arguments(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult | None:
    """`tool_name` の `inputSchema` に対し `arguments` を検証する。

    不正なら旧 Node 実装(zod + `@modelcontextprotocol/sdk`)の
    `"MCP error -32602: Input validation error: ..."` 散文を模した
    `CallToolResult`(`isError=True`)を返す。妥当なら `None` を返す
    (呼び出し側はその場合ハンドラ本体へ進む)。

    Python 側の文言は zod の文言と一字一句一致しない(`jsonschema` の
    `ValidationError.message` をそのまま使う)。`tests/mcp/replay.py` の
    `sdk_validation_error` 比較規則は `-32602` を含むこと・JSON として
    パースできないことしか要求しないため、これで十分。
    """
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


def _camelize_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """`BatchService.list()` の1件(snake_case)を旧 MCP 応答形状(camelCase)へ。"""
    entry: dict[str, Any] = {"name": batch["name"], "type": batch["type"]}
    items = batch["items"]
    if batch["type"] == "esa":
        entry["categories"] = [item["target"] for item in items]
        entry["outputDir"] = batch["output_dir"]
    elif batch["type"] == "web":
        options = items[0]["options"] if items else {}
        entry["url"] = options.get("url")
        entry["outputDir"] = batch["output_dir"]
        entry["maxDepth"] = options.get("max_depth")
        entry["delay"] = options.get("delay")
    elif batch["type"] == "git":
        options = items[0]["options"] if items else {}
        entry["repository"] = options.get("repository")
        entry["branch"] = options.get("branch")
        entry["outputDir"] = batch["output_dir"]
    else:  # pragma: no cover - 現行スキーマでは esa/web/git のみ
        entry["outputDir"] = batch["output_dir"]
    return entry


# ---------------------------------------------------------------------------
# ブロッキング実処理の共通配線
# ---------------------------------------------------------------------------

_TAIL_MAX_CHARS = 4000
_MAX_LISTED_ITEMS = 20
_PROBE_LEASE_TTL_SECONDS = 5.0

#: `(環境変数名, 既定秒数)`。brief §7.4.2: batch 60分 / web 30分 / git 15分 / esa 10分。
_TIMEOUT_ENV: dict[str, tuple[str, float]] = {
    "batch": ("KB_DOWNLOAD_BATCH_TIMEOUT_SECONDS", 60 * 60.0),
    "web": ("KB_DOWNLOAD_WEB_TIMEOUT_SECONDS", 30 * 60.0),
    "esa": ("KB_DOWNLOAD_ESA_TIMEOUT_SECONDS", 10 * 60.0),
}


def _timeout_seconds(kind: str) -> float:
    env_name, default = _TIMEOUT_ENV[kind]
    raw = os.environ.get(env_name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _connection_db_path(conn: sqlite3.Connection) -> Path:
    """`infrastructure.jobs.execution._connection_db_path` と同じ手法(別スレッド用に
    同じ DB ファイルへ専用接続を開く。sqlite3 の `check_same_thread` 制約を回避する)。"""
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2])


class _LogCapture:
    """`emit()` で流れる進捗行を蓄積し、`stdoutTail`/`stderrTail` の代替を作る。

    モジュール docstring の決定事項参照: 旧実装の子プロセス標準出力/標準エラーの
    代わりに、severity=ERROR の行を stderr 相当、それ以外を stdout 相当として
    末尾 `_TAIL_MAX_CHARS` 文字を返す。
    """

    def __init__(self) -> None:
        self._info_lines: list[str] = []
        self._error_lines: list[str] = []

    def record(self, *, phase: str, message: str = "", severity: Severity = Severity.INFO) -> None:
        line = f"[{phase}] {message}" if message else f"[{phase}]"
        if severity is Severity.ERROR:
            self._error_lines.append(line)
        else:
            self._info_lines.append(line)

    def stdout_tail(self) -> str:
        return "\n".join(self._info_lines)[-_TAIL_MAX_CHARS:]

    def stderr_tail(self) -> str:
        return "\n".join(self._error_lines)[-_TAIL_MAX_CHARS:]


def _tool_result(payload: dict[str, Any], *, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=dumps_tool_json(payload))],
        isError=is_error,
    )


def _looks_like_http_url(url: str) -> bool:
    """旧実装の `/^https?:\\/\\//i` チェックを再現する。"""
    return url.lower().startswith(("http://", "https://"))


_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _looks_like_absolute_or_traversal(path: str) -> bool:
    """`add_web_batch` の `outputDir` 検証: 絶対パス・`..` を拒否する。"""
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    if _WINDOWS_ABS_RE.match(path):
        return True
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    return ".." in parts


class KbDownloadTools:
    """kb-download の8ツールを実装するアダプタ。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        root_dir: Path | None = None,
        docs_dir: Path | None = None,
        reports_dir: Path | None = None,
        missing_threshold: int = DEFAULT_MISSING_THRESHOLD,
    ) -> None:
        self._conn = conn
        self._batch_service = BatchService(conn)
        self._batches_repo = BatchRepository(conn)
        self._root_dir = Path(root_dir) if root_dir is not None else Path.cwd()
        self._docs_dir = Path(docs_dir) if docs_dir is not None else (self._root_dir / "docs")
        self._reports_dir = (
            Path(reports_dir) if reports_dir is not None else (self._root_dir / "reports")
        )
        self._missing_threshold = missing_threshold

    def _sync_service(self, conn: sqlite3.Connection) -> SyncService:
        return SyncService(
            root_dir=self._root_dir,
            docs_dir=self._docs_dir,
            reports_dir=self._reports_dir,
            documents=DocumentRepository(conn),
            sources=SourceRepository(conn),
            batches=BatchRepository(conn),
            missing_threshold=self._missing_threshold,
        )

    def _esa_client_kwargs(self) -> dict[str, str | None]:
        team = os.environ.get("ESA_TEAM_NAME")
        token = os.environ.get("ESA_ACCESS_TOKEN")
        if not team or not token:
            raise AppError(
                code=ErrorCode.CONFIG_ERROR,
                message="環境変数 ESA_TEAM_NAME / ESA_ACCESS_TOKEN が設定されていません。",
                exit_code=ExitCode.CONFIG_ERROR,
            )
        # ESA_BASE_URL はテストがローカルのモックサーバーへ向けるためだけの抜け道
        # (application.sync_service の source.connection.base_url と同じ位置づけ)。
        return {"team": team, "access_token": token, "base_url": os.environ.get("ESA_BASE_URL")}

    # -- list_batches ---------------------------------------------------------

    def list_batches(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        batches = self._batch_service.list()
        entries = [_camelize_batch(batch) for batch in batches]
        payload = {"ok": True, "count": len(entries), "batches": entries}
        return ok_result(payload)

    # -- ブロッキング実処理の共通配線 -------------------------------------------

    def _busy_result(
        self, *, command: list[str], batch_type: str, output_dir: str | None
    ) -> types.CallToolResult:
        payload = {
            "ok": False,
            "exitCode": None,
            "command": command,
            "outputDir": output_dir,
            "batchType": batch_type,
            "sync": None,
            "stdoutTail": "",
            "stderrTail": "",
            "error": (
                "他のバッチ/同期処理が実行中です(docs-write リースを取得できません"
                "でした)。完了後に再試行してください。"
            ),
        }
        return _tool_result(payload, is_error=True)

    def _run_blocking(
        self,
        *,
        kind: str,
        batch_type: str,
        command: list[str],
        output_dir: str | None,
        timeout_seconds: float,
        make_sync_call: Any,
    ) -> types.CallToolResult:
        """docs-write リースの busy チェック(非待機)の後、実処理を別スレッド/別接続で行う。

        モジュール docstring の決定事項参照: probe 取得→即解放→実処理側の(既定
        wait=True の)取得、という2段構えなので完全にはアトミックではないが、
        busy の即時応答という旧サーバーの意味論を壊さないための実用上の妥協。
        """
        probe_owner = str(uuid.uuid4())
        try:
            with leases.acquire_resource_lease(
                self._conn,
                ResourceKind.DOCS_WRITE,
                owner_id=probe_owner,
                ttl_seconds=_PROBE_LEASE_TTL_SECONDS,
                wait=False,
            ):
                pass
        except AppError as exc:
            if exc.code is ErrorCode.CONFLICT:
                return self._busy_result(
                    command=command, batch_type=batch_type, output_dir=output_dir
                )
            raise

        return self._run_inline_in_thread(
            kind=kind,
            batch_type=batch_type,
            command=command,
            output_dir=output_dir,
            timeout_seconds=timeout_seconds,
            make_sync_call=make_sync_call,
        )

    def _run_inline_in_thread(
        self,
        *,
        kind: str,
        batch_type: str,
        command: list[str],
        output_dir: str | None,
        timeout_seconds: float,
        make_sync_call: Any,
    ) -> types.CallToolResult:
        db_path = _connection_db_path(self._conn)
        log = _LogCapture()
        outbox: dict[str, Any] = {}
        result_box: dict[str, Any] = {}
        owner_id = str(uuid.uuid4())

        def _worker() -> None:
            thread_conn = connect(db_path)
            try:
                sync_call = make_sync_call(thread_conn)

                def handler(run: Any) -> None:
                    def captured_emit(
                        *,
                        phase: str,
                        current: int | None = None,
                        total: int | None = None,
                        message: str = "",
                        severity: Severity = Severity.INFO,
                        item: str | None = None,
                    ) -> None:
                        log.record(phase=phase, message=message, severity=severity)
                        run.emit(
                            phase=phase,
                            current=current,
                            total=total,
                            message=message,
                            severity=severity,
                            item=item,
                        )

                    summary, report_path = sync_call(captured_emit, run.check_lease)
                    outbox["summary"] = summary
                    outbox["report_path"] = report_path

                job_service = JobService(
                    thread_conn,
                    owner_id=owner_id,
                    handlers={kind: handler},
                    resource_for_kind={kind: (ResourceKind.DOCS_WRITE, None)},
                )
                result_box["job"] = job_service.run_inline(kind, {})
            except BaseException as exc:  # noqa: BLE001 - スレッド越しに呼び出し元へ伝える
                result_box["error"] = exc
            finally:
                thread_conn.close()

        thread = threading.Thread(target=_worker, name="kb-download-blocking", daemon=True)
        thread.start()
        thread.join(timeout=timeout_seconds)

        if thread.is_alive():
            payload = {
                "ok": False,
                "exitCode": None,
                "command": command,
                "outputDir": output_dir,
                "batchType": batch_type,
                "sync": None,
                "stdoutTail": log.stdout_tail(),
                "stderrTail": log.stderr_tail(),
                "error": f"タイムアウトしました(上限 {timeout_seconds:.0f} 秒)。",
            }
            return _tool_result(payload, is_error=True)

        if "error" in result_box:
            exc = result_box["error"]
            message = exc.message if isinstance(exc, AppError) else str(exc)
            exit_code = int(exc.exit_code) if isinstance(exc, AppError) else 1
            summary = outbox.get("summary")
            payload = {
                "ok": False,
                "exitCode": exit_code,
                "command": command,
                "outputDir": output_dir,
                "batchType": batch_type,
                "sync": summary.to_report_dict() if summary is not None else None,
                "stdoutTail": log.stdout_tail(),
                "stderrTail": log.stderr_tail(),
                "error": message,
            }
            return _tool_result(payload, is_error=True)

        job = result_box["job"]
        summary = outbox["summary"]
        report_path = outbox.get("report_path")
        conflicts = [item for item in summary.items if item.get("action") == "conflict"]
        missing = [item for item in summary.items if item.get("action") == "missing"]
        errors = [item for item in summary.items if item.get("action") == "error"]
        ok = job.state == JobState.SUCCEEDED and bool(summary.full_sync_succeeded) and not errors
        payload = {
            "ok": ok,
            "exitCode": 0 if ok else 1,
            "command": command,
            "outputDir": output_dir,
            "batchType": batch_type,
            "sync": {
                "totals": summary.totals,
                "conflicts": conflicts[:_MAX_LISTED_ITEMS],
                "missingCandidates": missing[:_MAX_LISTED_ITEMS],
                "errors": errors[:_MAX_LISTED_ITEMS],
                "reports": [str(report_path)] if report_path else [],
            },
            "stdoutTail": log.stdout_tail(),
            "stderrTail": log.stderr_tail(),
        }
        return _tool_result(payload, is_error=not ok)

    # -- run_batch --------------------------------------------------------

    def run_batch(self, arguments: dict[str, Any]) -> types.CallToolResult:
        name = arguments["batch"]
        try:
            batch = self._batch_service.list_by_name(name)
        except AppError as exc:
            if exc.code is ErrorCode.NOT_FOUND:
                available = ", ".join(b["name"] for b in self._batch_service.list())
                payload = {
                    "ok": False,
                    "exitCode": None,
                    "command": [],
                    "stdoutTail": "",
                    "stderrTail": "",
                    "error": f'バッチ "{name}" は存在しません。利用可能: {available}',
                }
                return _tool_result(payload, is_error=True)
            raise

        batch_id = batch["id"]
        batch_type = batch["type"]
        output_dir = batch.get("output_dir")
        command = ["run_batch", name]

        def make_sync_call(conn: sqlite3.Connection) -> Any:
            def sync_call(emit: Any, check_lease: Any) -> Any:
                return self._sync_service(conn).sync_batch(
                    batch_id, emit=emit, check_lease=check_lease
                )

            return sync_call

        return self._run_blocking(
            kind="kb_download_batch",
            batch_type=batch_type,
            command=command,
            output_dir=output_dir,
            timeout_seconds=_timeout_seconds("batch"),
            make_sync_call=make_sync_call,
        )

    # -- add_web_batch ------------------------------------------------------

    def add_web_batch(self, arguments: dict[str, Any]) -> types.CallToolResult:
        name = arguments["name"]
        url = arguments["url"]
        delay = arguments.get("delay", 1000)
        max_depth = arguments.get("maxDepth", 3)
        overwrite = bool(arguments.get("overwrite", False))
        output_dir_arg = arguments.get("outputDir")

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return error_result(f"url は http/https のみ対応しています: {url}")

        if output_dir_arg is not None and _looks_like_absolute_or_traversal(output_dir_arg):
            return error_result(
                f"outputDir に絶対パスや親ディレクトリ参照(..)は使用できません: {output_dir_arg}"
            )

        output_dir = with_docs_prefix(output_dir_arg or safe_batch_name(name))

        existing = self._batches_repo.get_by_name(name)
        items = [
            {
                "options": {
                    "url": url,
                    "output_dir": output_dir,
                    "max_depth": max_depth,
                    "delay": delay,
                }
            }
        ]

        if existing is not None:
            if existing["type"] != "web":
                return error_result(
                    f"バッチ '{name}' は web 型ではないため上書きできません"
                    f"(現在の型: {existing['type']})。"
                )
            if not overwrite:
                return error_result(
                    f"バッチ '{name}' は既に存在します。上書きするには overwrite:true "
                    "を指定してください。"
                )
            batch = self._batch_service.edit(existing["id"], output_dir=output_dir, items=items)
        else:
            batch = self._batch_service.add(
                name=name, type="web", output_dir=output_dir, items=items
            )

        payload = {"ok": True, "batch": _camelize_batch(batch)}
        return ok_result(payload)

    # -- download_esa_post ----------------------------------------------------

    def download_esa_post(self, arguments: dict[str, Any]) -> types.CallToolResult:
        post_number = arguments["post"]
        output_dir = with_docs_prefix(arguments.get("outputDir") or "docs")
        command = ["download_esa_post", str(post_number)]

        def make_sync_call(conn: sqlite3.Connection) -> Any:
            def sync_call(_emit: Any, _check_lease: Any) -> Any:
                import asyncio

                creds = self._esa_client_kwargs()

                async def _run() -> Any:
                    summary = new_sync_summary("esa")
                    summary.options = {"outputDir": output_dir, "post": post_number}
                    runner = EsaSyncRunner(
                        documents=DocumentRepository(conn),
                        root_dir=self._root_dir,
                        docs_dir=self._docs_dir,
                        output_dir=output_dir,
                        missing_threshold=self._missing_threshold,
                    )
                    async with EsaClient(
                        team=creds["team"],
                        access_token=creds["access_token"],
                        base_url=creds["base_url"],
                    ) as client:
                        post = await client.get_post(post_number)
                    item = runner.save_post(post)
                    record_sync_result(summary, item)
                    summary.full_sync_succeeded = True
                    from datetime import UTC, datetime

                    summary.finished_at = datetime.now(UTC).isoformat()
                    return summary

                summary = asyncio.run(_run())
                report_path = write_sync_report(
                    summary, reports_dir=self._reports_dir, label=f"post-{post_number}"
                )
                return summary, report_path

            return sync_call

        return self._run_blocking(
            kind="kb_download_esa_post",
            batch_type="esa",
            command=command,
            output_dir=output_dir,
            timeout_seconds=_timeout_seconds("esa"),
            make_sync_call=make_sync_call,
        )

    # -- download_esa_category ------------------------------------------------

    def download_esa_category(self, arguments: dict[str, Any]) -> types.CallToolResult:
        category = arguments["category"]
        output_dir = with_docs_prefix(arguments.get("outputDir") or "docs")
        command = ["download_esa_category", category]

        def make_sync_call(conn: sqlite3.Connection) -> Any:
            def sync_call(emit: Any, check_lease: Any) -> Any:
                import asyncio

                creds = self._esa_client_kwargs()
                source = {
                    "output_dir": output_dir,
                    "connection": {
                        "team": creds["team"],
                        "access_token": creds["access_token"],
                        "base_url": creds["base_url"],
                    },
                }
                summary = asyncio.run(
                    self._sync_service(conn)._run_source_sync(  # noqa: SLF001 - 意図的な再利用
                        source,
                        categories=[category],
                        force=False,
                        dry_run=False,
                        prune_orphans=False,
                        emit=emit,
                        check_lease=check_lease,
                    )
                )
                report_path = write_sync_report(
                    summary, reports_dir=self._reports_dir, label=category
                )
                return summary, report_path

            return sync_call

        return self._run_blocking(
            kind="kb_download_esa_category",
            batch_type="esa",
            command=command,
            output_dir=output_dir,
            timeout_seconds=_timeout_seconds("esa"),
            make_sync_call=make_sync_call,
        )

    # -- download_esa_search --------------------------------------------------

    def download_esa_search(self, arguments: dict[str, Any]) -> types.CallToolResult:
        query = arguments["query"]
        output_dir = with_docs_prefix(arguments.get("outputDir") or "docs")
        command = ["download_esa_search", query]

        def make_sync_call(conn: sqlite3.Connection) -> Any:
            def sync_call(emit: Any, check_lease: Any) -> Any:
                import asyncio

                creds = self._esa_client_kwargs()

                async def _run() -> Any:
                    from datetime import UTC, datetime

                    summary = new_sync_summary("esa")
                    summary.options = {"outputDir": output_dir, "query": query}
                    runner = EsaSyncRunner(
                        documents=DocumentRepository(conn),
                        root_dir=self._root_dir,
                        docs_dir=self._docs_dir,
                        output_dir=output_dir,
                        missing_threshold=self._missing_threshold,
                    )
                    async with EsaClient(
                        team=creds["team"],
                        access_token=creds["access_token"],
                        base_url=creds["base_url"],
                    ) as client:
                        posts = await client.search_posts(query)
                    summary.full_sync_succeeded = True
                    # 検索結果は「カテゴリ」の境界を持たないため、esa の欠落判定
                    # (detect_missing_posts)は対象外とする(design判断: 検索クエリ
                    # ヒットが0件になっても「記事が消えた」とは判定できない)。
                    for index, post in enumerate(posts):
                        if check_lease is not None:
                            check_lease()
                        item = runner.save_post(post)
                        record_sync_result(summary, item)
                        if emit is not None:
                            emit(
                                phase="sync-esa",
                                current=index + 1,
                                total=len(posts),
                                message=f"{query}: {item.action}",
                                item=item.path or item.file_path,
                            )
                    summary.finished_at = datetime.now(UTC).isoformat()
                    return summary

                summary = asyncio.run(_run())
                report_path = write_sync_report(summary, reports_dir=self._reports_dir, label=query)
                return summary, report_path

            return sync_call

        return self._run_blocking(
            kind="kb_download_esa_search",
            batch_type="esa",
            command=command,
            output_dir=output_dir,
            timeout_seconds=_timeout_seconds("esa"),
            make_sync_call=make_sync_call,
        )

    # -- download_web ---------------------------------------------------------

    def download_web(self, arguments: dict[str, Any]) -> types.CallToolResult:
        url = arguments["url"]
        if not _looks_like_http_url(url):
            payload = {
                "ok": False,
                "exitCode": None,
                "command": [],
                "stdoutTail": "",
                "stderrTail": "",
                "error": f"URL が不正です（http/https で始まる必要があります）: {url}",
            }
            return _tool_result(payload, is_error=True)

        output_dir = with_docs_prefix(arguments.get("outputDir") or "docs")
        max_depth = arguments.get("maxDepth", 3)
        delay = arguments.get("delay", 1000)
        concurrency = arguments.get("concurrency", 5)
        command = ["download_web", url]

        def make_sync_call(conn: sqlite3.Connection) -> Any:
            def sync_call(emit: Any, check_lease: Any) -> Any:
                return self._sync_service(conn)._sync_web_target(  # noqa: SLF001
                    items=[
                        {
                            "options": {
                                "url": url,
                                "max_depth": max_depth,
                                "delay": delay,
                                "concurrency": concurrency,
                            }
                        }
                    ],
                    batch_output_dir=output_dir,
                    label="download_web",
                    force=False,
                    dry_run=False,
                    emit=emit,
                    check_lease=check_lease,
                )

            return sync_call

        return self._run_blocking(
            kind="kb_download_web",
            batch_type="web",
            command=command,
            output_dir=output_dir,
            timeout_seconds=_timeout_seconds("web"),
            make_sync_call=make_sync_call,
        )


__all__ = ["KbDownloadTools", "list_tools", "validate_arguments"]
