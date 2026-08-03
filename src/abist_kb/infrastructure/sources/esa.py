"""esa.io ソースアダプター(旧 `download-article.js` の移植、task-3-brief)。

httpx 非同期クライアントで記事の取得・検索を行い、`domain.sync_policy` の判定に
従ってローカルファイルと `documents` テーブルへ反映する。ネットワーク・判定・
ファイルI/O・DB記録という複数の関心事を1ファイルへ集約しているのは旧実装
(`download-article.js`)の構造をそのまま踏襲したためで、`EsaClient`(HTTP)と
`EsaSyncRunner`(1回の同期セッションの状態)は独立して差し替え可能にしてある。

**この移植が守る旧実装の契約(すべて実際の不具合から生まれたもの)**:

1. **第二のバイト同一性ガード。** front matter の `updated_at` は日付のみに
   丸めているため、同日中の取得元更新は `decide_sync_action` に「変わった」と
   判定されてしまう。レンダリング結果を実際に既存ファイルと比較し、1バイトも
   違わなければ `unchanged` へ格下げして書き込みを止める。これが無いと毎回
   全件書き換えになる。
2. **DB書き込み失敗はダウンロードを失敗させない(旧 `doc-record.js` の契約)。**
   `documents` への読み書きはすべて例外を握りつぶし警告するだけにする。
   一時的なDB障害でダウンロード済みの内容を失ったり、実行を丸ごと失敗させない。
3. **`missing`/`orphan` は `decide_missing_candidate` に委ねる、保守的な判定。**
   一覧不在だけでは「削除された」と判定しない(カテゴリ移動・権限変更・
   WIP変化と区別できないため)。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from abist_kb.domain.errors import AppError, ErrorCode, wrap
from abist_kb.domain.frontmatter import hash_body, parse_frontmatter, sha256_hex
from abist_kb.domain.metadata_schema import (
    ManagedBy,
    Source,
    classify_document,
    sanitize_category_path,
    sanitize_file_name,
)
from abist_kb.domain.sync_policy import (
    LocalState,
    MissingDecision,
    RemoteState,
    SyncAction,
    SyncRecord,
    SyncStatus,
    decide_missing_candidate,
    decide_sync_action,
    sync_status_for,
)
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.sources.base import docs_relative_path, ensure_within_docs

logger = logging.getLogger(__name__)

WarnFn = Callable[[str], None]

DEFAULT_MISSING_THRESHOLD = 3
DEFAULT_TIMEOUT_SECONDS = 30.0

_BARE_FRONTMATTER_KEYS = frozenset({"source", "managed_by", "document_type", "status"})


def _default_warn(message: str) -> None:
    logger.warning(message)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_text_preserving_eol(file_path: Path) -> str:
    """`Path.read_text()` の既定(universal newlines)を避け、改行を1バイトも変えずに読む。

    esa 由来の本文は front matter=LF・本文=CRLF が混在する(`frontmatter.py` の
    モジュール docstring 参照)。`Path.read_text()` は既定でテキストモードの
    改行変換(`\\r\\n`/`\\r` -> `\\n`)を行うため、素朴に使うと読み直した内容が
    書き込んだ内容と一致しなくなり、`decide_sync_action` のハッシュ比較が
    毎回「ローカル編集あり」に化けてしまう(実装中に実測した不具合)。
    """
    return file_path.read_bytes().decode("utf-8")


def _write_text_preserving_eol(file_path: Path, content: str) -> None:
    """`Path.write_text()` の既定の改行変換を避け、`content` を1バイトも変えずに書く。

    Windows の既定テキストモードは書き込み時に単独の `\\n` を `os.linesep`
    (`\\r\\n`)へ変換する。`content` は front matter=LF・本文=CRLF を意図的に
    混在させているため、この変換が起きると `\\r\\n` が `\\r\\r\\n` に壊れる。
    """
    file_path.write_bytes(content.encode("utf-8"))


# --------------------------------------------------------------------------
# HTTP クライアント
# --------------------------------------------------------------------------


class EsaClient:
    """esa.io API の薄いラッパー(httpx 非同期)。"""

    def __init__(
        self,
        *,
        team: str,
        access_token: str,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._team = team
        self._token = access_token
        self._base_url = (base_url or f"https://api.esa.io/v1/teams/{team}").rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> EsaClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        # トークンはここにしか組み立てない。ログ・例外メッセージへは絶対に含めない。
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def get_post(self, post_number: int) -> dict[str, Any]:
        """記事番号を指定して1件取得する。"""
        url = f"{self._base_url}/posts/{post_number}"
        try:
            response = await self._client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise wrap(
                exc,
                code=ErrorCode.EXTERNAL_SERVICE,
                message=f"記事の取得に失敗しました: #{post_number}",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise AppError(
                code=ErrorCode.EXTERNAL_SERVICE,
                message=f"記事の取得に失敗しました: {response.status_code} #{post_number}",
                retryable=response.status_code >= 500,
            )
        return response.json()

    async def search_posts(self, query: str = "", *, per_page: int = 100) -> list[dict[str, Any]]:
        """検索クエリで記事一覧を取得する(ページネーション対応)。"""
        all_posts: list[dict[str, Any]] = []
        page = 1
        while True:
            params = {"q": query, "per_page": per_page, "page": page}
            try:
                response = await self._client.get(
                    f"{self._base_url}/posts", headers=self._headers(), params=params
                )
            except httpx.HTTPError as exc:
                raise wrap(
                    exc,
                    code=ErrorCode.EXTERNAL_SERVICE,
                    message="記事の検索に失敗しました",
                    retryable=True,
                ) from exc
            if response.status_code >= 400:
                raise AppError(
                    code=ErrorCode.EXTERNAL_SERVICE,
                    message=f"記事の検索に失敗しました: {response.status_code}",
                    retryable=response.status_code >= 500,
                )
            data = response.json()
            posts = data.get("posts") or []
            all_posts.extend(posts)
            has_more = len(posts) == per_page and data.get("next_page") is not None
            if not has_more:
                break
            page += 1
        return all_posts


def category_search_queries(category_path: str) -> list[str]:
    """カテゴリパスから esa 検索クエリを組み立てる(末尾スラッシュ有無の両方を試す)。"""
    trimmed = category_path.rstrip("/")
    return [f'category:"{trimmed}/"', f'category:"{trimmed}"']


# --------------------------------------------------------------------------
# 保存先パス解決
# --------------------------------------------------------------------------


def resolve_post_path(
    post: Mapping[str, Any], *, root_dir: Path, output_dir: str, docs_dir: Path
) -> tuple[Path, Path]:
    """記事の保存先ディレクトリ・ファイルパスを決める(保存せずに使う場面もある)。"""
    file_name = sanitize_file_name(post.get("name") or f"post-{post.get('number')}")
    category = post.get("category")
    category_dir = sanitize_category_path(category) if category else ""
    base = root_dir / output_dir
    dir_path = (base / category_dir) if category_dir else base
    file_path = dir_path / f"{file_name}.md"
    ensure_within_docs(file_path, docs_dir)
    return dir_path, file_path


# --------------------------------------------------------------------------
# front matter 生成
# --------------------------------------------------------------------------


def _date_only(value: str) -> str:
    """esa の ISO8601 タイムスタンプを UTC の日付部分だけに丸める。

    JS の `new Date(v).toISOString().split('T')[0]` と同じ挙動(常に UTC へ変換
    してから日付を取り出す)。
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).date().isoformat()


def _yaml_scalar_line(key: str, value: Any) -> str | None:
    """front matter の1行を組み立てる。空値は `None`(=行を出さない)。

    旧実装 `generateFrontMatter` と同じ規則: `value !== null && value !==
    undefined && value !== ''` のときだけ行を出す。この条件は空配列・`0`・
    `false` を弾かない(型が違うので `''` と等しくならない)ため、それらは
    値ありとして出力される。配列は要素ごとにダブルクォート(空配列は
    `key: []` になる)、メタスキーマの4キー(`source`/`managed_by`/
    `document_type`/`status`)は素の値、それ以外の文字列は常にダブルクォート
    (内部の `"` だけをエスケープ、バックスラッシュは変換しない)。
    """
    if value is None or value == "":
        return None
    if isinstance(value, list):
        parts = ", ".join(f'"{v}"' for v in value)
        return f"{key}: [{parts}]"
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{key}: {value}"
    text = str(value)
    if key in _BARE_FRONTMATTER_KEYS:
        return f"{key}: {text}"
    escaped = text.replace('"', '\\"')
    return f'{key}: "{escaped}"'


def generate_front_matter(
    post: Mapping[str, Any], metadata: Mapping[str, Any] | None = None
) -> str:
    """front matter ブロックを生成する(キー順は `FRONTMATTER_KEY_ORDER` と一致)。"""
    created_at = post.get("created_at")
    updated_at = post.get("updated_at")
    created_by = post.get("created_by") or {}
    updated_by = post.get("updated_by") or {}
    author = created_by.get("screen_name") if isinstance(created_by, Mapping) else None
    updated_by_name = updated_by.get("screen_name") if isinstance(updated_by, Mapping) else None

    front_matter: dict[str, Any] = {
        "title": post.get("name") or "Untitled",
        "date": _date_only(created_at) if created_at else "",
        "updated_at": _date_only(updated_at) if updated_at else "",
        "author": author or "",
        "updated_by": updated_by_name or "",
        "category": post.get("category") or "",
        "tags": post.get("tags") or [],
        "post_number": post.get("number"),
        "url": post.get("url") or "",
    }
    front_matter.update(metadata or {})

    lines = ["---"]
    for key, value in front_matter.items():
        line = _yaml_scalar_line(key, value)
        if line is not None:
            lines.append(line)
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 同期実行の状態
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SyncItem:
    """1記事の同期結果(`application.sync_service` のレポート化で使う)。"""

    path: str | None
    file_path: str
    post_number: int | None
    title: str
    action: str
    reason: str
    write: bool = False
    error: str | None = None
    expected_path: str | None = None


def _to_sync_record(row: Mapping[str, Any] | None) -> SyncRecord | None:
    if row is None:
        return None
    return SyncRecord(
        local_content_hash=row.get("local_content_hash"),
        source_content_hash=row.get("source_content_hash"),
        source_updated_at=row.get("source_updated_at"),
    )


def _human_managed_from(content: str) -> dict[str, str]:
    """既存ファイルから人間が設定した `status`/`document_type` を引き継ぐ。"""
    parsed = parse_frontmatter(content)
    preserved: dict[str, str] = {}
    status = parsed.data.get("status")
    if isinstance(status, str) and status:
        preserved["status"] = status
    document_type = parsed.data.get("document_type")
    if isinstance(document_type, str) and document_type:
        preserved["document_type"] = document_type
    return preserved


def _adopt_fields(post: Mapping[str, Any], existing: str | None) -> dict[str, Any]:
    number = post.get("number")
    return {
        "source": Source.ESA.value,
        "managed_by": ManagedBy.ESA_SYNC.value,
        "title": post.get("name") or "",
        "url": post.get("url") or "",
        "category": post.get("category") or "",
        "post_number": number,
        "source_key": f"esa:{number}" if number else None,
        # 一致の保証が無い取得元ハッシュを入れてはいけない(旧実装のコメント参照)。
        "source_content_hash": None,
        "local_content_hash": None if existing is None else hash_body(existing),
    }


class EsaSyncRunner:
    """1つの出力先(source/batch item)に対する esa 同期セッション。"""

    def __init__(
        self,
        *,
        documents: DocumentRepository,
        root_dir: Path,
        docs_dir: Path,
        output_dir: str,
        force: bool = False,
        dry_run: bool = False,
        missing_threshold: int = DEFAULT_MISSING_THRESHOLD,
        warn: WarnFn = _default_warn,
    ) -> None:
        self._documents = documents
        self._root_dir = root_dir
        self._docs_dir = docs_dir
        self._output_dir = output_dir
        self._force = force
        self._dry_run = dry_run
        self._missing_threshold = missing_threshold
        self._warn = warn

    # -- DB アクセス(失敗してもダウンロードを止めない契約) -------------------

    def _safe_get(self, path: str) -> dict[str, Any] | None:
        try:
            return self._documents.get(path)
        except Exception as exc:  # noqa: BLE001 - doc-record.js と同じ契約
            self._warn(f"sync-state の読み取りに失敗しました ({path}): {exc}")
            return None

    def _safe_upsert(self, record: dict[str, Any]) -> None:
        try:
            self._documents.upsert(record)
        except Exception as exc:  # noqa: BLE001 - doc-record.js と同じ契約
            self._warn(f"sync-state への記録に失敗しました ({record.get('path')}): {exc}")

    def _safe_mark_missing(self, path: str) -> int:
        try:
            return self._documents.mark_missing(path)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"欠落カウントの記録に失敗しました ({path}): {exc}")
            return 0

    def _safe_clear_missing(self, path: str) -> None:
        try:
            self._documents.clear_missing(path)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"欠落カウントのリセットに失敗しました ({path}): {exc}")

    # -- 1記事の保存 --------------------------------------------------------

    def save_post(self, post: Mapping[str, Any]) -> SyncItem:
        """1記事を差分同期する(旧実装 `savePost` の移植)。"""
        dir_path, file_path = resolve_post_path(
            post, root_dir=self._root_dir, output_dir=self._output_dir, docs_dir=self._docs_dir
        )
        relative_path = docs_relative_path(file_path, self._docs_dir)
        body_md = post.get("body_md") or post.get("body") or ""

        existing = _read_text_preserving_eol(file_path) if file_path.is_file() else None
        record = self._safe_get(relative_path) if relative_path else None

        decision = decide_sync_action(
            remote=RemoteState(content_hash=sha256_hex(body_md), updated_at=post.get("updated_at")),
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
            post_number=post.get("number"),
            title=post.get("name") or "",
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
                    fields.update(_adopt_fields(post, existing))
                self._safe_upsert(fields)
            return item

        preserved = _human_managed_from(existing) if existing is not None else {}
        classification = classify_document(
            relative_path=relative_path or file_path.name,
            frontmatter={
                "post_number": post.get("number"),
                "category": post.get("category"),
                **preserved,
            },
        )
        metadata = {
            "source": Source.ESA.value,
            "managed_by": ManagedBy.ESA_SYNC.value,
            "document_type": preserved.get("document_type") or classification.document_type,
            "status": preserved.get("status") or classification.status,
        }
        content = generate_front_matter(post, metadata) + body_md

        # 第二のバイト同一性ガード(モジュール docstring の契約1)。
        if existing is not None and content == existing:
            item.action = str(SyncAction.UNCHANGED)
            item.reason = "取得元の内容が現在のファイルと同一のため書き込みませんでした"
            item.write = False
            if relative_path and not self._dry_run:
                self._safe_upsert(
                    {
                        "path": relative_path,
                        "source_updated_at": post.get("updated_at"),
                        "source_content_hash": sha256_hex(body_md),
                        "local_content_hash": hash_body(existing),
                        "last_checked_at": _now_iso(),
                        "missing_count": 0,
                        "missing_since": None,
                        "sync_status": str(SyncStatus.SYNCED),
                        "sync_error": None,
                    }
                )
            return item

        if self._dry_run:
            return item

        dir_path.mkdir(parents=True, exist_ok=True)
        _write_text_preserving_eol(file_path, content)

        if relative_path:
            number = post.get("number")
            self._safe_upsert(
                {
                    "path": relative_path,
                    "source": Source.ESA.value,
                    "managed_by": ManagedBy.ESA_SYNC.value,
                    "document_type": metadata["document_type"],
                    "status": metadata["status"],
                    "title": post.get("name") or "",
                    "url": post.get("url") or "",
                    "category": post.get("category") or "",
                    "post_number": number,
                    "source_key": f"esa:{number}" if number else None,
                    "source_updated_at": post.get("updated_at"),
                    "source_content_hash": sha256_hex(body_md),
                    "local_content_hash": hash_body(content),
                    "downloaded_at": _now_iso(),
                    "last_checked_at": _now_iso(),
                    "missing_count": 0,
                    "missing_since": None,
                    "sync_status": str(SyncStatus.SYNCED),
                    "sync_error": None,
                }
            )
        return item

    # -- 欠落・取り残し(orphan)検出 ------------------------------------------

    async def detect_missing_posts(
        self,
        *,
        all_posts: Sequence[Mapping[str, Any]],
        category_path: str | None,
        full_sync_succeeded: bool,
        prune_orphans: bool = False,
        fetch_post: Callable[[int], Awaitable[Any]] | None = None,
        check_lease: Callable[[], None] | None = None,
    ) -> list[SyncItem]:
        """取得元一覧に無くなった文書/カテゴリ移動で取り残された文書を扱う。

        旧実装 `detectMissingPosts` の移植。一覧不在だけでは削除と判定しない
        (設計原則5)。`decide_missing_candidate` が3条件すべてを要求する。

        `check_lease` は `save_post` の書き込みループ(`sync_service._sync_categories`)
        と同じ契約: このループは取り残し(orphan)ファイルの実削除・欠落カウントの
        DB更新という1件ずつの副作用を繰り返すため、次の副作用の前に毎回呼ぶ
        (`JobRunContext.check_lease` の docstring 参照)。削除は書き込みより
        取り返しがつかないため、この対応漏れは書き込みループの対応漏れより
        深刻(レビュー指摘、fix2 で書き込みループのみ対応され本ループは
        取り残されていた)。
        """
        expected_paths: dict[int, str] = {}
        for post in all_posts:
            number = post.get("number")
            if number is None:
                continue
            _, file_path = resolve_post_path(
                post, root_dir=self._root_dir, output_dir=self._output_dir, docs_dir=self._docs_dir
            )
            rel = docs_relative_path(file_path, self._docs_dir)
            if rel:
                expected_paths[number] = rel
        seen_numbers = {p.get("number") for p in all_posts if p.get("number") is not None}

        output_rel = docs_relative_path(self._root_dir / self._output_dir, self._docs_dir)
        path_prefix = output_rel or None

        try:
            tracked = self._documents.list(
                source=Source.ESA.value,
                path_prefix=path_prefix,
                category_prefix=category_path,
            )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"欠落判定のための一覧取得に失敗しました: {exc}")
            return []

        results: list[SyncItem] = []
        for row in tracked:
            # 次の副作用(取り残しの実削除・欠落カウントの更新)の前に毎回確認する
            # (`JobRunContext.check_lease` の契約、fix2 の書き込みループと同じ理由)。
            if check_lease is not None:
                check_lease()
            post_number = row.get("post_number")
            path = row["path"]

            # --- 取得元にはあるが保存先パスが変わった(カテゴリ移動・リネーム) ---
            if post_number and post_number in expected_paths:
                expected = expected_paths[post_number]
                if expected and path != expected:
                    results.append(
                        SyncItem(
                            path=path,
                            file_path=path,
                            post_number=post_number,
                            title="",
                            action=str(SyncAction.ORPHAN),
                            reason=(
                                f"取得元でカテゴリが変わり、現在の保存先は {expected} "
                                "です。この旧パスのファイルは取り残しです"
                            ),
                            write=False,
                            expected_path=expected,
                        )
                    )
                    if prune_orphans:
                        try:
                            (self._docs_dir / path).unlink(missing_ok=True)
                            self._documents.delete(path)
                        except Exception as exc:  # noqa: BLE001
                            self._warn(f"取り残しの削除に失敗しました ({path}): {exc}")
                    continue

            if not post_number or post_number in seen_numbers:
                if row.get("missing_count", 0) and row["missing_count"] > 0:
                    self._safe_clear_missing(path)
                continue

            missing_count = self._safe_mark_missing(path)

            if missing_count < self._missing_threshold:
                verdict = decide_missing_candidate(
                    full_sync_succeeded=full_sync_succeeded,
                    missing_count=missing_count,
                    individual_fetch_failed=True,
                    threshold=self._missing_threshold,
                )
                results.append(
                    SyncItem(
                        path=path,
                        file_path=path,
                        post_number=post_number,
                        title="",
                        action=str(SyncAction.MISSING),
                        reason=verdict.reason,
                        write=False,
                    )
                )
                continue

            individual_fetch_failed = False
            fetch_error: str | None = None
            if fetch_post is not None:
                try:
                    await fetch_post(post_number)
                except Exception as exc:  # noqa: BLE001 - 取得失敗は判定材料であり伝播しない
                    individual_fetch_failed = True
                    fetch_error = str(exc)
            else:
                individual_fetch_failed = True

            verdict = decide_missing_candidate(
                full_sync_succeeded=full_sync_succeeded,
                missing_count=missing_count,
                individual_fetch_failed=individual_fetch_failed,
                threshold=self._missing_threshold,
            )

            if verdict.source_missing:
                self._safe_upsert(
                    {
                        "path": path,
                        "sync_status": str(SyncStatus.SOURCE_MISSING),
                        "sync_error": fetch_error,
                    }
                )
            elif not individual_fetch_failed:
                self._safe_clear_missing(path)

            results.append(
                SyncItem(
                    path=path,
                    file_path=path,
                    post_number=post_number,
                    title="",
                    action=str(SyncAction.MISSING),
                    reason=verdict.reason,
                    write=False,
                    error=fetch_error,
                )
            )
        return results


__all__ = [
    "DEFAULT_MISSING_THRESHOLD",
    "EsaClient",
    "EsaSyncRunner",
    "MissingDecision",
    "SyncItem",
    "category_search_queries",
    "generate_front_matter",
    "resolve_post_path",
]
