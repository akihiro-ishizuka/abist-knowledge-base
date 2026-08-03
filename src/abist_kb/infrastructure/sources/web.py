"""Web クローラーソースアダプター(旧 `download-web.js` / `test/sync-web.test.js` の移植)。

`tests/fixtures/PROVENANCE.md` が明示する通り `test/sync-web.test.js` は
fixture化されておらず、このファイルでの pytest 再実装(`tests/sources/test_web.py`)
が受け入れ基準そのものになる。HTML→Markdown変換自体は `html_to_md.py`
(advisory契約)に分離してある。

**この移植が守る旧実装の契約(すべて実際の不具合・設計原則から生まれたもの)**:

1. **条件付きGET。** 保存済みの `etag`/`last_modified` を `If-None-Match`/
   `If-Modified-Since` として送る。`--force` 指定時、または初回取得時は送らない。
2. **304では本文が来ないため、保存済み Markdown からリンクを再抽出して巡回を
   継続する。** これが無いと変更の無いページに当たるたびクロールが停止し、
   その先にあるページへ到達できない。
3. **ローカル編集は保護する。** `domain.sync_policy.decide_sync_action` に従い、
   ローカル編集がある限り自動上書きしない(esa と同じ判定ロジックを再利用)。
4. **HTTP失敗はローカルファイルを削除しない。** サイトが落ちていることが
   コーパスを空にする理由にはならない(設計原則5と同じ考え方)。
5. **`etag`/`last_modified`/`source_content_hash` は実際に書き込んだ(または
   取得成功でバイト同一と判定した)ときだけ永続化する。** スキップ
   (`conflict`/`local_modified`/`unknown_local`/304)で更新すると、次回の
   条件付きGETが304を返し、競合やローカル編集が解決済みに見えてしまう。
6. **クロール上限は必須。** 同一ホストのみ・最大深さ・最大ページ数・最大
   サイズ・タイムアウトをすべて強制する(セキュリティ要件: 誤った/悪意ある
   ホストへ向けた無制限クローラは他者サーバーへのDoSになりうる)。旧実装は
   ページ数・サイズに上限が無かった(`最大ページ数: 無制限` とログに出していた)。
   これは意図的な仕様追加(旧実装からの逸脱)である。**`DEFAULT_MAX_PAGES` は
   実際の `batch-config.js`(旧リポジトリ)の `catiadoc` バッチ(`maxDepth: 10`)を
   実際にクロールした結果(約1,300ファイル)を踏まえて選定した値であり、
   バッチごとに `batch_items.options.max_pages`/`max_size_bytes` で上書きできる
   (`application.sync_service` 参照)。上限に達した場合は例外で握り潰さず
   `warn` 経由でログに残す(`WebSyncRunner.crawl` 参照)。

**もう1つの意図的な逸脱: `docs/` 強制付与。** 旧実装は `--output-dir` を
そのままファイルパス組み立てに使っており、`docs/` を含まないディレクトリ名
(例: `catiadoc`)を指定すると `docs/` の外へ書き込まれ、`documents` テーブル
からも見えなくなる文書が実際に発生した(`PROVENANCE.md` の
`docs/knowledge/catiadoc` 孤児データ記録参照)。ここでは esa と同じ
`with_docs_prefix` を通す。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from abist_kb.domain.frontmatter import hash_body, parse_frontmatter, sha256_hex
from abist_kb.domain.metadata_schema import classify_document, sanitize_file_name
from abist_kb.domain.sync_policy import (
    LocalState,
    RemoteState,
    SyncAction,
    SyncRecord,
    SyncStatus,
    decide_sync_action,
    sync_status_for,
)
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.sources.base import (
    docs_relative_path,
    ensure_within_docs,
    with_docs_prefix,
)
from abist_kb.infrastructure.sources.html_to_md import (
    extract_links,
    extract_links_from_markdown,
    extract_page_title,
    html_to_markdown,
)

logger = logging.getLogger(__name__)

WarnFn = Callable[[str], None]
CheckLeaseFn = Callable[[], None]
EmitFn = Callable[..., None]

# --- クロール上限(必須、任意化しない: セキュリティ要件) -----------------------
DEFAULT_MAX_DEPTH = 3
#: 旧実装は無制限だったため新規に決めた値。旧 `batch-config.js` の実在バッチ
#: `catiadoc`(`maxDepth: 10`)を実際にクロールした結果が約1,300ファイルだった
#: ことを踏まえ、この実測値に安全マージンを載せた5,000を既定にする(200のままだと
#: この実在バッチが黙って打ち切られていた)。バッチごとに
#: `batch_items.options.max_pages` で上書き可能(`application.sync_service`)。
DEFAULT_MAX_PAGES = 5_000
DEFAULT_MAX_SIZE_BYTES = 5_000_000  # 1ページあたり5MB
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_CONCURRENCY = 5

_USER_AGENT = "Mozilla/5.0 (compatible; abist-kb-web-sync/1.0)"


def _default_warn(message: str) -> None:
    logger.warning(message)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_text_preserving_eol(file_path: Path) -> str:
    """esa.py と同じ理由(front matter=LF・本文=CRLFの混在を保つ)で生バイトを読む。"""
    return file_path.read_bytes().decode("utf-8")


def _write_text_preserving_eol(file_path: Path, content: str) -> None:
    file_path.write_bytes(content.encode("utf-8"))


def _human_managed_from(content: str) -> dict[str, str]:
    parsed = parse_frontmatter(content)
    preserved: dict[str, str] = {}
    status = parsed.data.get("status")
    if isinstance(status, str) and status:
        preserved["status"] = status
    document_type = parsed.data.get("document_type")
    if isinstance(document_type, str) and document_type:
        preserved["document_type"] = document_type
    return preserved


def _existing_date(content: str) -> str | None:
    date = parse_frontmatter(content).data.get("date")
    return date if isinstance(date, str) and date else None


def _to_sync_record(row: dict[str, Any] | None) -> SyncRecord | None:
    if row is None:
        return None
    return SyncRecord(
        local_content_hash=row.get("local_content_hash"),
        source_content_hash=row.get("source_content_hash"),
        source_updated_at=row.get("source_updated_at"),
    )


def generate_web_front_matter(
    *, title: str, url: str, date: str, document_type: str, status: str
) -> str:
    """front matter を生成する(旧実装 :435-445 のテンプレートリテラルの移植)。

    旧実装は `title`/`url` を `JSON.stringify()` で埋め込んでいる(esa 側の
    `_yaml_scalar_line` のような簡易クォートエスケープではない)。JSON文字列化は
    バックスラッシュ・制御文字も正しくエスケープするため、Python 側も
    `json.dumps(..., ensure_ascii=False)` で揃える(`ensure_ascii=False` が
    無いと非ASCII文字が `\\uXXXX` に化けて JS の挙動と食い違う)。
    """
    title_json = json.dumps(title, ensure_ascii=False)
    url_json = json.dumps(url, ensure_ascii=False)
    lines = [
        "---",
        f"title: {title_json}",
        f"url: {url_json}",
        f"date: {date}",
        "source: web",
        "managed_by: web-sync",
        f"document_type: {document_type}",
        f"status: {status}",
        "---",
        "",
    ]
    return "\n".join(lines) + "\n"


def _url_to_path(target_url: str, base_domain: str) -> str | None:
    """URLからパス部分を取り出す(旧実装 `urlToPath` の移植)。同一ホストでなければ `None`。"""
    parsed = urlparse(target_url)
    base = urlparse(base_domain)
    if parsed.hostname != base.hostname:
        return None
    path = parsed.path
    if path.endswith("/"):
        path = path[:-1]
    if not path:
        path = "/index"
    return path


def resolve_web_path(
    url: str, *, root_dir: Path, output_dir: str, base_domain: str, docs_dir: Path
) -> tuple[Path, Path] | None:
    """URLから保存先ディレクトリ・ファイルパスを決める(旧実装 `resolveWebPath` の移植)。

    同一ホストでない場合は `None`(旧実装は `urlToPath` が `null` を返す挙動)。
    """
    url_path = _url_to_path(url, base_domain)
    if url_path is None:
        return None
    parts = [p for p in url_path.split("/") if p]
    if not parts:
        parts = ["index"]
    file_name = sanitize_file_name(parts[-1])
    dir_parts = [sanitize_file_name(p) for p in parts[:-1]]
    base = root_dir / output_dir
    dir_path = base.joinpath(*dir_parts) if dir_parts else base
    file_path = dir_path / f"{file_name}.md"
    ensure_within_docs(file_path, docs_dir)
    return dir_path, file_path


@dataclass(slots=True)
class SyncItem:
    """1ページの同期結果(`application.sync_service` のレポート化で使う)。"""

    path: str | None
    file_path: str
    url: str
    action: str
    reason: str
    write: bool = False
    error: str | None = None


@dataclass(slots=True)
class WebFetchResult:
    """`WebClient.fetch` の戻り値。"""

    status: int | None
    html: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    skipped: bool = False


class WebClient:
    """条件付きGET・サイズ上限・タイムアウトを守る薄い httpx ラッパー。"""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def __aenter__(self) -> WebClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        max_size_bytes: int | None = None,
    ) -> WebFetchResult:
        headers = {"User-Agent": _USER_AGENT}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        try:
            async with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code == 304:
                    return WebFetchResult(status=304)
                if response.status_code >= 400:
                    return WebFetchResult(
                        status=response.status_code,
                        error=f"HTTP {response.status_code} {response.reason_phrase}",
                    )
                content_type = response.headers.get("content-type", "")
                if "text/html" not in content_type:
                    return WebFetchResult(status=response.status_code, skipped=True)

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if max_size_bytes is not None and total > max_size_bytes:
                        return WebFetchResult(
                            status=response.status_code,
                            error=f"サイズ上限({max_size_bytes}バイト)を超過したため打ち切りました",
                        )
                    chunks.append(chunk)
                body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                return WebFetchResult(
                    status=response.status_code,
                    html=body,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
        except httpx.HTTPError as exc:
            return WebFetchResult(status=None, error=str(exc))


@dataclass(slots=True)
class CrawlResult:
    """`WebSyncRunner.crawl` の戻り値。"""

    items: list[SyncItem]
    full_sync_succeeded: bool
    pages_processed: int
    pages_over_limit: int


class WebSyncRunner:
    """1つの出力先に対する Web クロール・同期セッション。"""

    def __init__(
        self,
        *,
        documents: DocumentRepository,
        root_dir: Path,
        docs_dir: Path,
        output_dir: str,
        base_domain: str,
        force: bool = False,
        dry_run: bool = False,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        warn: WarnFn = _default_warn,
    ) -> None:
        self._documents = documents
        self._root_dir = root_dir
        self._docs_dir = docs_dir
        # 旧実装の不整合を矯正する(モジュール docstring の逸脱2)。
        self._output_dir = with_docs_prefix(output_dir)
        self._base_domain = base_domain
        self._force = force
        self._dry_run = dry_run
        self._max_depth = max_depth
        self._max_pages = max_pages
        self._max_size_bytes = max_size_bytes
        self._timeout_seconds = timeout_seconds
        self._warn = warn

    # -- DB アクセス(失敗してもダウンロードを止めない契約、esa と同じ) ----------

    def _safe_get(self, path: str) -> dict[str, Any] | None:
        try:
            return self._documents.get(path)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"sync-state の読み取りに失敗しました ({path}): {exc}")
            return None

    def _safe_upsert(self, record: dict[str, Any]) -> None:
        try:
            self._documents.upsert(record)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"sync-state への記録に失敗しました ({record.get('path')}): {exc}")

    # -- 1ページの同期 ---------------------------------------------------------

    async def sync_page(
        self, client: WebClient, url: str, *, check_lease: CheckLeaseFn | None = None
    ) -> tuple[SyncItem | None, list[str]]:
        """1ページを条件付きGETで差分同期する(旧実装 `downloadPage` の移植)。

        `check_lease` はこのページが実際にファイルへ書き込む直前(このメソッド内の
        唯一の `_write_text_preserving_eol` 呼び出しの直前)にだけ呼ぶ。取得
        (`client.fetch`)自体は副作用が無いため、リース確認を取得前に置いても
        「スケジュール済みの取得がリース喪失後も完了してしまう」ことを防げない
        (レビュー指摘: 高並行度では確認直後に多数のフェッチが開始してしまう)。
        意味のある副作用(=ファイル書き込み)の直前に置くことで、確認と副作用の
        間に他の処理が挟まらない最小の窓にする。
        """
        resolved = resolve_web_path(
            url,
            root_dir=self._root_dir,
            output_dir=self._output_dir,
            base_domain=self._base_domain,
            docs_dir=self._docs_dir,
        )
        if resolved is None:
            return None, []
        dir_path, file_path = resolved
        relative_path = docs_relative_path(file_path, self._docs_dir)
        record = self._safe_get(relative_path) if relative_path else None
        existing = _read_text_preserving_eol(file_path) if file_path.is_file() else None

        etag = record.get("etag") if record else None
        last_modified = record.get("last_modified") if record else None
        if self._force or existing is None:
            etag = last_modified = None

        result = await client.fetch(
            url, etag=etag, last_modified=last_modified, max_size_bytes=self._max_size_bytes
        )

        # --- ネットワークエラー(タイムアウト含む) ---
        if result.status is None:
            if relative_path and not self._dry_run:
                self._safe_upsert(
                    {
                        "path": relative_path,
                        "last_checked_at": _now_iso(),
                        "sync_error": result.error,
                    }
                )
            return (
                SyncItem(
                    path=relative_path,
                    file_path=str(file_path),
                    url=url,
                    action=str(SyncAction.ERROR),
                    reason=result.error or "network error",
                    error=result.error,
                ),
                [],
            )

        # --- 304 Not Modified: 本文が来ないので保存済み Markdown からリンクを継続 ---
        if result.status == 304:
            if relative_path and not self._dry_run:
                self._safe_upsert({"path": relative_path, "last_checked_at": _now_iso()})
            links = (
                extract_links_from_markdown(existing, self._base_domain)
                if existing is not None
                else []
            )
            return (
                SyncItem(
                    path=relative_path,
                    file_path=str(file_path),
                    url=url,
                    action=str(SyncAction.UNCHANGED),
                    reason="条件付きGETで304 Not Modified",
                ),
                links,
            )

        # --- HTTP失敗: ローカルファイルを削除しない(設計原則5) ---
        if result.error is not None:
            if relative_path and not self._dry_run:
                self._safe_upsert(
                    {
                        "path": relative_path,
                        "last_checked_at": _now_iso(),
                        "sync_error": result.error,
                    }
                )
            return (
                SyncItem(
                    path=relative_path,
                    file_path=str(file_path),
                    url=url,
                    action=str(SyncAction.ERROR),
                    reason=result.error,
                    error=result.error,
                ),
                [],
            )

        if result.skipped or result.html is None:
            return None, []

        html = result.html
        markdown = html_to_markdown(html, url)
        links = extract_links(html, url, self._base_domain)

        decision = decide_sync_action(
            remote=RemoteState(content_hash=sha256_hex(markdown), updated_at=result.last_modified),
            record=_to_sync_record(record),
            local=LocalState(
                exists=existing is not None,
                body_hash=None if existing is None else hash_body(existing),
            ),
            force=self._force,
        )
        item = SyncItem(
            path=relative_path,
            file_path=str(file_path),
            url=url,
            action=str(decision.action),
            reason=decision.reason,
            write=decision.write,
        )

        if not decision.write:
            if relative_path and not self._dry_run:
                fields: dict[str, Any] = {
                    "path": relative_path,
                    "last_checked_at": _now_iso(),
                    "sync_status": str(sync_status_for(decision.action)),
                }
                if decision.action is SyncAction.ADOPT:
                    fields["local_content_hash"] = None if existing is None else hash_body(existing)
                self._safe_upsert(fields)
            return item, links

        preserved = _human_managed_from(existing) if existing is not None else {}
        classification = classify_document(
            relative_path=relative_path or file_path.name,
            frontmatter={"source": "web", "url": url, **preserved},
        )
        document_type = preserved.get("document_type") or classification.document_type
        status = preserved.get("status") or classification.status
        # date は初回取得日を保つ(brief必須要件)。
        date = (_existing_date(existing) if existing is not None else None) or datetime.now(
            UTC
        ).date().isoformat()
        title = extract_page_title(html) or file_path.stem

        content = (
            generate_web_front_matter(
                title=title, url=url, date=date, document_type=document_type, status=status
            )
            + markdown
        )

        # source_* / etag / last_modified は「最後に同期できた状態」。
        # 書いていない(スキップ)ときに更新すると、次回304が競合を隠す(brief必須要件)。
        synced_fields: dict[str, Any] = {
            "path": relative_path,
            "source": "web",
            "managed_by": "web-sync",
            "url": url,
            "source_key": f"web:{url}",
            "source_content_hash": sha256_hex(markdown),
            "etag": result.etag,
            "last_modified": result.last_modified,
            "last_checked_at": _now_iso(),
            "sync_error": None,
        }

        if existing is not None and content == existing:
            item.action = str(SyncAction.UNCHANGED)
            item.reason = "取得内容が現在のファイルと同一のため書き込みませんでした"
            item.write = False
            if relative_path and not self._dry_run:
                self._safe_upsert(
                    {
                        **synced_fields,
                        "local_content_hash": hash_body(existing),
                        "sync_status": str(SyncStatus.SYNCED),
                    }
                )
            return item, links

        if self._dry_run:
            return item, links

        # 実際にファイルへ書き込む直前でのリース生存確認(このメソッドの docstring・
        # `crawl` の `_guarded_sync_page` 参照)。
        if check_lease is not None:
            check_lease()

        dir_path.mkdir(parents=True, exist_ok=True)
        _write_text_preserving_eol(file_path, content)

        if relative_path:
            self._safe_upsert(
                {
                    **synced_fields,
                    "document_type": document_type,
                    "status": status,
                    "title": title,
                    "local_content_hash": hash_body(content),
                    "downloaded_at": _now_iso(),
                    "sync_status": str(SyncStatus.SYNCED),
                }
            )
        return item, links

    # -- クロール(キュー + 並行数制御 + 上限強制) -------------------------------

    async def crawl(
        self,
        start_url: str,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        check_lease: CheckLeaseFn | None = None,
        emit: EmitFn | None = None,
    ) -> CrawlResult:
        """`start_url` から同一ホスト内を幅優先でクロールする(旧実装 `main`/`processQueue` の移植)。

        旧実装はワーカープール + アイドルタイムアウトという、Node の
        非同期スケジューリング都合の実装だった。ここでは深さレベルごとに
        `concurrency` 件までまとめて並行取得するシンプルな幅優先探索に
        置き換えている(キューが尽きれば自然に終了するため、アイドル
        タイムアウトのようなヒューリスティックは不要)。クロール上限
        (深さ・ページ数・サイズ・タイムアウト・同一ホスト)は必須のまま。
        """
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        items: list[SyncItem] = []
        pages_processed = 0
        hit_page_limit = False

        async with WebClient(timeout=self._timeout_seconds) as client:
            while queue:
                if pages_processed >= self._max_pages:
                    hit_page_limit = True
                    self._warn(
                        f"最大ページ数の上限({self._max_pages})に達したため、"
                        f"クロールを打ち切りました(未処理のURLが{len(queue)}件残っています)。"
                        "batch_items.options.max_pages で上限を引き上げられます。"
                    )
                    break

                batch: list[tuple[str, int]] = []
                while queue and len(batch) < max(1, concurrency):
                    url, depth = queue.popleft()
                    if url in visited or depth > self._max_depth:
                        continue
                    visited.add(url)
                    batch.append((url, depth))

                if not batch:
                    continue

                # fix(レビュー指摘): 取得(await前)ではなく `sync_page` 内の実際の
                # ファイル書き込み直前でリース確認する(`sync_page` の docstring参照)。
                # BFS+gather の並行モデルでは「取得開始前」の確認だと、既に
                # スケジュール済みの取得群がリース喪失後も完了し書き込みまで
                # 進んでしまう(確認とその後の並行フェッチの間に窓がある)。
                # 書き込み直前まで確認を遅らせることで、その窓を最小化する。
                results = await asyncio.gather(
                    *(self.sync_page(client, url, check_lease=check_lease) for url, _ in batch)
                )

                for (url, depth), (item, links) in zip(batch, results, strict=True):
                    if item is not None:
                        items.append(item)
                        pages_processed += 1
                        if emit is not None:
                            emit(
                                phase="sync-web",
                                current=pages_processed,
                                total=None,
                                message=f"{item.action}: {url}",
                                item=item.path or item.file_path,
                            )
                        if pages_processed >= self._max_pages:
                            hit_page_limit = True
                            break
                    if depth < self._max_depth:
                        for link in links:
                            if link not in visited:
                                queue.append((link, depth + 1))

        full_sync_succeeded = not queue and not hit_page_limit
        return CrawlResult(
            items=items,
            full_sync_succeeded=full_sync_succeeded,
            pages_processed=pages_processed,
            pages_over_limit=len(queue) if hit_page_limit else 0,
        )


__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_MAX_SIZE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "CrawlResult",
    "SyncItem",
    "WebClient",
    "WebFetchResult",
    "WebSyncRunner",
    "generate_web_front_matter",
    "resolve_web_path",
]
