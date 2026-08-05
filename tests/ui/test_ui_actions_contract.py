"""UI 操作可能性の横断契約テスト(Task 8、design/ui-action-matrix.yaml を正本とする)。

`tests/ui/test_cross_ui_contract.py`(既存)は「表示が Web/TUI/CLI で一致するか」
だけを見る。それが緑だったために、実際には画面から呼べない操作(ボタン/キー
バインド/ルートが無い)が M6〜M7 の間に見逃された。**本ファイルはその再発防止**:
`design/ui-action-matrix.yaml` を正本として読み、各操作について

1. 宣言された面(Web/TUI/API)から実際に**呼び出せること**(関数の存在ではなく、
   画面から到達できること — ボタン・キーバインド・ルートの存在)。
2. 破壊的操作は確認を経ること(Web/TUI: 「いいえ」で副作用なし、API:
   `confirmed` 無しで 400)。
3. 宣言されていない面には理由(`*_omitted_reason`)が必須。理由が無い欠落・
   宣言されているのに未配線の操作は失格。
4. 上記の検査機構自体が空虚(何も検査せず常に緑)でないこと。

を検査する。既存の個別テスト(`test_tui.py`/`test_web_pages.py`/`tests/api/test_api.py`)
と検査対象が重なる部分はあるが、ここでの目的はそれらとは違う: 個別テストは
「実装者が書き忘れなければ検出できる」のに対し、本ファイルは
`design/ui-action-matrix.yaml` が更新されるたびに**自動で**新しい操作の配線漏れ
を検出する(手書きテストの追加を待たない)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from nicegui.testing import User
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Static

from abist_kb.config import Settings
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.api.app import create_api_app
from abist_kb.presentation.tui.app import KbApp
from abist_kb.presentation.web.app import register_pages
from abist_kb.presentation.web.viewmodels import screens
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

pytest_plugins = ["nicegui.testing.plugin"]

MATRIX_PATH = Path(__file__).resolve().parents[2] / "design" / "ui-action-matrix.yaml"
ALL_SURFACES = ("web", "tui", "api")
MonkeyPatch = pytest.MonkeyPatch


# ---------------------------------------------------------------------------
# 正本(YAML)の読み込みとヘルパー
# ---------------------------------------------------------------------------


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def iter_actions(matrix: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """`(screen_id, action_id, action_dict)` のフラットな一覧。"""
    result: list[tuple[str, str, dict[str, Any]]] = []
    for screen_id, screen in (matrix.get("screens") or {}).items():
        for action_id, action in (screen.get("actions") or {}).items():
            result.append((screen_id, action_id, action))
    return result


def actions_for_surface(matrix: dict[str, Any], surface: str) -> list[str]:
    return sorted(
        action_id
        for _screen_id, action_id, action in iter_actions(matrix)
        if surface in (action.get("surfaces") or [])
    )


def destructive_actions(matrix: dict[str, Any]) -> list[str]:
    return sorted(
        action_id
        for _screen_id, action_id, action in iter_actions(matrix)
        if action.get("destructive")
    )


def missing_registry_entries(
    matrix: dict[str, Any], registry: dict[str, Any], surface: str
) -> list[str]:
    """`surface` を宣言している操作のうち `registry` に登録が無いものの一覧。

    Step 4(非空虚性)の検査対象そのもの: このヘルパーが常に `[]` を返す
    実装(例: 判定を素通りさせる)だと、`registry` から1件消しても `[]` の
    ままになり検査が無意味になる。`test_registry_completeness_check_is_non_vacuous`
    がこの関数を直接、意図的に1件欠けた `registry` へ通してそれを確認する。
    """
    return [
        action_id for action_id in actions_for_surface(matrix, surface) if action_id not in registry
    ]


THE_MATRIX = load_matrix()


@pytest.fixture
def wired_container(tmp_root: Path) -> ServiceContainer:
    """Web ページを登録済みの `ServiceContainer`(`nicegui.testing.User` 用)。

    `tests/ui/test_web_pages.py` の同名フィクスチャと同じ構成。
    """
    settings = Settings(root_dir=tmp_root / "root", _env_file=None)
    settings.ensure_directories()
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    cont = ServiceContainer(settings, check_same_thread=False)
    register_pages(cont)
    return cont


# ---------------------------------------------------------------------------
# Step 3: 意図的な未実装の明示(理由の必須化)
# ---------------------------------------------------------------------------


def test_every_action_declares_a_reason_for_every_omitted_surface() -> None:
    """`surfaces` に含まれない面ごとに `{surface}_omitted_reason` が無ければ失格。

    黙って欠ける(理由を書かずに面を諦める)ことを許さない — Task 0 の契約。
    """
    matrix = load_matrix()
    failures: list[str] = []
    for screen_id, action_id, action in iter_actions(matrix):
        surfaces = set(action.get("surfaces") or [])
        unknown = surfaces - set(ALL_SURFACES)
        if unknown:
            failures.append(f"{screen_id}.{action_id}: 未知の surface {sorted(unknown)}")
        for surface in ALL_SURFACES:
            if surface not in surfaces and not action.get(f"{surface}_omitted_reason"):
                failures.append(f"{screen_id}.{action_id}: {surface}_omitted_reason が無い")
    assert failures == [], "\n".join(failures)


def test_matrix_has_no_orphaned_actions_without_any_surface() -> None:
    """全面が省略(全て `*_omitted_reason` 頼み)の操作は定義ミスとみなす。"""
    matrix = load_matrix()
    orphans = [
        f"{screen_id}.{action_id}"
        for screen_id, action_id, action in iter_actions(matrix)
        if not (action.get("surfaces") or [])
    ]
    assert orphans == [], f"どの面にも配線されていない操作: {orphans}"


# ---------------------------------------------------------------------------
# API: ルートの存在(§1 到達可能性)+ 破壊的操作の confirmed ガード(§2)
# ---------------------------------------------------------------------------

# action_id -> (HTTPメソッド, FastAPI に登録されたパステンプレート)。
# `create_api_app()` が実際に登録するルートと文字列一致で比較する
# (`Starlette` の `route.path` はデコレータに書いた文字列そのまま、
# 例: `/api/v1/documents/{path:path}`)。
API_ROUTE_BY_ACTION: dict[str, tuple[str, str]] = {
    "source_add": ("POST", "/api/v1/sources"),
    "source_edit": ("PATCH", "/api/v1/sources/{source_id}"),
    "source_remove": ("DELETE", "/api/v1/sources/{source_id}"),
    "source_test_connection": ("POST", "/api/v1/sources/{source_id}/test"),
    "batch_add": ("POST", "/api/v1/batches"),
    "batch_edit": ("PATCH", "/api/v1/batches/{batch_id}"),
    "batch_remove": ("DELETE", "/api/v1/batches/{batch_id}"),
    "batch_run": ("POST", "/api/v1/batches/{batch_id}/run"),
    "job_cancel": ("POST", "/api/v1/jobs/{job_id}/cancel"),
    "job_retry": ("POST", "/api/v1/jobs/{job_id}/retry"),
    "document_update_metadata": ("PATCH", "/api/v1/documents/{path:path}"),
    "document_delete": ("DELETE", "/api/v1/documents/{path:path}"),
    "search_run": ("GET", "/api/v1/search"),
    "chat_start": ("POST", "/api/v1/chat/start"),
    "chat_ask": ("POST", "/api/v1/chat/ask"),
    "chat_history": ("GET", "/api/v1/chat/{conversation_id}/history"),
    "visualization_validate": ("POST", "/api/v1/visualization/validate"),
    "visualization_submit_render": ("POST", "/api/v1/visualization/render"),
    "visualization_deps": ("GET", "/api/v1/visualization/deps"),
    "quality_run_integrity": ("POST", "/api/v1/quality/integrity"),
    "quality_run_duplicates": ("POST", "/api/v1/quality/duplicates"),
    "quality_run_contradictions": ("POST", "/api/v1/quality/contradictions"),
    "quality_run_backfill_metadata": ("POST", "/api/v1/quality/backfill-metadata"),
}


def _assert_route_registered(client: TestClient, method: str, path: str) -> None:
    for route in client.app.routes:
        if getattr(route, "path", None) == path and method.upper() in (
            getattr(route, "methods", None) or set()
        ):
            return
    raise AssertionError(f"API ルートが未登録: {method} {path}")


@pytest.fixture
def client(container: ServiceContainer) -> TestClient:
    return TestClient(create_api_app(container, bind_host="127.0.0.1"))


def test_every_api_action_has_a_registered_route_mapping() -> None:
    """マトリクスが `api` を宣言している操作は全て `API_ROUTE_BY_ACTION` に居る。

    無ければ「未配線」として即失格(Step 3: 宣言されているのに未実装は失格)。
    """
    missing = missing_registry_entries(THE_MATRIX, API_ROUTE_BY_ACTION, "api")
    assert missing == [], f"API 到達性チェックが未登録の操作: {missing}"


@pytest.mark.parametrize("action_id", actions_for_surface(THE_MATRIX, "api"))
def test_api_action_route_is_reachable(action_id: str, client: TestClient) -> None:
    method, path = API_ROUTE_BY_ACTION[action_id]
    _assert_route_registered(client, method, path)


# action_id -> DELETE/POST を confirmed 無しで叩いて 400 になることを確認する。
API_DECLINE_CHECKS: dict[str, Callable[[TestClient, ServiceContainer], None]] = {}


def _api_source_remove_without_confirmation(
    client: TestClient, container: ServiceContainer
) -> None:
    source = container.sources.add(
        type="web",
        display_name="契約テスト用ソース",
        connection={"url": "https://example.invalid"},
        output_dir="docs/contract-test",
    )
    response = client.delete(f"/api/v1/sources/{source['id']}")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert any(s["id"] == source["id"] for s in container.sources.list())


def _api_batch_remove_without_confirmation(client: TestClient, container: ServiceContainer) -> None:
    batch = container.batches.add(name="契約テスト用バッチ", type="web", items=[])
    response = client.delete(f"/api/v1/batches/{batch['id']}")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert any(b["id"] == batch["id"] for b in container.batches.list())


def _api_job_cancel_without_confirmation(client: TestClient, container: ServiceContainer) -> None:
    job = JobRepository(container.conn).submit("noop", {})
    response = client.post(f"/api/v1/jobs/{job.id}/cancel")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert container.jobs.get(job.id).state == JobState.QUEUED


def _api_document_delete_without_confirmation(
    client: TestClient, container: ServiceContainer
) -> None:
    container.documents.upsert(
        {"path": "contract-test-keep.md", "source": "manual", "status": "active"}
    )
    response = client.delete("/api/v1/documents/contract-test-keep.md")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert container.documents.get_or_none("contract-test-keep.md") is not None


API_DECLINE_CHECKS = {
    "source_remove": _api_source_remove_without_confirmation,
    "batch_remove": _api_batch_remove_without_confirmation,
    "job_cancel": _api_job_cancel_without_confirmation,
    "document_delete": _api_document_delete_without_confirmation,
}


def test_every_destructive_api_action_has_a_confirmation_check() -> None:
    missing = missing_registry_entries(THE_MATRIX, API_DECLINE_CHECKS, "api")
    destructive = set(destructive_actions(THE_MATRIX))
    missing_destructive = [a for a in missing if a in destructive]
    assert missing_destructive == [], (
        f"破壊的 API 操作で confirmed ガードの検査が未登録: {missing_destructive}"
    )


@pytest.mark.parametrize("action_id", destructive_actions(THE_MATRIX))
def test_destructive_api_action_requires_confirmation(
    action_id: str, client: TestClient, container: ServiceContainer
) -> None:
    """§2: 破壊的操作は API で `confirmed` 無しだと 400、かつ副作用が無い。"""
    check = API_DECLINE_CHECKS.get(action_id)
    if check is None:
        pytest.skip(f"{action_id} は API 面を宣言していない")
    check(client, container)


# ---------------------------------------------------------------------------
# Web: ボタン/マーカーの存在(§1 到達可能性)。`nicegui.testing.User` で実描画する。
# `user.find(...)` は要素が1つも無いと `AssertionError` を投げる(素通りしない)。
# ---------------------------------------------------------------------------


def _make_chat_available(monkeypatch: MonkeyPatch) -> None:
    """テスト環境は `openai_api_key` 未設定でチャットが常にスタブになるため、
    到達性チェックのためだけに `screens.chat_*` を差し替える
    (`tests/ui/test_tui.py::test_available_chat_renders_...` と同じ手法)。"""
    monkeypatch.setattr(screens, "chat_stub", lambda _container: {"available": True})
    monkeypatch.setattr(
        screens, "chat_start", lambda _container, **_kw: {"conversation_id": "contract-check-conv"}
    )
    monkeypatch.setattr(
        screens,
        "chat_ask",
        lambda _container, *, conversation_id, question: {
            "conversation_id": conversation_id,
            "message_id": f"contract-check-{question}",
            "text": f"回答: {question}",
            "citations": [],
            "citation_warnings": [],
        },
    )


WebCheck = Callable[[User, ServiceContainer, MonkeyPatch], Awaitable[None]]


async def _web_open_and_find(
    user: User, path: str, label_or_marker: str, *, marker: bool = False
) -> None:
    """`marker=False` の既定では **`ui.button` に限定して** ラベルを探す。

    `nicegui.testing.User.find(str)` はページ上の任意要素の
    テキスト/ラベル/値等を対象に**部分一致**で検索する
    (`ElementFilter`: "Partial matches like 'Hello' in 'Hello World!' are
    sufficient")。ボタンのラベルが他要素の説明文の部分文字列にもなっている
    ケース(例: `quality.py` の説明文「...メタデータ補完(dry-run)を実行できます。」
    は同ページのボタンラベル「メタデータ補完(dry-run)」を部分文字列として含む)
    では、ボタン自体を消してもこの説明文にマッチして**偽陽性**になる
    (実際にこの偽陽性を手元で確認し、`kind=ui.button` 限定に修正した)。
    そのため `kind=ui.button` を必ず付け、ボタンそのものの存在だけを見る。
    """
    from nicegui import ui

    await user.open(path)
    if marker:
        user.find(marker=label_or_marker)
    else:
        user.find(kind=ui.button, content=label_or_marker)


async def _web_source_add(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/sources", "ソース追加")


async def _web_source_edit(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/sources", "ソース編集")


async def _web_source_remove(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/sources", "ソース削除")


async def _web_source_test_connection(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/sources", "接続テスト")


async def _web_batch_add(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/sources", "バッチ追加")


async def _web_batch_edit(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/sources", "バッチ編集")


async def _web_batch_remove(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/sources", "バッチ削除")


async def _web_batch_run(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/sources", "バッチ実行")


async def _web_job_cancel(user: User, container: ServiceContainer, _m: MonkeyPatch) -> None:
    job = JobRepository(container.conn).submit("noop", {})
    await _web_open_and_find(user, f"/jobs/{job.id}", "キャンセル")


async def _web_job_retry(user: User, container: ServiceContainer, _m: MonkeyPatch) -> None:
    job = JobRepository(container.conn).submit("noop", {})
    await _web_open_and_find(user, f"/jobs/{job.id}", "再実行")


async def _web_document_update_metadata(
    user: User, container: ServiceContainer, _m: MonkeyPatch
) -> None:
    container.documents.upsert(
        {"path": "reach-check-update.md", "source": "manual", "status": "active"}
    )
    await _web_open_and_find(user, "/documents/reach-check-update.md", "メタデータを保存")


async def _web_document_delete(user: User, container: ServiceContainer, _m: MonkeyPatch) -> None:
    container.documents.upsert(
        {"path": "reach-check-delete.md", "source": "manual", "status": "active"}
    )
    await _web_open_and_find(user, "/documents/reach-check-delete.md", "文書を削除")


async def _web_search_run(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/search", "検索")


async def _web_chat_start(
    user: User, container: ServiceContainer, monkeypatch: MonkeyPatch
) -> None:
    """`chat_start` は専用ボタンが無く、初回送信時に暗黙的に呼ばれる(chat.py)。
    そのため到達性は「メッセージ入力欄+送信ボタンが画面に存在すること」で見る。"""
    _make_chat_available(monkeypatch)
    await user.open("/chat")
    user.find(marker="chat-input")
    user.find(marker="chat-send")


async def _web_chat_ask(user: User, container: ServiceContainer, monkeypatch: MonkeyPatch) -> None:
    """実際に1往復させ、応答が描画されることまで確認する(単なる存在確認より強い)。"""
    _make_chat_available(monkeypatch)
    await user.open("/chat")
    user.find(marker="chat-input").type("こんにちは")
    user.find(marker="chat-send").click()
    await user.should_see("回答: こんにちは")


async def _web_chat_history(
    user: User, container: ServiceContainer, monkeypatch: MonkeyPatch
) -> None:
    """`chat_history` に対応する専用ボタンは無い: Web はサーバへ再取得せず、
    ログ列(`#chat-log`)へ会話を蓄積し続けることで履歴を提示する設計
    (`presentation/web/pages/chat.py`)。2往復目でも1往復目の文言が
    画面に残っていることを確認し、「履歴が見える」ことを検証する。"""
    _make_chat_available(monkeypatch)
    await user.open("/chat")
    user.find(marker="chat-input").type("最初の質問")
    user.find(marker="chat-send").click()
    await user.should_see("回答: 最初の質問")
    user.find(marker="chat-input").type("次の質問")
    user.find(marker="chat-send").click()
    await user.should_see("回答: 次の質問")
    await user.should_see("回答: 最初の質問")  # 1往復目の履歴が消えていない


async def _web_visualization_validate(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/visualization", "検証")


async def _web_visualization_submit_render(
    user: User, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    await _web_open_and_find(user, "/visualization", "visualization-render-button", marker=True)


async def _web_visualization_deps(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/visualization", "visualization-deps", marker=True)


async def _web_quality_run_integrity(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/quality", "整合性を検査")


async def _web_quality_run_duplicates(user: User, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    await _web_open_and_find(user, "/quality", "重複を検出")


async def _web_quality_run_contradictions(
    user: User, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    await _web_open_and_find(user, "/quality", "矛盾候補を検出")


async def _web_quality_run_backfill_metadata(
    user: User, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    await _web_open_and_find(user, "/quality", "メタデータ補完(dry-run)")


WEB_CHECKS: dict[str, WebCheck] = {
    "source_add": _web_source_add,
    "source_edit": _web_source_edit,
    "source_remove": _web_source_remove,
    "source_test_connection": _web_source_test_connection,
    "batch_add": _web_batch_add,
    "batch_edit": _web_batch_edit,
    "batch_remove": _web_batch_remove,
    "batch_run": _web_batch_run,
    "job_cancel": _web_job_cancel,
    "job_retry": _web_job_retry,
    "document_update_metadata": _web_document_update_metadata,
    "document_delete": _web_document_delete,
    "search_run": _web_search_run,
    "chat_start": _web_chat_start,
    "chat_ask": _web_chat_ask,
    "chat_history": _web_chat_history,
    "visualization_validate": _web_visualization_validate,
    "visualization_submit_render": _web_visualization_submit_render,
    "visualization_deps": _web_visualization_deps,
    "quality_run_integrity": _web_quality_run_integrity,
    "quality_run_duplicates": _web_quality_run_duplicates,
    "quality_run_contradictions": _web_quality_run_contradictions,
    "quality_run_backfill_metadata": _web_quality_run_backfill_metadata,
}


def test_every_web_action_has_a_reachability_check() -> None:
    missing = missing_registry_entries(THE_MATRIX, WEB_CHECKS, "web")
    assert missing == [], f"Web 到達性チェックが未登録の操作: {missing}"


@pytest.mark.parametrize("action_id", actions_for_surface(THE_MATRIX, "web"))
async def test_web_action_is_reachable_from_the_screen(
    action_id: str, user: User, wired_container: ServiceContainer, monkeypatch: MonkeyPatch
) -> None:
    await WEB_CHECKS[action_id](user, wired_container, monkeypatch)


# Web の破壊的操作:「いいえ」で副作用が無いこと。
WebDeclineCheck = Callable[[User, ServiceContainer], Awaitable[None]]


async def _web_decline_source_remove(user: User, container: ServiceContainer) -> None:
    from nicegui import ui

    source = container.sources.add(
        type="web",
        display_name="拒否テスト用ソース",
        connection={"url": "https://example.invalid"},
        output_dir="docs/decline-test",
    )
    await user.open("/sources")
    table = next(iter(user.find(kind=ui.table, marker="sources-table").elements))
    table.selected = [source]
    user.find(kind=ui.button, content="ソース削除").click()
    await user.should_see("削除しますか?")
    user.find(kind=ui.button, content="いいえ").click()
    assert any(s["id"] == source["id"] for s in container.sources.list())


async def _web_decline_batch_remove(user: User, container: ServiceContainer) -> None:
    from nicegui import ui

    batch = container.batches.add(name="拒否テスト用バッチ", type="web", items=[])
    await user.open("/sources")
    table = next(iter(user.find(kind=ui.table, marker="batches-table").elements))
    table.selected = [batch]
    user.find(kind=ui.button, content="バッチ削除").click()
    await user.should_see("削除しますか?")
    user.find(kind=ui.button, content="いいえ").click()
    assert any(b["id"] == batch["id"] for b in container.batches.list())


async def _web_decline_job_cancel(user: User, container: ServiceContainer) -> None:
    from nicegui import ui

    job = JobRepository(container.conn).submit("noop", {})
    await user.open(f"/jobs/{job.id}")
    user.find(kind=ui.button, content="キャンセル").click()
    await user.should_see("キャンセルしますか?")
    user.find(kind=ui.button, content="いいえ").click()
    assert container.jobs.get(job.id).state == JobState.QUEUED


async def _web_decline_document_delete(user: User, container: ServiceContainer) -> None:
    from nicegui import ui

    container.documents.upsert(
        {"path": "decline-test-keep.md", "source": "manual", "status": "active"}
    )
    audit_before = container.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    await user.open("/documents/decline-test-keep.md")
    user.find(kind=ui.button, content="文書を削除").click()
    await user.should_see("削除しますか?")
    user.find(kind=ui.button, content="いいえ").click()
    assert container.documents.get_or_none("decline-test-keep.md") is not None
    audit_after = container.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    assert audit_after == audit_before


WEB_DECLINE_CHECKS: dict[str, WebDeclineCheck] = {
    "source_remove": _web_decline_source_remove,
    "batch_remove": _web_decline_batch_remove,
    "job_cancel": _web_decline_job_cancel,
    "document_delete": _web_decline_document_delete,
}


def test_every_destructive_web_action_has_a_decline_check() -> None:
    missing = missing_registry_entries(THE_MATRIX, WEB_DECLINE_CHECKS, "web")
    destructive = set(destructive_actions(THE_MATRIX))
    missing_destructive = [a for a in missing if a in destructive]
    assert missing_destructive == [], (
        f"破壊的 Web 操作で「いいえ」検査が未登録: {missing_destructive}"
    )


@pytest.mark.parametrize("action_id", destructive_actions(THE_MATRIX))
async def test_destructive_web_action_declines_without_side_effect(
    action_id: str, user: User, wired_container: ServiceContainer
) -> None:
    check = WEB_DECLINE_CHECKS.get(action_id)
    if check is None:
        pytest.skip(f"{action_id} は Web 面を宣言していない")
    await check(user, wired_container)


# ---------------------------------------------------------------------------
# TUI: ボタン/入力欄の存在(§1 到達可能性)。`Pilot`(`run_test`)で実描画する。
# `app.query_one(...)` は無いと `NoMatches` を投げる(素通りしない)。
# ---------------------------------------------------------------------------

TuiCheck = Callable[["KbApp", Any, ServiceContainer, MonkeyPatch], Awaitable[None]]


async def _tui_source_remove(app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    app.action_goto_area("sources_batches")
    await pilot.pause()
    app.query_one("#source-remove", Button)


async def _tui_source_test_connection(
    app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    app.action_goto_area("sources_batches")
    await pilot.pause()
    app.query_one("#source-test", Button)


async def _tui_batch_remove(app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    app.action_goto_area("sources_batches")
    await pilot.pause()
    app.query_one("#batch-remove", Button)


async def _tui_batch_run(app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    app.action_goto_area("sources_batches")
    await pilot.pause()
    app.query_one("#batch-run", Button)


async def _tui_job_cancel(
    app: KbApp, pilot: Any, container: ServiceContainer, _m: MonkeyPatch
) -> None:
    job = JobRepository(container.conn).submit("noop", {})
    app.detail = ("job", job.id)
    app.current_area = "jobs"
    app.render_area("jobs")
    await pilot.pause()
    app.query_one("#job-cancel", Button)


async def _tui_job_retry(
    app: KbApp, pilot: Any, container: ServiceContainer, _m: MonkeyPatch
) -> None:
    job = JobRepository(container.conn).submit("noop", {})
    app.detail = ("job", job.id)
    app.current_area = "jobs"
    app.render_area("jobs")
    await pilot.pause()
    app.query_one("#job-retry", Button)


async def _tui_document_update_metadata(
    app: KbApp, pilot: Any, container: ServiceContainer, _m: MonkeyPatch
) -> None:
    container.documents.upsert(
        {"path": "tui-reach-check-update.md", "source": "manual", "status": "active"}
    )
    app.detail = ("document", "tui-reach-check-update.md")
    app.current_area = "documents"
    app.render_area("documents")
    await pilot.pause()
    app.query_one("#document-save", Button)


async def _tui_document_delete(
    app: KbApp, pilot: Any, container: ServiceContainer, _m: MonkeyPatch
) -> None:
    container.documents.upsert(
        {"path": "tui-reach-check-delete.md", "source": "manual", "status": "active"}
    )
    app.detail = ("document", "tui-reach-check-delete.md")
    app.current_area = "documents"
    app.render_area("documents")
    await pilot.pause()
    app.query_one("#document-delete", Button)


async def _tui_search_run(app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch) -> None:
    _m.setattr(
        screens,
        "search",
        lambda _container, _query: {"results": [{"path": "docs/contract-search.md", "score": 1.0}]},
    )
    app.action_goto_search()
    await pilot.pause()
    search_input = app.query_one("#search-input", Input)
    search_input.focus()
    await pilot.press(*"契約テスト")
    await pilot.press("enter")
    await pilot.pause()
    results_text = str(app.query_one("#search-results", Static).render())
    assert "docs/contract-search.md" in results_text


async def _tui_chat_start(
    app: KbApp, pilot: Any, _c: ServiceContainer, monkeypatch: MonkeyPatch
) -> None:
    _make_chat_available(monkeypatch)
    app.action_goto_area("chat")
    await pilot.pause()
    app.query_one("#chat-input", Input)


async def _tui_chat_ask(
    app: KbApp, pilot: Any, _c: ServiceContainer, monkeypatch: MonkeyPatch
) -> None:
    _make_chat_available(monkeypatch)
    app.action_goto_area("chat")
    await pilot.pause()
    chat_input = app.query_one("#chat-input", Input)
    chat_input.focus()
    await pilot.press(*"こんにちは")
    await pilot.press("enter")
    await pilot.pause()
    log_text = str(app.query_one("#chat-log", Static).render())
    assert "回答: こんにちは" in log_text


async def _tui_chat_history(
    app: KbApp, pilot: Any, _c: ServiceContainer, monkeypatch: MonkeyPatch
) -> None:
    """Web と同じ理由(専用ボタン無し、`#chat-log` への蓄積で履歴提示)。"""
    _make_chat_available(monkeypatch)
    app.action_goto_area("chat")
    await pilot.pause()
    chat_input = app.query_one("#chat-input", Input)
    chat_input.focus()
    await pilot.press(*"最初の質問")
    await pilot.press("enter")
    await pilot.pause()
    chat_input.focus()
    await pilot.press(*"次の質問")
    await pilot.press("enter")
    await pilot.pause()
    log_text = str(app.query_one("#chat-log", Static).render())
    assert "回答: 次の質問" in log_text
    assert "最初の質問" in log_text  # 1往復目の履歴が消えていない


async def _tui_visualization_validate(
    app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    app.action_goto_area("visualization")
    await pilot.pause()
    app.query_one("#visualization-validate", Button)


async def _tui_visualization_submit_render(
    app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    app.action_goto_area("visualization")
    await pilot.pause()
    app.query_one("#visualization-render", Button)


async def _tui_visualization_deps(
    app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    app.action_goto_area("visualization")
    await pilot.pause()
    app.query_one("#visualization-deps", Static)


async def _tui_quality_run_integrity(
    app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    app.action_goto_area("quality")
    await pilot.pause()
    app.query_one("#quality-integrity", Button)


async def _tui_quality_run_duplicates(
    app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    app.action_goto_area("quality")
    await pilot.pause()
    app.query_one("#quality-duplicates", Button)


async def _tui_quality_run_contradictions(
    app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    app.action_goto_area("quality")
    await pilot.pause()
    app.query_one("#quality-contradictions", Button)


async def _tui_quality_run_backfill_metadata(
    app: KbApp, pilot: Any, _c: ServiceContainer, _m: MonkeyPatch
) -> None:
    app.action_goto_area("quality")
    await pilot.pause()
    app.query_one("#quality-backfill", Button)


TUI_CHECKS: dict[str, TuiCheck] = {
    "source_remove": _tui_source_remove,
    "source_test_connection": _tui_source_test_connection,
    "batch_remove": _tui_batch_remove,
    "batch_run": _tui_batch_run,
    "job_cancel": _tui_job_cancel,
    "job_retry": _tui_job_retry,
    "document_update_metadata": _tui_document_update_metadata,
    "document_delete": _tui_document_delete,
    "search_run": _tui_search_run,
    "chat_start": _tui_chat_start,
    "chat_ask": _tui_chat_ask,
    "chat_history": _tui_chat_history,
    "visualization_validate": _tui_visualization_validate,
    "visualization_submit_render": _tui_visualization_submit_render,
    "visualization_deps": _tui_visualization_deps,
    "quality_run_integrity": _tui_quality_run_integrity,
    "quality_run_duplicates": _tui_quality_run_duplicates,
    "quality_run_contradictions": _tui_quality_run_contradictions,
    "quality_run_backfill_metadata": _tui_quality_run_backfill_metadata,
}


def test_every_tui_action_has_a_reachability_check() -> None:
    missing = missing_registry_entries(THE_MATRIX, TUI_CHECKS, "tui")
    assert missing == [], f"TUI 到達性チェックが未登録の操作: {missing}"


@pytest.mark.parametrize("action_id", actions_for_surface(THE_MATRIX, "tui"))
async def test_tui_action_is_reachable_from_the_screen(
    action_id: str, container: ServiceContainer, monkeypatch: MonkeyPatch
) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await TUI_CHECKS[action_id](app, pilot, container, monkeypatch)


# TUI の破壊的操作:「いいえ (n)」で副作用が無いこと。
TuiDeclineCheck = Callable[["KbApp", Any, ServiceContainer], Awaitable[None]]


async def _tui_decline_source_remove(app: KbApp, pilot: Any, container: ServiceContainer) -> None:
    source = container.sources.add(
        type="web",
        display_name="TUI拒否テスト用ソース",
        connection={"url": "https://example.invalid"},
        output_dir="docs/decline-test",
    )
    app.action_goto_area("sources_batches")
    await pilot.pause()
    app.remove_source(source["id"])
    await pilot.pause()
    await pilot.press("n")
    await pilot.pause()
    assert any(s["id"] == source["id"] for s in container.sources.list())


async def _tui_decline_batch_remove(app: KbApp, pilot: Any, container: ServiceContainer) -> None:
    batch = container.batches.add(name="TUI拒否テスト用バッチ", type="web", items=[])
    app.action_goto_area("sources_batches")
    await pilot.pause()
    app.remove_batch(batch["id"])
    await pilot.pause()
    await pilot.press("n")
    await pilot.pause()
    assert any(b["id"] == batch["id"] for b in container.batches.list())


async def _tui_decline_job_cancel(app: KbApp, pilot: Any, container: ServiceContainer) -> None:
    job = JobRepository(container.conn).submit("noop", {})
    app.detail = ("job", job.id)
    app.current_area = "jobs"
    app.render_area("jobs")
    await pilot.pause()
    app.cancel_job(job.id)
    await pilot.pause()
    await pilot.press("n")
    await pilot.pause()
    assert container.jobs.get(job.id).state == JobState.QUEUED


async def _tui_decline_document_delete(app: KbApp, pilot: Any, container: ServiceContainer) -> None:
    container.documents.upsert(
        {"path": "tui-decline-test-keep.md", "source": "manual", "status": "active"}
    )
    audit_before = container.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    app.detail = ("document", "tui-decline-test-keep.md")
    app.current_area = "documents"
    app.render_area("documents")
    await pilot.pause()
    app.delete_document("tui-decline-test-keep.md")
    await pilot.pause()
    await pilot.press("n")
    await pilot.pause()
    assert container.documents.get_or_none("tui-decline-test-keep.md") is not None
    audit_after = container.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    assert audit_after == audit_before


TUI_DECLINE_CHECKS: dict[str, TuiDeclineCheck] = {
    "source_remove": _tui_decline_source_remove,
    "batch_remove": _tui_decline_batch_remove,
    "job_cancel": _tui_decline_job_cancel,
    "document_delete": _tui_decline_document_delete,
}


def test_every_destructive_tui_action_has_a_decline_check() -> None:
    missing = missing_registry_entries(THE_MATRIX, TUI_DECLINE_CHECKS, "tui")
    destructive = set(destructive_actions(THE_MATRIX))
    missing_destructive = [a for a in missing if a in destructive]
    assert missing_destructive == [], (
        f"破壊的 TUI 操作で「いいえ」検査が未登録: {missing_destructive}"
    )


@pytest.mark.parametrize("action_id", destructive_actions(THE_MATRIX))
async def test_destructive_tui_action_declines_without_side_effect(
    action_id: str, container: ServiceContainer
) -> None:
    check = TUI_DECLINE_CHECKS.get(action_id)
    if check is None:
        pytest.skip(f"{action_id} は TUI 面を宣言していない")
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await check(app, pilot, container)


# ---------------------------------------------------------------------------
# Step 4: 非空虚性の確認。「操作を1つ意図的に外してテストが落ちることを確認する。
# 落ちなければテストが何も検査していない。」(brief より)
#
# 2段の証明にする:
#   (a) 構造レベル: マトリクスが要求する操作が registry から1件消えたら、
#       `missing_registry_entries` がそれを検出すること(上のカバレッジテスト
#       群が実際に使っている判定関数そのもの)。
#   (b) 実行レベル: 到達性チェックが使う一次アサーション
#       (`user.find`/`app.query_one`/`_assert_route_registered`)が、
#       実在しないボタン・ID・ルートに対して本当に失敗すること
#       (=これらのチェックは「常に緑」ではない)。
# ---------------------------------------------------------------------------


def test_registry_completeness_check_is_non_vacuous_for_api() -> None:
    mutated = dict(API_ROUTE_BY_ACTION)
    dropped = next(iter(mutated))
    del mutated[dropped]
    missing = missing_registry_entries(THE_MATRIX, mutated, "api")
    assert missing == [dropped]


def test_registry_completeness_check_is_non_vacuous_for_web() -> None:
    mutated = dict(WEB_CHECKS)
    dropped = next(iter(mutated))
    del mutated[dropped]
    missing = missing_registry_entries(THE_MATRIX, mutated, "web")
    assert missing == [dropped]


def test_registry_completeness_check_is_non_vacuous_for_tui() -> None:
    mutated = dict(TUI_CHECKS)
    dropped = next(iter(mutated))
    del mutated[dropped]
    missing = missing_registry_entries(THE_MATRIX, mutated, "tui")
    assert missing == [dropped]


def test_api_route_reachability_assertion_fails_for_a_dropped_route(client: TestClient) -> None:
    """`batch_run` の実装が消えた(=ルートが登録されなくなった)状況を模して、
    到達性チェックが本当に失敗を返すことを確認する。"""
    with pytest.raises(AssertionError):
        _assert_route_registered(client, "POST", "/api/v1/does-not-exist/action")


async def test_web_reachability_assertion_fails_for_a_dropped_button(
    user: User, wired_container: ServiceContainer
) -> None:
    """既存の品質画面から、存在しないラベルを探させて本当に失敗することを確認する
    (= `_web_quality_run_integrity` 等が使う一次アサーションは空虚ではない)。"""
    await user.open("/quality")
    with pytest.raises(AssertionError):
        user.find("この操作は存在しません_contract_test_placeholder")


async def test_tui_reachability_assertion_fails_for_a_dropped_widget(
    container: ServiceContainer,
) -> None:
    app = KbApp(container, start_worker=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.action_goto_area("quality")
        await pilot.pause()
        with pytest.raises(NoMatches):
            app.query_one("#quality-does-not-exist", Button)
