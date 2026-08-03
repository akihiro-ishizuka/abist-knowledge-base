"""kb-download MCP サーバーの8ツール(M5 task-3a: 契約駆動の半分のみ)。

このタスクの範囲は「fixture でビット契約が固定されているケース」だけである
(`.superpowers/sdd/M5-compat-mcp/task-3a-report.md` 参照)。具体的には:

- `list_batches` — 正常系(全件一覧)。`BatchService.list()` をそのまま整形する。
- `run_batch` — 未知バッチ名エラーのみ。実バッチ実行(esa/web/git 判別・
  `download-batch.js` 相当の同期処理)は M3 Task 3〜5 の範囲であり未実装。
- `download_esa_post`/`download_esa_category`/`download_esa_search`/`download_git`
  — fixture はすべて **スキーマ検証で弾かれ、ハンドラに到達しない**エラー
  ケースのみ。したがってハンドラ本体(esa API 呼び出し・`git clone`)は
  この契約の対象外であり、呼ばれたら明確な `NotImplementedError` を返す。
- `download_web` — fixture は「URL 形式不正」のハンドラ冒頭チェックのみ
  (`tool_result_json` 形式)。実際のクロール処理は対象外。
- `add_web_batch` — fixture は `tools/list` のスキーマのみ(呼び出し自体が
  `batch-config.js` 相当の状態を書き換えるため brief 上スキップ)。よって
  実装せず、呼ばれたら明確な `NotImplementedError` を返す。

**スキーマ検証エラーの形(`sdk_validation_error`)を Python 側で再現する:**
旧 Node 実装は `@modelcontextprotocol/sdk` + zod が
`"MCP error -32602: Input validation error: ..."` という英語の散文を生成する。
Python MCP SDK の `Server.call_tool()` 既定動作(`jsonschema` を使った自動検証)
はメッセージに `-32602` を含まないため、`tests/mcp/replay.py` の比較規則
(`-32602` を含むこと・JSON としてパースできないこと)を満たさない。そこで
`validate_input=False` で自動検証を無効化し、この関数内で `jsonschema` を
使い、`-32602` を含む散文メッセージへ手動で変換してから各ハンドラへ渡す
(全文一致は要求されないため、文言そのものは zod の文言を模倣するだけで
よい — 詳細は `replay.py` の `sdk_validation_error` 比較規則)。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import jsonschema
import mcp.types as types

from abist_kb.application.batch_service import BatchService
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.presentation.mcp.payloads import ok_result

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
    "download_git": (
        "Git リポジトリを取得して docs/ 配下に反映する（download-git.js）。"
        "差分同期: data/git-cache/ にクローンを保持し、2回目以降は shallow fetch "
        "で差分だけ反映する。内容が同じファイルには触れず、上流から消えたファイル"
        'だけを削除して応答の sync に一覧を返す。outputDir が "docs" 始まりで'
        "ない場合は docs/ が前置される。プライベートリポジトリは .env の "
        "GIT_TOKEN 等が必要。"
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
        types.Tool(
            name="download_git",
            description=TOOL_DESCRIPTIONS["download_git"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "branch": {
                        "description": "ブランチ名（省略時はリポジトリのデフォルトブランチ）",
                        "type": "string",
                    },
                    "outputDir": {
                        "description": "出力先（既定 docs/<リポジトリ名>）",
                        "type": "string",
                    },
                    "repository": {
                        "description": "リポジトリ URL（https:// または git@ 形式）",
                        "minLength": 1,
                        "type": "string",
                    },
                },
                "required": ["repository"],
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


class KbDownloadTools:
    """kb-download の8ツールを実装するアダプタ。

    `list_batches`/`run_batch`(未知バッチ名エラー)のみ本物の動作をする。
    残りは fixture がスキーマ検証失敗のみを固定しているため、ハンドラ本体は
    未実装(`NotImplementedError`)。`server_core.py` 側の `call_tool` ディス
    パッチが `validate_arguments()` を先に呼ぶため、fixture が要求するケース
    (すべてスキーマ検証で弾かれる/ハンドラ到達前に完結する)ではこれらの
    `NotImplementedError` に到達しない。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._batch_service = BatchService(conn)

    # -- list_batches ---------------------------------------------------------

    def list_batches(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        batches = self._batch_service.list()
        entries = [_camelize_batch(batch) for batch in batches]
        payload = {"ok": True, "count": len(entries), "batches": entries}
        return ok_result(payload)

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
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=_dumps(payload))],
                    isError=True,
                )
            raise
        raise NotImplementedError(
            f"run_batch({batch['name']!r}) の実同期処理は M5 task-3a の範囲外です"
            "(esa/web/git 実行は後続タスクで実装されます)。"
        )

    # -- add_web_batch(範囲外) ------------------------------------------------

    def add_web_batch(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        raise NotImplementedError(
            "add_web_batch は M5 task-3a の範囲外です(batch-config.js 相当の"
            "状態を書き換える実装は後続タスク)。"
        )

    # -- download_*(範囲外: fixture はスキーマ検証失敗のみを固定) -----------------

    def download_esa_post(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        raise NotImplementedError(
            "download_esa_post の実処理(esa API 呼び出し)は M5 task-3a の範囲外です。"
        )

    def download_esa_category(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        raise NotImplementedError(
            "download_esa_category の実処理(esa API 呼び出し)は M5 task-3a の範囲外です。"
        )

    def download_esa_search(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        raise NotImplementedError(
            "download_esa_search の実処理(esa API 呼び出し)は M5 task-3a の範囲外です。"
        )

    def download_git(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        raise NotImplementedError(
            "download_git の実処理(git clone/fetch)は M5 task-3a の範囲外です。"
        )

    # -- download_web(URL 形式チェックのみ fixture が固定) -----------------------

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
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_dumps(payload))],
                isError=True,
            )
        raise NotImplementedError("download_web の実処理(再帰クロール)は M5 task-3a の範囲外です。")


def _looks_like_http_url(url: str) -> bool:
    """旧実装の `/^https?:\\/\\//i` チェックを再現する。"""
    return url.lower().startswith(("http://", "https://"))


def _dumps(payload: dict[str, Any]) -> str:
    from abist_kb.presentation.mcp.payloads import dumps_tool_json

    return dumps_tool_json(payload)


__all__ = ["KbDownloadTools", "list_tools", "validate_arguments"]
