"""`infrastructure.sources.web`: 旧 `download-web.js` / `test/sync-web.test.js` の移植。

`tests/fixtures/PROVENANCE.md` が明示する通り `test/sync-web.test.js` のシナリオは
fixture化されておらず、このファイルでの pytest 再実装がその受け入れ基準そのものに
なる。各テストの docstring に対応する旧テスト名を残す。HTML→Markdown変換自体は
`tests/sources/test_html_to_md.py`(golden 15件、advisory契約)で別途検証する。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

from abist_kb.domain.frontmatter import parse_frontmatter
from abist_kb.domain.sync_policy import SyncAction, SyncStatus
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.sources.html_to_md import extract_links_from_markdown
from abist_kb.infrastructure.sources.web import (
    HostRateLimiter,
    SyncItem,
    WebClient,
    WebSyncRunner,
    resolve_web_path,
)

# ---------------------------------------------------------------------------
# 保存済み Markdown からのリンク抽出(304時の巡回継続に使う、旧テスト直接転記)
# ---------------------------------------------------------------------------


def test_extract_links_from_markdown_finds_same_domain_links_and_drops_fragment() -> None:
    """旧テスト『保存済み Markdown から同一ドメインのリンクを抽出する』。"""
    md = "\n".join(
        [
            "[内部](http://example.com/a)",
            "[別ドメイン](http://other.com/b)",
            "<http://example.com/c>",
            "[フラグメント付き](http://example.com/d#sec)",
            "![画像](http://example.com/img.png)",
        ]
    )
    links = extract_links_from_markdown(md, "http://example.com")
    assert "http://example.com/a" in links
    assert "http://example.com/c" in links
    assert "http://example.com/d" in links, "フラグメントを除去していない"
    assert not any("other.com" in link for link in links), "別ドメインを拾った"


def test_extract_links_from_markdown_returns_empty_when_no_links() -> None:
    """旧テスト『リンクが無ければ空配列』。"""
    assert extract_links_from_markdown("# 見出しだけ", "http://example.com") == []


# ---------------------------------------------------------------------------
# テスト用 HTTP サーバー(ETag・本文・ステータスを差し替えられる、旧 startServer 相当)
# ---------------------------------------------------------------------------


@dataclass
class ServerState:
    etag: str = '"v1"'
    title: str = "ページ"
    body: str = "<p>本文</p>"
    status: int = 200
    links: dict[str, str] = field(default_factory=dict)  # path -> html body fragment (追加リンク用)
    requests: list[dict] = field(default_factory=list)


class _Handler(BaseHTTPRequestHandler):
    server: WebPageServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        state = self.server.state
        state.requests.append(
            {"path": self.path, "headers": dict(self.headers), "time": time.monotonic()}
        )

        if self.path not in ("/", "/index.html", *state.links.keys()):
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>not found</body></html>")
            return

        if state.status != 200:
            self.send_response(state.status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>error</body></html>")
            return

        if self.headers.get("If-None-Match") == state.etag:
            self.send_response(304)
            self.send_header("ETag", state.etag)
            self.end_headers()
            return

        body = state.links.get(self.path, state.body)
        html = f"<html><head><title>{state.title}</title></head><body>{body}</body></html>"
        encoded = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("ETag", state.etag)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class WebPageServer:
    def __init__(self) -> None:
        self.state = ServerState()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"


@pytest.fixture
def web_server() -> Iterator[WebPageServer]:
    server = WebPageServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def sync_dirs(tmp_root: Path) -> tuple[Path, Path, str]:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir()
    return tmp_root, docs_dir, "docs/_test_sync_web"


def make_runner(
    documents: DocumentRepository,
    sync_dirs: tuple[Path, Path, str],
    base_domain: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    max_depth: int = 3,
    max_pages: int = 200,
    max_size_bytes: int = 5_000_000,
) -> WebSyncRunner:
    root_dir, docs_dir, output_dir = sync_dirs
    return WebSyncRunner(
        documents=documents,
        root_dir=root_dir,
        docs_dir=docs_dir,
        output_dir=output_dir,
        base_domain=base_domain,
        force=force,
        dry_run=dry_run,
        max_depth=max_depth,
        max_pages=max_pages,
        max_size_bytes=max_size_bytes,
    )


def read_raw(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def one_saved_file(docs_dir: Path, output_dir: str) -> Path:
    base = docs_dir.parent / output_dir
    files = [p for p in base.rglob("*.md")]
    assert len(files) == 1, files
    return files[0]


async def _sync_once(runner: WebSyncRunner, url: str) -> SyncItem:
    async with WebClient() as client:
        item, _ = await runner.sync_page(client, url)
    assert item is not None
    return item


# ---------------------------------------------------------------------------
# 受入条件1・3: 条件付きGETで未変更ページを上書きしない
# ---------------------------------------------------------------------------


def test_conditional_get_skips_unchanged_page_and_updates_on_etag_change(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    """旧テスト『条件付きGETで未変更ページを上書きしない(受入条件1・3)』。"""
    url = web_server.base_url + "/"
    runner = make_runner(documents, sync_dirs, web_server.base_url)

    first = asyncio.run(_sync_once(runner, url))
    assert first.action == str(SyncAction.CREATE)

    path = one_saved_file(sync_dirs[1], sync_dirs[2])
    content_after_first = read_raw(path)
    mtime_after_first = path.stat().st_mtime_ns
    assert "本文" in content_after_first
    assert "managed_by: web-sync" in content_after_first

    row = documents.get(path.relative_to(sync_dirs[1]).as_posix())
    assert row["etag"] == '"v1"'

    # --- 2回目: ETag一致で304 ---
    second = asyncio.run(_sync_once(runner, url))
    assert second.action == str(SyncAction.UNCHANGED)
    assert "304" in second.reason
    assert read_raw(path) == content_after_first
    assert path.stat().st_mtime_ns == mtime_after_first, "未変更なのに書き換えた"

    conditional_requests = [r for r in web_server.state.requests if "If-None-Match" in r["headers"]]
    assert conditional_requests, "If-None-Match を送っていない"

    # --- 3回目: 取得元が変わったら更新する ---
    web_server.state.etag = '"v2"'
    web_server.state.body = "<p>更新後の本文</p>"
    third = asyncio.run(_sync_once(runner, url))
    assert third.action == str(SyncAction.UPDATE)
    assert "更新後の本文" in read_raw(path)
    row2 = documents.get(path.relative_to(sync_dirs[1]).as_posix())
    assert row2["etag"] == '"v2"'


# ---------------------------------------------------------------------------
# 受入条件2: ローカル編集は自動上書きしない
# ---------------------------------------------------------------------------


def test_local_edit_with_remote_update_is_conflict_and_force_overrides(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    """旧テスト『ローカル編集があれば取得元更新でも上書きしない(受入条件2)』。"""
    url = web_server.base_url + "/"
    runner = make_runner(documents, sync_dirs, web_server.base_url)
    asyncio.run(_sync_once(runner, url))

    path = one_saved_file(sync_dirs[1], sync_dirs[2])
    edited = read_raw(path) + "\n\nローカル追記\n"
    path.write_bytes(edited.encode("utf-8"))

    web_server.state.etag = '"v2"'
    web_server.state.body = "<p>取得元も更新</p>"

    result = asyncio.run(_sync_once(runner, url))
    assert result.action == str(SyncAction.CONFLICT)
    assert read_raw(path) == edited, "ローカル編集を上書きした"
    row = documents.get(path.relative_to(sync_dirs[1]).as_posix())
    assert row["sync_status"] == str(SyncStatus.CONFLICT)

    # --force なら取得元を優先する
    forced_runner = make_runner(documents, sync_dirs, web_server.base_url, force=True)
    forced = asyncio.run(_sync_once(forced_runner, url))
    assert forced.action == str(SyncAction.CONFLICT_OVERWRITTEN)
    assert "取得元も更新" in read_raw(path)


# ---------------------------------------------------------------------------
# 受入条件3: HTTP失敗でローカルファイルを消さない
# ---------------------------------------------------------------------------


def test_http_failure_does_not_delete_local_file(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    """旧テスト『HTTP 失敗でローカルファイルを消さない(受入条件3)』。"""
    url = web_server.base_url + "/"
    runner = make_runner(documents, sync_dirs, web_server.base_url)
    asyncio.run(_sync_once(runner, url))

    path = one_saved_file(sync_dirs[1], sync_dirs[2])
    before = read_raw(path)

    web_server.state.status = 500
    result = asyncio.run(_sync_once(runner, url))

    assert path.exists(), "HTTP失敗でファイルが消えた"
    assert read_raw(path) == before, "HTTP失敗でファイルが変わった"
    assert result.action == str(SyncAction.ERROR)

    row = documents.get(path.relative_to(sync_dirs[1]).as_posix())
    assert row["sync_status"] != str(SyncStatus.SOURCE_MISSING), "HTTP失敗を削除扱いにした"
    assert "500" in (row["sync_error"] or "")


# ---------------------------------------------------------------------------
# brief必須要件: etag/last_modified/source_content_hash は書き込み時のみ永続化
# ---------------------------------------------------------------------------


def test_etag_is_not_persisted_on_conflict_skip(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    """スキップ(conflict)では etag を更新しない: 次回304が競合を隠さないようにする。"""
    url = web_server.base_url + "/"
    runner = make_runner(documents, sync_dirs, web_server.base_url)
    asyncio.run(_sync_once(runner, url))
    path = one_saved_file(sync_dirs[1], sync_dirs[2])
    path.write_bytes((read_raw(path) + "\n\n編集\n").encode("utf-8"))

    web_server.state.etag = '"v2"'
    row_before = documents.get(path.relative_to(sync_dirs[1]).as_posix())

    asyncio.run(_sync_once(runner, url))

    row_after = documents.get(path.relative_to(sync_dirs[1]).as_posix())
    assert row_after["etag"] == row_before["etag"], "conflictなのにetagを更新した"


# ---------------------------------------------------------------------------
# front matter: 7キー・dateの初回取得日維持
# ---------------------------------------------------------------------------


def test_front_matter_has_seven_keys_and_preserves_first_fetch_date(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    url = web_server.base_url + "/"
    runner = make_runner(documents, sync_dirs, web_server.base_url)
    asyncio.run(_sync_once(runner, url))
    path = one_saved_file(sync_dirs[1], sync_dirs[2])
    parsed = parse_frontmatter(read_raw(path))
    assert set(parsed.data.keys()) == {
        "title",
        "url",
        "date",
        "source",
        "managed_by",
        "document_type",
        "status",
    }
    first_date = parsed.data["date"]

    web_server.state.etag = '"v2"'
    web_server.state.body = "<p>更新後</p>"
    asyncio.run(_sync_once(runner, url))
    parsed_after = parse_frontmatter(read_raw(path))
    assert parsed_after.data["date"] == first_date, "再取得でdateが動いた"


# ---------------------------------------------------------------------------
# クロール: リンク追跡・上限強制(必須、旧実装からの意図的な追加)
# ---------------------------------------------------------------------------


def test_crawl_follows_links_and_respects_max_depth(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    base = web_server.base_url
    web_server.state.body = f'<p>本文</p><a href="{base}/child">子</a>'
    web_server.state.links = {"/child": "<p>子ページ</p>"}
    runner = make_runner(documents, sync_dirs, base, max_depth=1)

    result = asyncio.run(runner.crawl(base + "/"))

    urls = {item.url for item in result.items}
    assert base + "/" in urls
    assert base + "/child" in urls
    assert result.full_sync_succeeded is True


def test_crawl_does_not_follow_links_beyond_max_depth(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    base = web_server.base_url
    web_server.state.body = f'<p>本文</p><a href="{base}/child">子</a>'
    web_server.state.links = {"/child": "<p>子ページ</p>"}
    runner = make_runner(documents, sync_dirs, base, max_depth=0)

    result = asyncio.run(runner.crawl(base + "/"))

    urls = {item.url for item in result.items}
    assert base + "/" in urls
    assert base + "/child" not in urls, "max_depthを超えて追跡した"


def test_crawl_ignores_cross_host_links(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    base = web_server.base_url
    web_server.state.body = '<p>本文</p><a href="http://other.example.com/x">外部</a>'
    runner = make_runner(documents, sync_dirs, base, max_depth=2)

    result = asyncio.run(runner.crawl(base + "/"))

    assert all("other.example.com" not in item.url for item in result.items)


def test_crawl_stops_at_max_pages(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    base = web_server.base_url
    web_server.state.body = f'<a href="{base}/a">a</a><a href="{base}/b">b</a>'
    web_server.state.links = {"/a": "ページA", "/b": "ページB"}
    runner = make_runner(documents, sync_dirs, base, max_depth=2, max_pages=2)

    result = asyncio.run(runner.crawl(base + "/"))

    assert result.pages_processed <= 2
    assert result.full_sync_succeeded is False, "上限で打ち切ったのに成功扱いにした"


def test_crawl_enforces_max_size_bytes(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    base = web_server.base_url
    web_server.state.body = "<p>" + ("あ" * 10_000) + "</p>"
    runner = make_runner(documents, sync_dirs, base, max_size_bytes=100)

    result = asyncio.run(runner.crawl(base + "/"))

    assert len(result.items) == 1
    assert result.items[0].action == str(SyncAction.ERROR)
    assert "サイズ上限" in result.items[0].reason


# ---------------------------------------------------------------------------
# delay 強制(carried-over fix): 旧 `download-web.js --delay` は Python 側では
# `batch_items.options.delay` として運ばれるだけで、どこも消費していなかった。
# `HostRateLimiter` が同一ホストへのリクエスト *開始* 間隔を強制する。
# ---------------------------------------------------------------------------


def test_host_rate_limiter_spaces_out_requests_to_same_host() -> None:
    """同一ホストへの `wait()` 呼び出しは並行に呼んでも delay 秒以上空く。"""
    limiter = HostRateLimiter(delay_seconds=0.05)

    async def _run() -> list[float]:
        timestamps: list[float] = []

        async def _one() -> None:
            await limiter.wait("example.com")
            timestamps.append(time.monotonic())

        await asyncio.gather(*(_one() for _ in range(4)))
        return sorted(timestamps)

    times = asyncio.run(_run())
    gaps = [b - a for a, b in zip(times, times[1:])]  # noqa: B905 - pairwise, lengths differ by design
    assert all(gap >= 0.045 for gap in gaps), f"delay未満の間隔があった: {gaps}"


def test_host_rate_limiter_does_not_serialize_across_hosts() -> None:
    """異なるホスト宛の待機はお互いをブロックしない(クロール全体を直列化しない)。

    以前は `asyncio.gather` の壁時計所要時間に上限(0.5秒)を課していたが、これは
    CPU高負荷下でプロセス全体が遅くなると無関係な理由で失敗しうる同種の
    スケジューリング運任せの assertion だった。実際に検証したいのは「待機時間が
    実時間としてどれだけ短いか」ではなく「一方のホストの待機がもう一方の待機の
    "決定"をブロックしていないか」なので、`asyncio.sleep` をスパイに差し替えて
    どちらのホストも delay_seconds 分の sleep を要求すること(=両方が独立して
    待っている)、かつそれが並行に要求されること(直列化されていれば2回目の
    sleep 要求は1回目の実際の完了を待ってしまう)を、実時間計測なしで検証する。
    """
    limiter = HostRateLimiter(delay_seconds=1.0)
    sleep_requests: list[str] = []

    real_sleep = asyncio.sleep

    async def spying_sleep(seconds: float) -> None:
        sleep_requests.append("start")
        await real_sleep(0)  # 実時間を消費せずイベントループに制御を返すだけ
        sleep_requests.append("end")

    async def _run() -> None:
        # 1回目は素通り(next_available_at が未設定なので待たない)。
        await asyncio.gather(limiter.wait("a.example.com"), limiter.wait("b.example.com"))
        # 2回目は両ホストとも直前の予約から delay_seconds 以内なので必ず sleep を要求する。
        await asyncio.gather(limiter.wait("a.example.com"), limiter.wait("b.example.com"))

    with mock.patch("asyncio.sleep", spying_sleep):
        asyncio.run(_run())

    # 直列化されていれば ["start", "end", "start", "end"] になるはず。
    # 直列化されていなければ両方が先に始まってから終わる。
    assert sleep_requests[:2] == ["start", "start"], (
        f"別ホスト宛のwaitが直列化された(片方の完了を待ってから開始した): {sleep_requests}"
    )


def test_host_rate_limiter_disabled_by_default_is_noop() -> None:
    """`delay_seconds=0`(既定)は待たない。

    以前は50回の `wait()` 呼び出しに壁時計で寛容な上限(0.2秒)を課していたが、
    「待たない」という契約は `asyncio.sleep` が一度も呼ばれないことそのもので
    検証できるため、実時間計測に頼る必要がない(スケジューリング運の排除)。
    """
    limiter = HostRateLimiter()

    async def _run() -> None:
        for _ in range(50):
            await limiter.wait("example.com")

    with mock.patch("asyncio.sleep") as mocked_sleep:
        asyncio.run(_run())

    mocked_sleep.assert_not_called()


def test_host_rate_limiter_waits_the_configured_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """`HostRateLimiter.wait` は「delay_seconds 分待った」という意思決定そのものを
    フェイク時計で検証する(carried-over fix)。

    旧テスト『delay_seconds を指定したクロールは同一ホストへの各取得開始が
    少なくとも delay 秒空く』は実サーバへのHTTPリクエストの壁時計到着時刻を比較
    しており、CPU高負荷下でスレッドスケジューリングが乱れると、ゲート解放順序と
    サーバ到着順序がずれて誤って失敗した(実運用で少なくとも3回観測)。
    ここでは実際の sleep やネットワークを一切使わず、`asyncio.sleep` と
    イベントループの `time()` をフェイクに差し替えて `HostRateLimiter` 自体の
    待機ロジックを決定論的に検証する。delay の強制が失われれば(例:`wait` が
    no-op になる、または計算が壊れる)`sleeps` が空になるか値がずれて必ず失敗する。
    """

    async def scenario() -> list[float]:
        limiter = HostRateLimiter(delay_seconds=0.1)
        loop = asyncio.get_event_loop()
        clock = {"now": 0.0}
        sleeps: list[float] = []

        def fake_time() -> float:
            return clock["now"]

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += seconds

        monkeypatch.setattr(loop, "time", fake_time)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        await limiter.wait("host")  # 1回目: 待たずに即時発火してよい
        clock["now"] += 0.02  # 2回目の到着は delay(0.1s)未満しか経過していない
        await limiter.wait("host")  # -> 0.08s 待つはず
        clock["now"] += 0.2  # 3回目は delay を十分超えて経過している
        await limiter.wait("host")  # -> 待たなくてよいはず

        return sleeps

    sleeps = asyncio.run(scenario())

    assert sleeps == [pytest.approx(0.08)], f"待機の意思決定が期待とずれている: {sleeps}"


def test_crawl_invokes_rate_limiter_once_per_request(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    """クロールは同一ホストへの各取得開始前に必ず `HostRateLimiter.wait` を呼ぶ
    (並行取得(concurrency)と共存させる、carried-over fix の配線を検証)。

    実時間の計測はしない(それは `HostRateLimiter` 自体を決定論的に検証する
    `test_host_rate_limiter_waits_the_configured_delay` の役割)。ここでは
    `_rate_limiter.wait` をスパイに差し替え、実際のリクエスト件数と同数
    呼ばれることだけを確認し、レート制限の配線が外れたら検知する。
    """
    base = web_server.base_url
    web_server.state.body = f'<a href="{base}/a">a</a><a href="{base}/b">b</a>'
    web_server.state.links = {"/a": "ページA", "/b": "ページB"}
    root_dir, docs_dir, output_dir = sync_dirs
    runner = WebSyncRunner(
        documents=documents,
        root_dir=root_dir,
        docs_dir=docs_dir,
        output_dir=output_dir,
        base_domain=base,
        max_depth=1,
        max_pages=10,
        delay_seconds=0.1,
    )

    wait_calls: list[str] = []
    original_wait = runner._rate_limiter.wait  # noqa: SLF001 - テストが配線を検証する

    async def spying_wait(host: str) -> None:
        wait_calls.append(host)
        await original_wait(host)

    with mock.patch.object(runner._rate_limiter, "wait", spying_wait):  # noqa: SLF001
        asyncio.run(runner.crawl(base + "/", concurrency=3))

    assert len(wait_calls) >= 3, f"取得のたびに rate limiter を呼んでいない: {wait_calls}"
    assert all(call for call in wait_calls), "rate limiter に空のホストを渡した"


# ---------------------------------------------------------------------------
# docs/ 強制付与(旧実装からの意図的な逸脱)
# ---------------------------------------------------------------------------


def test_output_dir_without_docs_prefix_is_still_written_under_docs(
    documents: DocumentRepository, tmp_root: Path, web_server: WebPageServer
) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir()
    runner = WebSyncRunner(
        documents=documents,
        root_dir=tmp_root,
        docs_dir=docs_dir,
        output_dir="catiadoc",  # docs/ を含まない旧式の指定
        base_domain=web_server.base_url,
    )
    asyncio.run(_sync_once(runner, web_server.base_url + "/"))

    saved = list((docs_dir / "catiadoc").glob("*.md"))
    assert len(saved) == 1, "docs/ 配下に書き込まれなかった"


def test_path_traversal_via_url_segment_is_rejected(sync_dirs, web_server: WebPageServer) -> None:
    root_dir, docs_dir, output_dir = sync_dirs
    with pytest.raises(Exception):  # noqa: B017 - AppError(INVALID_INPUT)
        resolve_web_path(
            web_server.base_url + "/../../outside",
            root_dir=root_dir,
            output_dir=output_dir,
            base_domain=web_server.base_url,
            docs_dir=docs_dir,
        )


# ---------------------------------------------------------------------------
# リースの契約: クロールの1件ずつの副作用ループは check_lease を反復ごとに呼ぶ
# ---------------------------------------------------------------------------


def test_crawl_checks_lease_between_pages_and_stops_after_theft(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    """`JobRunContext.check_lease` の契約: リースを奪われたら追加の書き込みを止める。"""
    base = web_server.base_url
    web_server.state.body = f'<a href="{base}/a">a</a><a href="{base}/b">b</a>'
    web_server.state.links = {"/a": "ページA", "/b": "ページB"}
    runner = make_runner(documents, sync_dirs, base, max_depth=1, max_pages=10)

    from abist_kb.domain.errors import AppError, ErrorCode

    calls = 0

    def lease_lost_on_second_item() -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AppError(code=ErrorCode.CONFLICT, message="リースが奪われました")

    with pytest.raises(AppError):
        asyncio.run(runner.crawl(base + "/", concurrency=1, check_lease=lease_lost_on_second_item))

    assert calls == 2


def test_sync_page_checks_lease_only_immediately_before_writing(
    documents: DocumentRepository, sync_dirs, web_server: WebPageServer
) -> None:
    """レビュー指摘の再現・回帰防止: `check_lease` は取得(fetch)ではなく実際の
    ファイル書き込み直前だけで呼ぶ。304(書き込み無し)では呼ばれない。"""
    url = web_server.base_url + "/"
    runner = make_runner(documents, sync_dirs, web_server.base_url)
    calls: list[str] = []

    async def _run() -> None:
        async with WebClient() as client:
            # 初回: 新規作成のため書き込みが発生する -> check_lease が1回呼ばれる。
            await runner.sync_page(client, url, check_lease=lambda: calls.append("write"))
            # 2回目: ETag一致で304、書き込みは発生しない -> check_lease は呼ばれない。
            await runner.sync_page(client, url, check_lease=lambda: calls.append("no-write"))

    asyncio.run(_run())

    assert calls == ["write"], "304(書き込み無し)でも呼ばれた、または初回に呼ばれなかった"
