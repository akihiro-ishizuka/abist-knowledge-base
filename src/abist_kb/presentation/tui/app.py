"""Textual TUI 本体(設計書 §7.1 Task 6.3)。

Web(`presentation/web/viewmodels/screens.py`)と同じ view-model 関数を
そのまま呼び、`ServiceContainer` を Web と共有する設計にする。これにより
同一ジョブが Web/TUI/CLI で同じ状態・件数・エラーコードを表示するという
§15 の受入条件が構造的に成り立つ(2本目の Application Service 問い合わせを
書かない)。

最低サイズ: 100桁×30行(§7.1)。未満では左ナビを畳んで単一ペイン表示にする
(`on_resize`)。破壊的操作(ジョブのキャンセル・再試行)は `ConfirmModal` を
経由する。長時間稼働エントリポイントとして起動時に `WorkerSupervisor` を
開始する(§10.1)。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    Static,
    TextArea,
)

from abist_kb.presentation.console.theme import TOKEN_STYLES, SemanticToken
from abist_kb.presentation.tui.modals import ConfirmModal, HelpModal
from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

MIN_COLUMNS = 100
MIN_ROWS = 30

_VISUALIZATION_SAMPLE_SPEC = {
    "schema_version": "1.0",
    "scene_kind": "explain",
    "output_format": "mp4",
    "template": "step_explanation",
    "title": "サンプルシーン",
    "sources": [],
    "beats": [],
}


def badge(token_value: str, text: str) -> str:
    """記号+ラベル(色だけで状態を伝えない、§6.1)。`token_value` は
    `SemanticToken` の値文字列(`screens.py` が返す `state_token` 等)。"""
    try:
        token = SemanticToken(token_value)
    except ValueError:
        return text
    style = TOKEN_STYLES[token]
    return f"{style.symbol} {text}"


@dataclass(frozen=True)
class NavArea:
    id: str
    label: str
    key: str  # 数字キー(1-9)


NAV_AREAS: list[NavArea] = [
    NavArea("dashboard", "ダッシュボード", "1"),
    NavArea("sources_batches", "ソース・バッチ", "2"),
    NavArea("jobs", "ジョブ", "3"),
    NavArea("documents", "文書", "4"),
    NavArea("search", "検索", "5"),
    NavArea("chat", "チャット", "6"),
    NavArea("visualization", "可視化", "7"),
    NavArea("quality", "品質", "8"),
    NavArea("settings", "設定・診断", "9"),
]
NAV_BY_ID = {area.id: area for area in NAV_AREAS}


class KbApp(App[None]):
    """9領域を左ナビで切り替える Textual アプリ。"""

    CSS = """
    #nav {
        width: 24;
        border-right: solid $primary;
    }
    #nav.hidden {
        display: none;
    }
    #content {
        width: 1fr;
        padding: 0 1;
    }
    #search-input {
        dock: top;
    }
    """

    BINDINGS = [
        Binding("ctrl+k", "command_palette", "パレット"),
        Binding("slash", "goto_search", "検索", key_display="/"),
        Binding("r", "refresh_view", "更新"),
        Binding("x", "remove_selected", "選択項目を削除"),
        Binding("t", "test_selected_source", "接続テスト"),
        Binding("e", "run_selected_batch", "バッチ実行"),
        Binding("escape", "go_back", "戻る"),
        Binding("question_mark", "show_help", "ヘルプ", key_display="?"),
        Binding("q", "quit", "終了"),
        *[Binding(area.key, f"goto_area('{area.id}')", area.label) for area in NAV_AREAS],
    ]

    def __init__(self, container: ServiceContainer, *, start_worker: bool = True) -> None:
        super().__init__()
        self.container = container
        self._start_worker = start_worker
        self._supervisor_thread: threading.Thread | None = None
        self.current_area = "dashboard"
        self.detail: tuple[str, str] | None = None  # (kind, id) e.g. ("job", "...")
        self._action_results: dict[tuple[str, str], str] = {}
        self._sources_batches_action_result: str | None = None
        self._selected_source_id: str | None = None
        self._selected_batch_id: str | None = None
        self._selected_source_or_batch: tuple[str, str] | None = None
        self._chat_conversation_id: str | None = None

    # -- ライフサイクル --------------------------------------------------

    def on_mount(self) -> None:
        if self._start_worker:
            # `ServiceContainer.build_worker_supervisor()` opens its own SQLite
            # connection (`open_app_db`), and SQLite forbids using a connection
            # from a thread other than the one that created it. It must be
            # called from *inside* the background thread's target, not before
            # `Thread.start()` on the main (TUI) thread — otherwise every tick
            # raises `sqlite3.ProgrammingError` forever (previously observed:
            # this flooded the terminal via stderr logging and made the TUI
            # unresponsive/unkillable, since the worker thread never dies and
            # the flood interferes with Textual's raw-mode terminal handling).
            def _run_supervisor() -> None:
                supervisor = self.container.build_worker_supervisor()
                supervisor.run_forever()

            self._supervisor_thread = threading.Thread(
                target=_run_supervisor, daemon=True, name="tui-worker-supervisor"
            )
            self._supervisor_thread.start()
        self._apply_layout(self.size.width, self.size.height)
        self.render_area(self.current_area)

    async def _on_resize(self, event: Any) -> None:
        await super()._on_resize(event)
        self._apply_layout(event.size.width, event.size.height)

    def _apply_layout(self, width: int, height: int) -> None:
        nav = self.query_one("#nav")
        narrow = width < MIN_COLUMNS or height < MIN_ROWS
        nav.set_class(narrow, "hidden")

    # -- レイアウト --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="nav"):
                yield ListView(
                    *[ListItem(Static(f"{a.key}. {a.label}"), id=f"nav-{a.id}") for a in NAV_AREAS],
                    id="nav-list",
                )
            with VerticalScroll(id="content"):
                yield Static("読み込み中…", id="content-body")
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id.startswith("nav-"):
            self.action_goto_area(item_id.removeprefix("nav-"))

    # -- ナビゲーション ------------------------------------------------------

    def action_goto_area(self, area_id: str) -> None:
        if area_id not in NAV_BY_ID:
            return
        self.current_area = area_id
        self.detail = None
        self.render_area(area_id)

    def action_goto_search(self) -> None:
        import contextlib

        self.action_goto_area("search")
        with contextlib.suppress(Exception):
            self.query_one("#search-input", Input).focus()

    def action_refresh_view(self) -> None:
        self.render_area(self.current_area)

    def action_go_back(self) -> None:
        if self.detail is not None:
            self.detail = None
            self.render_area(self.current_area)

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    # -- 描画 --------------------------------------------------------------

    def _content(self) -> VerticalScroll:
        return self.query_one("#content", VerticalScroll)

    def _set_body(self, *widgets: Any) -> None:
        content = self._content()
        content.remove_children()
        content.mount_all(widgets)

    def render_area(self, area_id: str) -> None:
        if self.detail is not None:
            kind, item_id = self.detail
            if kind == "job":
                self._render_job_detail(item_id)
            elif kind == "document":
                self._render_document_detail(item_id)
            return

        renderers = {
            "dashboard": self._render_dashboard,
            "sources_batches": self._render_sources_batches,
            "jobs": self._render_jobs_list,
            "documents": self._render_documents_list,
            "search": self._render_search,
            "chat": lambda: self._render_stub(screens.chat_stub(self.container)),
            "visualization": self._render_visualization,
            "quality": lambda: self._render_stub(screens.quality_stub(self.container)),
            "settings": self._render_settings,
        }
        renderers[area_id]()

    def _render_stub(self, data: dict[str, Any]) -> None:
        if data.get("available"):
            renderer = {
                "chat": self._render_chat,
                "quality": self._render_quality,
            }.get(self.current_area)
            if renderer is not None:
                renderer()
                return
        text = "利用不可\n\n" + str(data.get("reason", ""))
        self._set_body(Static(text))

    def _render_dashboard(self) -> None:
        data = screens.dashboard(self.container)
        lines = [
            f"文書数: {data['document_count']}",
            "ソース別: "
            + ", ".join(f"{k}={v}" for k, v in data["document_count_by_source"].items()),
            "",
            "直近ジョブ:",
        ]
        for job in data["recent_jobs"]:
            lines.append(f"  {badge(job['state_token'], job['state'])}  {job['kind']}  {job['id']}")
        lines.append("")
        lines.append("警告:")
        for warning in data["warnings"]:
            lines.append(
                f"  {badge(warning['state_token'], warning['state'])}  "
                f"{warning['kind']}  {warning['job_id']}"
            )
        if not data["warnings"]:
            lines.append("  (なし)")
        self._set_body(Static("\n".join(lines)))

    def _render_sources_batches(self) -> None:
        sources = screens.sources_list(self.container)["sources"]
        batches = screens.batches_list(self.container)["batches"]
        source_table = DataTable(id="sources-table")
        source_table.cursor_type = "row"
        source_table.add_columns("id", "名前", "種別", "出力先")
        for src in sources:
            source_table.add_row(
                str(src.get("id", "")),
                str(src.get("display_name", "")),
                str(src.get("type", "")),
                str(src.get("output_dir", "")),
                key=str(src.get("id", "")),
            )

        batch_table = DataTable(id="batches-table")
        batch_table.cursor_type = "row"
        batch_table.add_columns("id", "名前", "種別", "出力先")
        for batch in batches:
            batch_table.add_row(
                str(batch.get("id", "")),
                str(batch.get("name", "")),
                str(batch.get("type", "")),
                str(batch.get("output_dir") or "未設定"),
                key=str(batch.get("id", "")),
            )

        widgets: list[Any] = [
            Static("追加・編集は Web / API のみ（フォーム入力が TUI では煩雑なため）"),
            Static("ソース"),
            source_table,
            _SourceActions(),
        ]
        widgets.extend([Static("バッチ"), batch_table, _BatchActions()])
        widgets.append(
            Static(self._sources_batches_action_result or "", id="sources-batches-result")
        )
        self._set_body(*widgets)

    def _render_jobs_list(self) -> None:
        jobs = screens.jobs_list(self.container)["jobs"]
        table = DataTable(id="jobs-table")
        table.add_columns("state", "kind", "id", "created_at")
        for job in jobs:
            table.add_row(
                badge(job["state_token"], job["state"]),
                job["kind"],
                job["id"],
                job["created_at"] or "",
                key=job["id"],
            )
        self._set_body(table)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table_id = event.data_table.id
        row_key = event.row_key.value
        if table_id == "jobs-table" and row_key:
            self.detail = ("job", str(row_key))
            self.render_area(self.current_area)
        elif table_id == "documents-table" and row_key:
            self.detail = ("document", str(row_key))
            self.render_area(self.current_area)
        elif table_id == "sources-table" and row_key:
            self._selected_source_id = str(row_key)
            self._selected_source_or_batch = ("source", str(row_key))
        elif table_id == "batches-table" and row_key:
            self._selected_batch_id = str(row_key)
            self._selected_source_or_batch = ("batch", str(row_key))

    def _render_job_detail(self, job_id: str) -> None:
        self.current_area = "jobs"
        data = screens.job_detail(self.container, job_id)
        if "error" in data:
            self._set_body(Static(f"エラー: {data['error']['code']} - {data['error']['message']}"))
            return
        job = data["job"]
        lines = [
            f"id: {job['id']}",
            f"kind: {job['kind']}",
            f"state: {badge(job['state_token'], job['state'])}",
            f"error: {job['error']}",
            f"progress: {job['progress']}",
        ]
        if action_result := self._action_results.get(("job", job_id)):
            lines.extend(["", action_result])
        lines.extend(["", "履歴:"])
        for evt in data["history"]:
            lines.append(f"  [{evt['severity']}] {evt['phase']}: {evt['message']}")
        self._set_body(Static("\n".join(lines)), _JobActions(job_id))

    @staticmethod
    def _format_action_result(result: Any) -> str:
        if isinstance(result, dict) and "error" in result:
            error = result["error"]
            return f"操作結果: エラー: {error['code']} - {error['message']}"
        if isinstance(result, dict) and "job" in result:
            job = result["job"]
            return f"操作結果: 成功: ジョブ {job['id']} state={job['state']}"
        if isinstance(result, dict) and "ok" in result:
            status = "成功" if result["ok"] else "失敗"
            return f"操作結果: {status}: {result.get('detail', '')}"
        return "操作結果: 成功"

    async def confirm_and_run(
        self,
        message: str,
        action: Any,
        *,
        result_key: tuple[str, str] | None = None,
        sources_batches_result: bool = False,
    ) -> None:
        """確認モーダルを経由して操作し、対象に対応する領域へ結果を保存する。"""

        def _after(confirmed: bool | None) -> None:
            if confirmed:
                result = action()
                formatted = self._format_action_result(result)
                if result_key is not None:
                    self._action_results[result_key] = formatted
                if sources_batches_result:
                    self._show_sources_batches_result(formatted)
                else:
                    self.render_area(self.current_area)

        self.push_screen(ConfirmModal(message), _after)

    def cancel_job(self, job_id: str) -> None:
        def _do() -> dict[str, Any]:
            return screens.job_cancel(self.container, job_id, confirmed=True)

        self.run_worker(
            self.confirm_and_run(
                f"ジョブ {job_id} をキャンセルしますか?",
                _do,
                result_key=("job", job_id),
            )
        )

    def retry_job(self, job_id: str) -> None:
        def _do() -> dict[str, Any]:
            return screens.job_retry(self.container, job_id)

        self.run_worker(
            self.confirm_and_run(
                f"ジョブ {job_id} を再投入しますか?",
                _do,
                result_key=("job", job_id),
            )
        )

    def _source(self, source_id: str) -> dict[str, Any] | None:
        return next(
            (
                source
                for source in screens.sources_list(self.container)["sources"]
                if source["id"] == source_id
            ),
            None,
        )

    def _batch(self, batch_id: str) -> dict[str, Any] | None:
        return next(
            (
                batch
                for batch in screens.batches_list(self.container)["batches"]
                if batch["id"] == batch_id
            ),
            None,
        )

    def _show_sources_batches_result(self, text: str) -> None:
        self._sources_batches_action_result = text
        self.query_one("#sources-batches-result", Static).update(text)

    def _show_sources_batches_invalid_input(self, message: str) -> None:
        self._show_sources_batches_result(
            self._format_action_result({"error": {"code": "INVALID_INPUT", "message": message}})
        )

    def remove_source(self, source_id: str) -> None:
        source = self._source(source_id)
        if source is None:
            return

        def _do() -> dict[str, Any]:
            outcome = screens.source_remove(self.container, source_id, confirmed=True)
            if outcome.get("deleted"):
                self._selected_source_id = None
                self._selected_source_or_batch = None
                self.query_one("#sources-table", DataTable).remove_row(source_id)
            return outcome

        message = (
            f"ソース {source['display_name']} を削除しますか?\n"
            f"出力先: {source.get('output_dir') or '未設定'}"
        )
        self.run_worker(self.confirm_and_run(message, _do, sources_batches_result=True))

    def test_source_connection(self, source_id: str) -> None:
        result = screens.source_test_connection(self.container, source_id)
        self._show_sources_batches_result(self._format_action_result(result))

    def remove_batch(self, batch_id: str) -> None:
        batch = self._batch(batch_id)
        if batch is None:
            return

        def _do() -> dict[str, Any]:
            outcome = screens.batch_remove(self.container, batch_id, confirmed=True)
            if outcome.get("deleted"):
                self._selected_batch_id = None
                self._selected_source_or_batch = None
                self.query_one("#batches-table", DataTable).remove_row(batch_id)
            return outcome

        message = (
            f"バッチ {batch['name']} を削除しますか?\n出力先: {batch.get('output_dir') or '未設定'}"
        )
        self.run_worker(self.confirm_and_run(message, _do, sources_batches_result=True))

    def run_batch(self, batch_id: str) -> None:
        batch = self._batch(batch_id)
        if batch is None:
            return

        def _do() -> dict[str, Any]:
            return screens.batch_run(self.container, batch_id)

        message = (
            f"バッチ {batch['name']} を実行しますか?\n出力先: {batch.get('output_dir') or '未設定'}"
        )
        self.run_worker(self.confirm_and_run(message, _do, sources_batches_result=True))

    def action_remove_selected(self) -> None:
        if self.current_area != "sources_batches":
            return
        if self._selected_source_or_batch is None:
            self._show_sources_batches_invalid_input(
                "削除するソースまたはバッチを選択してください。"
            )
            return
        kind, item_id = self._selected_source_or_batch
        if kind == "source":
            self.remove_source(item_id)
        else:
            self.remove_batch(item_id)

    def action_remove_selected_source(self) -> None:
        if self.current_area != "sources_batches":
            return
        if self._selected_source_id is None:
            self._show_sources_batches_invalid_input("削除するソースを選択してください。")
            return
        self.remove_source(self._selected_source_id)

    def action_remove_selected_batch(self) -> None:
        if self.current_area != "sources_batches":
            return
        if self._selected_batch_id is None:
            self._show_sources_batches_invalid_input("削除するバッチを選択してください。")
            return
        self.remove_batch(self._selected_batch_id)

    def action_test_selected_source(self) -> None:
        if self.current_area != "sources_batches":
            return
        if self._selected_source_id is None:
            self._show_sources_batches_invalid_input("接続テストするソースを選択してください。")
            return
        self.test_source_connection(self._selected_source_id)

    def action_run_selected_batch(self) -> None:
        if self.current_area != "sources_batches":
            return
        if self._selected_batch_id is None:
            self._show_sources_batches_invalid_input("実行するバッチを選択してください。")
            return
        self.run_batch(self._selected_batch_id)

    def _render_documents_list(self) -> None:
        docs = screens.documents_list(self.container)["documents"]
        table = DataTable(id="documents-table")
        table.add_columns("path", "source", "sync_status", "status")
        for doc in docs:
            table.add_row(
                str(doc.get("path", "")),
                str(doc.get("source", "")),
                str(doc.get("sync_status", "")),
                str(doc.get("status", "")),
                key=str(doc.get("path", "")),
            )
        self._set_body(table)

    def _render_document_detail(self, path: str) -> None:
        self.current_area = "documents"
        data = screens.document_detail(self.container, path)
        if "error" in data:
            self._set_body(Static(f"エラー: {data['error']['code']} - {data['error']['message']}"))
            return
        record = data["document"]
        lines = ["読み取り専用メタデータ:"]
        lines.extend(
            f"  {key}: {value}"
            for key, value in record.items()
            if key not in {"status", "document_type"}
        )
        if data["body_missing"]:
            lines.append("(本文ファイルが見つかりません)")
        if action_result := self._action_results.get(("document", path)):
            lines.extend(["", action_result])
        self._set_body(
            Static("\n".join(lines), id="document-readonly"),
            _DocumentActions(
                path,
                status=str(record.get("status") or ""),
                document_type=str(record.get("document_type") or ""),
            ),
        )

    def update_document_metadata(self, path: str, *, status: str, document_type: str) -> None:
        outcome = screens.document_update_metadata(
            self.container,
            path,
            {
                "status": status or None,
                "document_type": document_type or None,
            },
        )
        self._action_results[("document", path)] = self._format_action_result(outcome)
        self.query_one("#document-result", Static).update(self._action_results[("document", path)])

    def delete_document(self, path: str) -> None:
        def _do() -> dict[str, Any]:
            outcome = screens.document_delete(self.container, path, confirmed=True)
            if outcome.get("deleted"):
                self.detail = None
            return outcome

        self.run_worker(
            self.confirm_and_run(
                f"文書を削除しますか?\n対象パス: {path}",
                _do,
                result_key=("document", path),
            )
        )

    def _render_search(self) -> None:
        self._set_body(
            Input(placeholder="検索クエリを入力して Enter", id="search-input"),
            Static("", id="search-results"),
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat-input":
            self._ask_chat(event.value.strip())
            return
        if event.input.id != "search-input":
            return
        query = event.value.strip()
        if not query:
            return
        data = screens.search(self.container, query)
        results_widget = self.query_one("#search-results", Static)
        if "error" in data:
            results_widget.update(f"エラー: {data['error']['code']} - {data['error']['message']}")
            return
        results = data.get("results", [])
        lines = [f"{len(results)} 件"]
        for r in results:
            lines.append(f"  {r.get('path', r.get('id', ''))}  score={r.get('score', '')}")
        results_widget.update("\n".join(lines))

    def _render_chat(self) -> None:
        self._set_body(
            Static("チャット"),
            Input(placeholder="質問を入力して Enter", id="chat-input"),
            Static("", id="chat-log"),
        )

    def _ask_chat(self, question: str) -> None:
        if not question:
            return
        chat_input = self.query_one("#chat-input", Input)
        chat_log = self.query_one("#chat-log", Static)
        if self._chat_conversation_id is None:
            started = screens.chat_start(self.container)
            if "error" in started:
                error = started["error"]
                chat_log.update(f"エラー: {error.get('code', '')} - {error['message']}")
                return
            self._chat_conversation_id = started["conversation_id"]

        result = screens.chat_ask(
            self.container,
            conversation_id=self._chat_conversation_id,
            question=question,
        )
        chat_input.value = ""
        if "error" in result:
            error = result["error"]
            chat_log.update(f"エラー: {error.get('code', '')} - {error['message']}")
            return

        lines = [f"あなた: {question}", f"アシスタント: {result['text']}"]
        citations = result.get("citations", [])
        if citations:
            lines.append("引用:")
            for citation in citations:
                validity = "" if citation.get("valid") else " (検証失敗)"
                lines.append(
                    f"  {citation['path']}:{citation['start_line']}-"
                    f"{citation['end_line']}{validity}"
                )
        warnings = result.get("citation_warnings", [])
        if warnings:
            lines.append("警告:")
            lines.extend(f"  ⚠ {warning}" for warning in warnings)
        previous = str(chat_log.render())
        chat_log.update("\n".join(filter(None, (previous, "\n".join(lines)))))

    def _render_visualization(self) -> None:
        deps = screens.visualization_deps(self.container)
        if deps.get("ready"):
            deps_text = "レンダリング可能です(Python・Manim・ffmpeg すべて検出済み)。"
        else:
            messages = deps.get("messages") or []
            deps_text = "レンダリング不可: " + (
                "\n".join(messages) if messages else "依存が不足しています。"
            )
        self._set_body(
            Static(deps_text, id="visualization-deps"),
            TextArea(
                json.dumps(_VISUALIZATION_SAMPLE_SPEC, ensure_ascii=False, indent=2),
                id="visualization-spec",
            ),
            _VisualizationActions(render_disabled=not deps.get("ready")),
            Static("", id="visualization-result"),
        )

    def _visualization_spec(self) -> dict[str, Any] | None:
        text_area = self.query_one("#visualization-spec", TextArea)
        result_widget = self.query_one("#visualization-result", Static)
        try:
            parsed = json.loads(text_area.text or "{}")
        except json.JSONDecodeError as exc:
            result_widget.update(f"SceneSpec の JSON パースに失敗しました: {exc}")
            return None
        if not isinstance(parsed, dict):
            result_widget.update("SceneSpec は JSON オブジェクトで指定してください。")
            return None
        return parsed

    def validate_visualization(self) -> None:
        spec = self._visualization_spec()
        if spec is None:
            return
        outcome = screens.visualization_validate(self.container, spec)
        result_widget = self.query_one("#visualization-result", Static)
        if outcome.get("ok"):
            lines = ["検証OK: レンダリング可能です。"]
            lines.extend(f"警告: {w}" for w in outcome.get("warnings") or [])
            result_widget.update("\n".join(lines))
            return
        lines = [f"検証エラー: {outcome.get('code')}"]
        lines.extend(f"  {e['path']}: {e['message']}" for e in outcome.get("errors") or [])
        result_widget.update("\n".join(lines))

    def render_visualization(self) -> None:
        spec = self._visualization_spec()
        if spec is None:
            return
        outcome = screens.visualization_submit_render(self.container, spec)
        result_widget = self.query_one("#visualization-result", Static)
        if "error" in outcome:
            error = outcome["error"]
            result_widget.update(f"エラー: {error['code']} - {error['message']}")
            return
        job = outcome["job"]
        result_widget.update(f"レンダリングを投入しました: job_id={job['id']} state={job['state']}")

    def _render_quality(self) -> None:
        self._set_body(
            Static(
                "品質監査\n"
                "整合性・重複・矛盾・メタデータ補完(dry-run)を実行できます。\n"
                "メタデータ補完の apply=True はハード拒否されます。"
                "適用は CLI の --apply と確認プロンプトを使用してください。"
            ),
            _QualityActions(),
            Static("", id="quality-result"),
        )

    @staticmethod
    def _format_quality_result(result: dict[str, Any]) -> str:
        if "error" in result:
            return f"エラー: {result['error']['message']}"
        run_id = result.get("run_id", result.get("mode", ""))
        lines = [f"監査実行ID: {run_id}"]
        if totals := result.get("totals"):
            lines.append("件数:")
            lines.extend(f"  {key}: {value}" for key, value in totals.items())
        return "\n".join(lines)

    def run_quality_audit(self, audit: str) -> None:
        runners = {
            "integrity": screens.quality_run_integrity,
            "duplicates": screens.quality_run_duplicates,
            "contradictions": screens.quality_run_contradictions,
            "backfill": screens.quality_run_backfill_metadata,
        }
        result = runners[audit](self.container)
        self.query_one("#quality-result", Static).update(self._format_quality_result(result))

    def _render_settings(self) -> None:
        data = screens.settings_diagnostics(self.container)
        lines = ["パス:"]
        for k, v in data["paths"].items():
            lines.append(f"  {k}: {v}")
        lines.append(f"embedding_model: {data['embedding_model']}")
        lines.append("診断:")
        for k, v in data["diagnostics"].items():
            token = "success" if v else "danger"
            lines.append(f"  {badge(token, k)}: {v}")
        self._set_body(Static("\n".join(lines)))

    def action_quit(self) -> None:
        self.exit()


class _JobActions(Vertical):
    """ジョブ詳細画面のキャンセル/再試行ボタン(モーダル確認経由)。"""

    def __init__(self, job_id: str) -> None:
        super().__init__()
        self.job_id = job_id

    def compose(self) -> ComposeResult:
        from textual.widgets import Button

        yield Button("キャンセル", id="job-cancel", variant="error")
        yield Button("再試行", id="job-retry", variant="warning")

    def on_button_pressed(self, event: Any) -> None:
        app = self.app
        if not isinstance(app, KbApp):
            return
        if event.button.id == "job-cancel":
            app.cancel_job(self.job_id)
        elif event.button.id == "job-retry":
            app.retry_job(self.job_id)


class _DocumentActions(Vertical):
    """文書詳細の編集可能項目と削除操作。"""

    def __init__(self, path: str, *, status: str, document_type: str) -> None:
        super().__init__()
        self.path = path
        self.status = status
        self.document_type = document_type

    def compose(self) -> ComposeResult:
        from textual.widgets import Button

        yield Static("編集可能メタデータ:")
        yield Input(value=self.status, placeholder="status", id="document-status")
        yield Input(
            value=self.document_type,
            placeholder="document_type",
            id="document-type",
        )
        yield Button("メタデータを保存", id="document-save", variant="primary")
        yield Button("文書を削除", id="document-delete", variant="error")
        yield Static("", id="document-result")

    def on_button_pressed(self, event: Any) -> None:
        app = self.app
        if not isinstance(app, KbApp):
            return
        if event.button.id == "document-save":
            app.update_document_metadata(
                self.path,
                status=self.query_one("#document-status", Input).value,
                document_type=self.query_one("#document-type", Input).value,
            )
        elif event.button.id == "document-delete":
            app.delete_document(self.path)


class _SourceActions(Vertical):
    """選択中ソースの削除・接続テスト操作。"""

    def compose(self) -> ComposeResult:
        from textual.widgets import Button

        yield Button("接続テスト (t)", id="source-test", variant="primary")
        yield Button("削除 (x)", id="source-remove", variant="error")

    def on_button_pressed(self, event: Any) -> None:
        app = self.app
        if not isinstance(app, KbApp):
            return
        if event.button.id == "source-test":
            app.action_test_selected_source()
        elif event.button.id == "source-remove":
            app.action_remove_selected_source()


class _BatchActions(Vertical):
    """選択中バッチの削除・実行操作。"""

    def compose(self) -> ComposeResult:
        from textual.widgets import Button

        yield Button("実行 (e)", id="batch-run", variant="primary")
        yield Button("削除 (x)", id="batch-remove", variant="error")

    def on_button_pressed(self, event: Any) -> None:
        app = self.app
        if not isinstance(app, KbApp):
            return
        if event.button.id == "batch-run":
            app.action_run_selected_batch()
        elif event.button.id == "batch-remove":
            app.action_remove_selected_batch()


class _VisualizationActions(Vertical):
    """可視化画面の検証・レンダリング操作。検証とレンダリングは別操作にする
    (検証は外部依存なしで即座に返るが、レンダリングは分単位かかり Manim/ffmpeg
    を要するため、依存未導入時はレンダリングボタンを無効化する)。
    """

    def __init__(self, *, render_disabled: bool) -> None:
        super().__init__()
        self._render_disabled = render_disabled

    def compose(self) -> ComposeResult:
        yield Button("検証", id="visualization-validate")
        yield Button("レンダリング", id="visualization-render", disabled=self._render_disabled)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app = self.app
        if not isinstance(app, KbApp):
            return
        if event.button.id == "visualization-validate":
            app.validate_visualization()
        elif event.button.id == "visualization-render":
            app.render_visualization()


class _QualityActions(Vertical):
    """品質監査4種の実行ボタン。backfill は dry-run のみ。"""

    def compose(self) -> ComposeResult:
        yield Button("整合性を検査", id="quality-integrity")
        yield Button("重複を検出", id="quality-duplicates")
        yield Button("矛盾候補を検出", id="quality-contradictions")
        yield Button("メタデータ補完(dry-run)", id="quality-backfill")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app = self.app
        if not isinstance(app, KbApp) or event.button.id is None:
            return
        app.run_quality_audit(event.button.id.removeprefix("quality-"))


def run_tui(container: ServiceContainer, *, start_worker: bool = True) -> None:
    app = KbApp(container, start_worker=start_worker)
    app.run()


__all__ = ["KbApp", "MIN_COLUMNS", "MIN_ROWS", "NAV_AREAS", "run_tui"]
