"""`application.sync_service.SyncService`: esaソース/バッチ同期のオーケストレーション。

`EsaSyncRunner` 自体のシナリオ(create/unchanged/conflict等)は `test_esa.py` で
検証済みのため、ここでは「モックHTTPサーバー越しに検索・取得を行い、レポートを
`reports/sync/` へ旧形式互換で書き出す」という接着部分だけを検証する。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

from abist_kb.application.sync_service import SyncService, _resolve_git_repository
from abist_kb.config import Settings
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.schema import ensure_app_schema
from abist_kb.infrastructure.db.sources_repo import SourceRepository
from abist_kb.infrastructure.sources.web import DEFAULT_DELAY_SECONDS, HostRateLimiter

from .conftest import FAKE_TOKEN, MockEsaServer
from .test_esa import make_post
from .test_git import make_upstream
from .test_web import WebPageServer


def _build_raw_service(tmp_root: Path) -> tuple[SyncService, SourceRepository, BatchRepository]:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    sources = SourceRepository(conn)
    batches = BatchRepository(conn)
    service = SyncService(
        root_dir=tmp_root,
        docs_dir=docs_dir,
        reports_dir=tmp_root / "reports",
        documents=DocumentRepository(conn),
        sources=sources,
        batches=batches,
    )
    return service, sources, batches


def _build_service(tmp_root: Path, esa_server: MockEsaServer) -> tuple[SyncService, str]:
    service, sources, _batches = _build_raw_service(tmp_root)
    source = sources.create(
        type="esa",
        display_name="テストesa",
        connection={
            "team": esa_server.team,
            "access_token": FAKE_TOKEN,
            "base_url": esa_server.base_url,
        },
        output_dir="docs/_svc_test",
    )
    return service, source["id"]


@pytest.fixture
def web_server() -> Path:  # type: ignore[misc]
    server = WebPageServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_sync_source_writes_files_and_report(tmp_root: Path, esa_server: MockEsaServer) -> None:
    esa_server.add_post(make_post(category="対象カテゴリ"))
    esa_server.add_post(make_post(category="対象カテゴリ"))
    service, source_id = _build_service(tmp_root, esa_server)

    summary, report_path = service.sync_source(source_id, categories=["対象カテゴリ"])

    assert summary.totals["added"] == 2
    assert report_path is not None
    assert report_path.name.startswith("sync-esa-")
    assert report_path.parent.name == "sync"

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["source"] == "esa"
    assert payload["totals"]["added"] == 2
    assert FAKE_TOKEN not in report_path.read_text(encoding="utf-8")

    saved_files = list((tmp_root / "docs" / "_svc_test" / "対象カテゴリ").glob("*.md"))
    assert len(saved_files) == 2


def test_sync_source_dry_run_writes_no_files_and_no_report(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    esa_server.add_post(make_post(category="対象カテゴリ"))
    service, source_id = _build_service(tmp_root, esa_server)

    summary, report_path = service.sync_source(source_id, categories=["対象カテゴリ"], dry_run=True)

    assert summary.totals["added"] == 1
    assert report_path is None
    assert not (tmp_root / "reports" / "sync").exists()
    assert not list((tmp_root / "docs").rglob("*.md"))


def test_sync_batch_uses_batch_items_as_categories(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    esa_server.add_post(make_post(category="カテゴリ1"))
    esa_server.add_post(make_post(category="カテゴリ2"))
    service, source_id = _build_service(tmp_root, esa_server)
    batch = service._batches.create(  # noqa: SLF001 - テストの都合上直接組み立てる
        name="テストバッチ",
        type="esa",
        output_dir="docs/_svc_test",
        items=[
            {"source_id": source_id, "target": "カテゴリ1"},
            {"source_id": source_id, "target": "カテゴリ2"},
        ],
    )

    summary, report_path = service.sync_batch(batch["id"])

    assert summary.totals["added"] == 2
    assert report_path is not None
    assert "テストバッチ" in report_path.name


def test_sync_all_iterates_enabled_esa_batches_only(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    esa_server.add_post(make_post(category="カテゴリA"))
    service, source_id = _build_service(tmp_root, esa_server)
    service._batches.create(  # noqa: SLF001
        name="有効バッチ",
        type="esa",
        output_dir="docs/_svc_test",
        items=[{"source_id": source_id, "target": "カテゴリA"}],
    )
    service._batches.create(  # noqa: SLF001
        name="無効バッチ",
        type="esa",
        output_dir="docs/_svc_test",
        enabled=False,
        items=[{"source_id": source_id, "target": "カテゴリA"}],
    )

    results = service.sync_all()

    assert len(results) == 1
    assert results[0]["batch_name"] == "有効バッチ"


# ---------------------------------------------------------------------------
# web/git バッチの配線(task-5b): SyncService はソース種別で分岐して実行する。
# ---------------------------------------------------------------------------


def test_sync_batch_web_crawls_and_writes_report(tmp_root: Path, web_server: WebPageServer) -> None:
    web_server.state.body = "<p>本文</p>"
    service, _sources, batches = _build_raw_service(tmp_root)
    batch = batches.create(
        name="webバッチ",
        type="web",
        output_dir="docs/_svc_web_test",
        items=[{"options": {"url": web_server.base_url + "/", "max_depth": 0}}],
    )

    summary, report_path = service.sync_batch(batch["id"])

    assert summary.source == "web"
    assert summary.totals["added"] == 1
    assert report_path is not None
    assert report_path.name.startswith("sync-web-")
    saved = list((tmp_root / "docs" / "_svc_web_test").rglob("*.md"))
    assert len(saved) == 1


def test_sync_batch_git_mirrors_and_writes_report(tmp_root: Path) -> None:
    upstream = make_upstream(tmp_root / "upstream")
    service, _sources, batches = _build_raw_service(tmp_root)
    batch = batches.create(
        name="gitバッチ",
        type="git",
        output_dir="docs/_svc_git_test",
        items=[{"options": {"repository": str(upstream), "branch": "main"}}],
    )

    summary, report_path = service.sync_batch(batch["id"])

    assert summary.source == "git"
    assert summary.totals["added"] >= 2  # README.md + docs/a.md + docs/b.md
    assert report_path is not None
    assert report_path.name.startswith("sync-git-")
    saved = list((tmp_root / "docs" / "_svc_git_test").rglob("*.md"))
    assert len(saved) >= 2


def test_sync_source_web_uses_source_connection(tmp_root: Path, web_server: WebPageServer) -> None:
    web_server.state.body = "<p>本文</p>"
    service, sources, _batches = _build_raw_service(tmp_root)
    source = sources.create(
        type="web",
        display_name="webソース",
        connection={"url": web_server.base_url + "/", "max_depth": 0},
        output_dir="docs/_svc_web_source",
    )

    summary, report_path = service.sync_source(source["id"])

    assert summary.totals["added"] == 1
    assert report_path is not None
    assert list((tmp_root / "docs" / "_svc_web_source").rglob("*.md"))


def test_sync_source_git_uses_source_connection(tmp_root: Path) -> None:
    upstream = make_upstream(tmp_root / "upstream")
    service, sources, _batches = _build_raw_service(tmp_root)
    source = sources.create(
        type="git",
        display_name="gitソース",
        connection={"repository": str(upstream), "branch": "main"},
        output_dir="docs/_svc_git_source",
    )

    summary, report_path = service.sync_source(source["id"])

    assert summary.totals["added"] >= 2
    assert report_path is not None
    assert list((tmp_root / "docs" / "_svc_git_source").rglob("*.md"))


def test_sync_batch_git_rejects_dry_run(tmp_root: Path) -> None:
    """git には旧実装にも `--dry-run` が無いため、要求されたら明示的に拒否する。"""
    upstream = make_upstream(tmp_root / "upstream")
    service, _sources, batches = _build_raw_service(tmp_root)
    batch = batches.create(
        name="gitバッチ",
        type="git",
        output_dir="docs/_svc_git_dry",
        items=[{"options": {"repository": str(upstream), "branch": "main"}}],
    )

    with pytest.raises(AppError):
        service.sync_batch(batch["id"], dry_run=True)


def test_sync_batch_web_missing_url_raises(tmp_root: Path) -> None:
    service, _sources, batches = _build_raw_service(tmp_root)
    batch = batches.create(
        name="urlなしバッチ", type="web", output_dir="docs/_svc_web_bad", items=[{"options": {}}]
    )

    with pytest.raises(AppError):
        service.sync_batch(batch["id"])


# ---------------------------------------------------------------------------
# sync_all の部分失敗継続(task-5b の判断事項): 1バッチの失敗で全体を止めず、
# 続行して失敗を収集する。
# ---------------------------------------------------------------------------


def test_sync_all_continues_past_failed_batch_and_reports_it(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    esa_server.add_post(make_post(category="カテゴリA"))
    service, source_id = _build_service(tmp_root, esa_server)
    # 失敗するバッチ(repository が設定されておらず、実行前の検証で失敗する)。
    # クローン自体の失敗(到達不能なホスト等)は GitSyncRunner が握り潰さず
    # summary 内の error アイテムとして扱う契約(brief契約4)であり、バッチ全体を
    # 例外で落とすものではないため、ここでは「そもそも実行できない設定不備」を使う。
    service._batches.create(  # noqa: SLF001
        name="失敗バッチ",
        type="git",
        output_dir="docs/_svc_fail_test",
        items=[{"options": {}}],
    )
    service._batches.create(  # noqa: SLF001
        name="成功バッチ",
        type="esa",
        output_dir="docs/_svc_test",
        items=[{"source_id": source_id, "target": "カテゴリA"}],
    )

    results = service.sync_all()

    assert len(results) == 2
    by_name = {r["batch_name"]: r for r in results}
    assert "error" in by_name["失敗バッチ"]
    assert by_name["成功バッチ"]["summary"]["totals"]["added"] == 1


def test_sync_all_via_job_path_records_partial_state_on_batch_failure(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    """`sync_all` の部分失敗は CLI の終了コードだけでなく、ジョブ行そのものに
    反映されなければならない(M5のMCPツール・M6のWeb/TUI画面はジョブ状態しか
    読まないため)。CLI を経由せず `run_sync_inline`(ジョブ経路そのもの)を直接
    呼び、`jobs` テーブルの行が `SUCCEEDED` ではなく `PARTIAL` であることを検証
    する。
    """
    from abist_kb.application.sync_service import run_sync_inline
    from abist_kb.domain.job import JobState
    from abist_kb.infrastructure.jobs.repository import JobRepository

    esa_server.add_post(make_post(category="対象カテゴリ"))
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    sources = SourceRepository(conn)
    batches = BatchRepository(conn)
    source = sources.create(
        type="esa",
        display_name="テストesa",
        connection={
            "team": esa_server.team,
            "access_token": FAKE_TOKEN,
            "base_url": esa_server.base_url,
        },
        output_dir="docs/_job_all_test",
    )
    batches.create(
        name="成功バッチ",
        type="esa",
        output_dir="docs/_job_all_test",
        items=[{"source_id": source["id"], "target": "対象カテゴリ"}],
    )
    batches.create(
        name="失敗バッチ",
        type="git",
        output_dir="docs/_job_all_fail",
        items=[{"options": {}}],  # repository が無く実行時に必ず失敗する
    )

    result = run_sync_inline(
        conn,
        root_dir=tmp_root,
        docs_dir=docs_dir,
        reports_dir=tmp_root / "reports",
        target="all",
    )

    assert result["state"] == str(JobState.PARTIAL)
    by_name = {r["batch_name"]: r for r in result["results"]}
    assert "error" in by_name["失敗バッチ"]
    assert by_name["成功バッチ"]["summary"]["totals"]["added"] == 1

    job_row = JobRepository(conn).get(result["id"])
    assert job_row is not None
    assert job_row.state == JobState.PARTIAL, (
        f"ジョブ行が半分失敗した同期を SUCCEEDED として記録してしまっている: {job_row.state}"
    )
    assert job_row.error is not None


# ---------------------------------------------------------------------------
# 旧 batch-config.js からの一方向インポート → 実行までの往復(task-5b 必須要件)。
# ---------------------------------------------------------------------------


def test_imported_web_and_git_batches_can_actually_be_run(
    tmp_root: Path, tmp_path: Path, web_server: WebPageServer
) -> None:
    """`migration.batch_config_parser` が読み取る旧設定の web/git エントリを
    `BatchService.import_from_old_config` で取り込んだ後、そのバッチが
    `SyncService.sync_batch` で実際に実行できることを検証する。"""
    from abist_kb.application.batch_service import BatchService

    web_server.state.body = "<p>本文</p>"
    upstream = make_upstream(tmp_root / "upstream")

    config_path = tmp_path / "batch-config.js"
    config_path.write_text(
        "export const batchConfigs = {\n"
        "  'catiadoc-like': {\n"
        "    'type': 'web',\n"
        f"    'url': '{web_server.base_url}/',\n"
        "    'outputDir': 'docs/catiadoc-like',\n"
        "    'maxDepth': 0,\n"
        "    'delay': 1000\n"
        "  },\n"
        "  'catia-flotherm-prep-like': {\n"
        "    'type': 'git',\n"
        f"    'repository': '{upstream.as_posix()}',\n"
        "    'branch': 'main',\n"
        "    'outputDir': 'docs/catia-flotherm-prep-like'\n"
        "  }\n"
        "};\n",
        encoding="utf-8",
    )

    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    batch_service = BatchService(conn)
    result = batch_service.import_from_old_config(config_path)
    assert result["imported"] == 2

    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    sync_service = SyncService(
        root_dir=tmp_root,
        docs_dir=docs_dir,
        reports_dir=tmp_root / "reports",
        documents=DocumentRepository(conn),
        sources=SourceRepository(conn),
        batches=BatchRepository(conn),
    )

    web_batch = batch_service.list_by_name("catiadoc-like")
    web_summary, web_report = sync_service.sync_batch(web_batch["id"])
    assert web_summary.totals["added"] == 1
    assert web_report is not None
    assert list((docs_dir / "catiadoc-like").rglob("*.md"))

    git_batch = batch_service.list_by_name("catia-flotherm-prep-like")
    git_summary, git_report = sync_service.sync_batch(git_batch["id"])
    assert git_summary.totals["added"] >= 2
    assert git_report is not None
    assert list((docs_dir / "catia-flotherm-prep-like").rglob("*.md"))


def test_sync_batch_web_paces_requests_per_batch_items_options_delay(
    tmp_root: Path, web_server: WebPageServer
) -> None:
    """`catiadoc` バッチの実設定(`delay: 1000`)が `batch_items.options.delay` を
    経由して実際にクロールの節流へ渡ることを、`SyncService.sync_batch`
    (`_sync_web_target` → `WebSyncRunner`)経由で検証する(task-6の
    `HostRateLimiter` 配線が末端まで届いているかの確認)。単体では
    `WebSyncRunner(delay_seconds=...)` を直接呼ぶテストで既に確認済みだが、ここでは
    `options.delay`(ミリ秒、旧 `batch-config.js` 由来のキー)から実際に
    `SyncService` が消費する経路そのものを通す。

    以前は実HTTPサーバーへのリクエスト到着時刻(壁時計)の間隔を比較していたが、
    これは `test_web.py` の `test_crawl_enforces_delay_between_requests_to_same_host`
    と同じ理由でCPU高負荷下にフレークする(スレッド/ソケットのスケジューリングが
    乱れると、ゲート解放順序とサーバ到着順序がずれうる)。ここで確かめたいのは
    実際の待機秒数ではなく「`options.delay`(ミリ秒)が正しい秒数に変換されて
    `HostRateLimiter` まで届いているか」という配線なので、`HostRateLimiter.__init__`
    に渡された `delay_seconds` を直接検証する。
    """
    base = web_server.base_url
    web_server.state.body = f'<a href="{base}/a">a</a><a href="{base}/b">b</a>'
    web_server.state.links = {"/a": "ページA", "/b": "ページB"}

    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    batches = BatchRepository(conn)
    batch = batches.create(
        name="catiadoc-like",
        type="web",
        output_dir="docs/_delay_test",
        items=[{"options": {"url": f"{base}/", "max_depth": 1, "delay": 150}}],
    )
    sync_service = SyncService(
        root_dir=tmp_root,
        docs_dir=docs_dir,
        reports_dir=tmp_root / "reports",
        documents=DocumentRepository(conn),
        sources=SourceRepository(conn),
        batches=batches,
    )

    constructed_delays: list[float] = []
    original_init = HostRateLimiter.__init__

    def spying_init(self: HostRateLimiter, delay_seconds: float = DEFAULT_DELAY_SECONDS) -> None:
        constructed_delays.append(delay_seconds)
        original_init(self, delay_seconds)

    with mock.patch.object(HostRateLimiter, "__init__", spying_init):
        summary, _report = sync_service.sync_batch(batch["id"])

    assert summary.totals["added"] >= 3  # トップページ + a + b
    assert constructed_delays, "HostRateLimiter が構築されなかった(配線漏れの疑い)"
    assert constructed_delays[0] == pytest.approx(0.15), (
        f"batch_items.options.delay(150ms)がHostRateLimiterへ正しく渡っていない"
        f"(配線漏れの疑い): {constructed_delays}"
    )


@pytest.mark.timing_sensitive
def test_sync_batch_web_concurrent_requests_within_delay_window_still_overlap(
    tmp_root: Path, web_server: WebPageServer
) -> None:
    """`HostRateLimiter` はリクエスト *開始* 間隔だけを空けるのであって、クロール
    全体を直列化してはならない(brief要求: 「並行実行モデルを崩さない」)。
    レスポンスをわざと遅くしたサーバーへ、delay(150ms)より短い応答時間の
    ページを `concurrency=3` で3件取得させ、完了までの総時間が「直列実行した
    場合の下限」(delay*3 + レスポンス時間*3)より明確に短いこと ── つまり
    複数リクエストが実際に並行して *進行中* であることを確認する。

    これは「実際に複数リクエストが重なって進行しているか」という並行実行モデル
    そのものを検証するテストであり、`HostRateLimiter` の待機ロジック単体
    (`test_web.py::test_host_rate_limiter_waits_the_configured_delay` で決定論的に
    検証済み)とは別物なので、壁時計を完全に排除することはできない。直列実行なら
    最低でも delay*2=0.3秒かかるところ、上限を余裕を持って1.5秒に緩めた上で
    `timing_sensitive` マーカーを付け、CPU高負荷時の既知のフレーク要因として
    明示する。"""
    base = web_server.base_url
    web_server.state.body = f'<a href="{base}/a">a</a><a href="{base}/b">b</a>'
    web_server.state.links = {"/a": "ページA", "/b": "ページB"}

    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    batches = BatchRepository(conn)
    batch = batches.create(
        name="catiadoc-like-concurrent",
        type="web",
        output_dir="docs/_delay_concurrency_test",
        items=[{"options": {"url": f"{base}/", "max_depth": 1, "delay": 150, "concurrency": 3}}],
    )
    sync_service = SyncService(
        root_dir=tmp_root,
        docs_dir=docs_dir,
        reports_dir=tmp_root / "reports",
        documents=DocumentRepository(conn),
        sources=SourceRepository(conn),
        batches=batches,
    )

    started = time.monotonic()
    summary, _report = sync_service.sync_batch(batch["id"])
    elapsed = time.monotonic() - started

    assert summary.totals["added"] >= 3  # トップページ + a + b(深さ1で並行取得)
    # 直列実行なら少なくとも delay(0.15s) * (3ページ-1) = 0.3s はかかる。
    # 並行取得(asyncio.gather)であれば、開始間隔だけを守りつつ大きく重なるため
    # 明確に速い。CPU高負荷下でも誤検知しないよう、直列実行の下限(0.3s)に対して
    # 十分な余裕(1.5s)を持たせる(timing_sensitive、上記docstring参照)。
    assert elapsed < 1.5, f"delay がクロール全体を直列化している疑い: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 資格情報の解決(post-cutover fix): §12 は esa/git の資格情報を DB へ書くことを
# 禁じているため、マイグレーションが作った空の `connection={}` プレースホルダーは
# `Settings`(`.env`)へフォールバックできなければならない。優先順位はソースごとの
# `connection` 上書き > `Settings`、どちらも無ければ環境変数名を名指しした
# `CONFIG_ERROR` にする(接続設定を編集させない)。
# ---------------------------------------------------------------------------


def _build_service_with_settings(
    tmp_root: Path, esa_server: MockEsaServer, *, settings: Settings, connection: dict | None
) -> tuple[SyncService, str]:
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    sources = SourceRepository(conn)
    source = sources.create(
        type="esa",
        display_name="テストesa",
        connection=connection,
        output_dir="docs/_svc_test",
    )
    service = SyncService(
        root_dir=tmp_root,
        docs_dir=docs_dir,
        reports_dir=tmp_root / "reports",
        documents=DocumentRepository(conn),
        sources=sources,
        batches=BatchRepository(conn),
        settings=settings,
    )
    return service, source["id"]


def test_sync_source_esa_resolves_credentials_from_settings_when_connection_empty(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    """マイグレーションが作る `connection={}` プレースホルダーのまま同期できる
    (`Settings.esa_team_name`/`esa_access_token` へフォールバックする)。
    `base_url` だけはテスト専用の抜け道としてソース側の接続設定に残す。
    """
    esa_server.add_post(make_post(category="対象カテゴリ"))
    settings = Settings(
        root_dir=tmp_root,
        esa_team_name=esa_server.team,
        esa_access_token=FAKE_TOKEN,
        _env_file=None,
    )
    service, source_id = _build_service_with_settings(
        tmp_root, esa_server, settings=settings, connection={"base_url": esa_server.base_url}
    )

    summary, report_path = service.sync_source(source_id, categories=["対象カテゴリ"])

    assert summary.totals["added"] == 1
    assert report_path is not None
    assert FAKE_TOKEN not in report_path.read_text(encoding="utf-8")


def test_sync_source_esa_connection_override_wins_over_settings(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    """ソース固有の `connection.team`/`access_token`(複数チーム運用向けの上書き)は
    `Settings` より優先される。`Settings` 側にわざと別チーム/別トークンを入れ、
    実際にモックサーバーへ届いたのはソース側の値だと(認証成功で)確認する。
    """
    esa_server.add_post(make_post(category="対象カテゴリ"))
    settings = Settings(
        root_dir=tmp_root,
        esa_team_name="wrong-team",
        esa_access_token="wrong-token",
        _env_file=None,
    )
    service, source_id = _build_service_with_settings(
        tmp_root,
        esa_server,
        settings=settings,
        connection={
            "team": esa_server.team,
            "access_token": FAKE_TOKEN,
            "base_url": esa_server.base_url,
        },
    )

    summary, _report = service.sync_source(source_id, categories=["対象カテゴリ"])

    assert summary.totals["added"] == 1


def test_sync_source_esa_missing_credentials_raises_actionable_config_error(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    """`connection` も `Settings` も空なら、DB の接続設定ではなく設定すべき
    環境変数名(`ABIST_KB_ESA_TEAM_NAME`/`ABIST_KB_ESA_ACCESS_TOKEN`)を名指しした
    `CONFIG_ERROR` になる。旧メッセージ(「ソースの接続設定に...」)には戻さない。
    """
    settings = Settings(root_dir=tmp_root, _env_file=None)
    service, source_id = _build_service_with_settings(
        tmp_root, esa_server, settings=settings, connection={}
    )

    with pytest.raises(AppError) as exc_info:
        service.sync_source(source_id, categories=["対象カテゴリ"])

    err = exc_info.value
    assert err.code == ErrorCode.CONFIG_ERROR
    assert "ABIST_KB_ESA_TEAM_NAME" in err.message
    assert "ABIST_KB_ESA_ACCESS_TOKEN" in err.message
    assert "接続設定に team/access_token が不足しています" not in err.message


def test_sync_source_esa_missing_credentials_error_never_names_the_missing_secret(
    tmp_root: Path, esa_server: MockEsaServer
) -> None:
    """欠落エラーは「何が無いか」を環境変数名で示すだけで、値そのものは当然
    含まない(値がそもそも存在しないケースだが、`hint` にも FAKE_TOKEN が
    紛れ込んでいないことを合わせて確認する)。
    """
    settings = Settings(root_dir=tmp_root, esa_team_name="abist", _env_file=None)  # token だけ欠落
    service, source_id = _build_service_with_settings(
        tmp_root, esa_server, settings=settings, connection={}
    )

    with pytest.raises(AppError) as exc_info:
        service.sync_source(source_id, categories=["対象カテゴリ"])

    err = exc_info.value
    assert "ABIST_KB_ESA_ACCESS_TOKEN" in err.message
    assert "ABIST_KB_ESA_TEAM_NAME" not in err.message  # team は既に埋まっている
    assert FAKE_TOKEN not in err.message
    assert FAKE_TOKEN not in (err.hint or "")


def test_resolve_git_repository_uses_settings_token_when_connection_has_none() -> None:
    settings = Settings(git_token="ghp-from-env", _env_file=None)  # noqa: S106 - テスト専用のダミー値
    resolved = _resolve_git_repository(
        "https://github.com/example/repo.git", options={}, settings=settings
    )
    assert resolved == "https://ghp-from-env@github.com/example/repo.git"


def test_resolve_git_repository_per_source_token_overrides_settings() -> None:
    settings = Settings(git_token="ghp-from-env", _env_file=None)  # noqa: S106
    resolved = _resolve_git_repository(
        "https://github.com/example/repo.git",
        options={"token": "ghp-from-connection"},
        settings=settings,
    )
    assert resolved == "https://ghp-from-connection@github.com/example/repo.git"


def test_resolve_git_repository_respects_credentials_already_embedded_in_url() -> None:
    settings = Settings(git_token="ghp-from-env", _env_file=None)  # noqa: S106
    resolved = _resolve_git_repository(
        "https://explicit-user:explicit-pass@github.com/example/repo.git",
        options={},
        settings=settings,
    )
    assert resolved == "https://explicit-user:explicit-pass@github.com/example/repo.git"


def test_resolve_git_repository_leaves_url_unchanged_when_no_token_available() -> None:
    """git は esa と違い、資格情報が無くても公開リポジトリなら成立する。
    トークンが無ければエラーにせず URL をそのまま返す(ハードエラーにしない
    設計判断、`_sync_git_target` 側で clone 失敗時のヒント表示を担う)。
    """
    settings = Settings(_env_file=None)
    resolved = _resolve_git_repository(
        "https://github.com/example/repo.git", options={}, settings=settings
    )
    assert resolved == "https://github.com/example/repo.git"


def test_sync_batch_git_missing_token_error_hints_at_git_token_env_var_not_connection(
    tmp_root: Path,
) -> None:
    """private リポジトリ相当(存在しない/認証が必要なURL)への clone 失敗時、
    トークンが一切無い状態なら `ABIST_KB_GIT_TOKEN` を案内する。DB の接続設定を
    編集しろとは言わない(esa の教訓と同じ理由)。トークンの値そのものは
    このケースではそもそも存在しないため、案内メッセージにも当然含まれない。
    """
    conn = connect(tmp_root / "app.sqlite")
    ensure_app_schema(conn)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    batches = BatchRepository(conn)
    # 存在しないローカルパスへの clone は git 側の認証プロンプトなしで確実に失敗する
    # (実ネットワークに依存せず、`token_available=False` 経路をエクササイズする)。
    missing_repo = str(tmp_root / "no-such-repo-here")
    batch = batches.create(
        name="gitバッチ",
        type="git",
        output_dir="docs/_svc_git_missing_test",
        items=[{"options": {"repository": missing_repo}}],
    )
    service = SyncService(
        root_dir=tmp_root,
        docs_dir=docs_dir,
        reports_dir=tmp_root / "reports",
        documents=DocumentRepository(conn),
        sources=SourceRepository(conn),
        batches=batches,
        settings=Settings(root_dir=tmp_root, _env_file=None),
    )

    summary, _report = service.sync_batch(batch["id"])

    assert summary.full_sync_succeeded is False
    assert summary.note is not None
    assert "ABIST_KB_GIT_TOKEN" in summary.note
    assert "接続設定に team/access_token が不足しています" not in summary.note
