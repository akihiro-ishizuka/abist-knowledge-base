"""esa 同期のオーケストレーション(設計書 §10.2, §11.2, task-3-brief)。

`docs-write` リソースリースの下で実行し、ジョブ基盤(`infrastructure.jobs`)経由の
進捗通知・レポート出力までを一括して面倒を見る。HTTP通信・front matter生成・
差分判定・欠落判定は `infrastructure.sources.esa` に委ね、ここでは「1つの
source/batch/全batchを1回同期する」というユースケースの組み立てだけを持つ。

**レポートは旧 `sync-report.js` と互換な形式**(`reports/sync/sync-esa-<label>-
<timestamp>.json`)で書き出す。`ACTION_BUCKET`(`domain.sync_policy`)を経由した
`totals`/`actionCounts`/`items` の3点セットは旧実装のフィールド名
(camelCase)をそのまま踏襲する — 旧レポートを読む人間・ツールが変わらず
使えるようにするため。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.job import ResourceKind
from abist_kb.domain.sync_policy import ACTION_BUCKET, SyncAction
from abist_kb.infrastructure.db.batches_repo import BatchRepository
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.db.sources_repo import SourceRepository
from abist_kb.infrastructure.jobs.supervisor import JobRunContext
from abist_kb.infrastructure.sources import git as git_module
from abist_kb.infrastructure.sources import web as web_module
from abist_kb.infrastructure.sources.base import with_docs_prefix
from abist_kb.infrastructure.sources.esa import (
    DEFAULT_MISSING_THRESHOLD,
    EsaClient,
    EsaSyncRunner,
    SyncItem,
    category_search_queries,
)
from abist_kb.infrastructure.sources.git import GitSyncRunner
from abist_kb.infrastructure.sources.web import WebSyncRunner

#: `JobService(resource_for_kind=...)` へそのまま渡せる既定リソース要求。
#: 設計書 §10.2 の「バッチ・sync を全プロセス横断で直列化する」に従い、
#: すべての esa 同期ジョブは `docs-write` の単一区画を取り合う。
BUILTIN_SYNC_RESOURCES: dict[str, tuple[ResourceKind, str | None]] = {
    "sync": (ResourceKind.DOCS_WRITE, None),
}


@dataclass(slots=True)
class SyncSummary:
    """旧 `newSyncSummary`(`sync-planner.js`)と同じ形の同期サマリ。"""

    source: str
    started_at: str
    finished_at: str | None = None
    full_sync_succeeded: bool | None = None
    totals: dict[str, int] = field(
        default_factory=lambda: {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "conflict": 0,
            "missing": 0,
            "error": 0,
        }
    )
    action_counts: dict[str, int] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    def to_report_dict(self) -> dict[str, Any]:
        """旧レポート形式(camelCase キー)。"""
        return {
            "source": self.source,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "fullSyncSucceeded": self.full_sync_succeeded,
            "totals": self.totals,
            "actionCounts": self.action_counts,
            "items": self.items,
            "options": self.options,
            "note": self.note,
        }


def new_sync_summary(source: str) -> SyncSummary:
    return SyncSummary(source=source, started_at=datetime.now(UTC).isoformat())


def _item_payload(item: SyncItem | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, SyncItem):
        return {
            "path": item.path,
            "filePath": item.file_path,
            "postNumber": item.post_number,
            "title": item.title,
            "action": item.action,
            "reason": item.reason,
            **({"error": item.error} if item.error else {}),
            **({"expectedPath": item.expected_path} if item.expected_path else {}),
        }
    return dict(item)


def _web_item_payload(item: web_module.SyncItem) -> dict[str, Any]:
    """web の `SyncItem` を旧 `download-web.js` の `recordSyncResult` 呼び出しと
    同じキー集合(path/url/action/reason、失敗時のみerror)へ変換する。"""
    payload: dict[str, Any] = {
        "path": item.path,
        "url": item.url,
        "action": item.action,
        "reason": item.reason,
    }
    if item.error:
        payload["error"] = item.error
    return payload


def _git_item_payload(item: git_module.SyncItem) -> dict[str, Any]:
    """git の `SyncItem` を旧 `download-git.js` の `recordSyncResult` 呼び出しと
    同じキー集合(path/action、必要に応じてreason/error)へ変換する。"""
    payload: dict[str, Any] = {"path": item.path, "action": item.action}
    if item.reason:
        payload["reason"] = item.reason
    if item.error:
        payload["error"] = item.error
    return payload


def record_sync_result(summary: SyncSummary, item: SyncItem | Mapping[str, Any]) -> SyncSummary:
    """判定結果をサマリへ足す(旧 `recordSyncResult` と同じ集計規則)。"""
    payload = _item_payload(item)
    action = payload.get("action")
    try:
        bucket = ACTION_BUCKET[SyncAction(action)] if action else None
    except ValueError:
        bucket = None
    if bucket is None:
        raise AppError(code=ErrorCode.INVALID_INPUT, message=f"未知の同期アクションです: {action}")
    summary.totals[bucket] = summary.totals.get(bucket, 0) + 1
    summary.action_counts[action] = summary.action_counts.get(action, 0) + 1
    summary.items.append(payload)
    return summary


def _sanitize_label(label: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\s]', "-", str(label))
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned[:80] or "sync"


def write_sync_report(summary: SyncSummary, *, reports_dir: Path, label: str) -> Path:
    """`reports/sync/sync-<source>-<label>-<timestamp>.json` へ書き出す。"""
    sync_dir = reports_dir / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    summary.finished_at = summary.finished_at or datetime.now(UTC).isoformat()
    stamp = re.sub(r"[:.]", "-", summary.finished_at)
    file_path = sync_dir / f"sync-{summary.source}-{_sanitize_label(label)}-{stamp}.json"
    file_path.write_text(
        json.dumps(summary.to_report_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return file_path


class SyncService:
    """esa ソース・バッチの同期を実行する(設計書 §10.2)。"""

    def __init__(
        self,
        *,
        root_dir: Path,
        docs_dir: Path,
        reports_dir: Path,
        documents: DocumentRepository,
        sources: SourceRepository,
        batches: BatchRepository,
        missing_threshold: int = DEFAULT_MISSING_THRESHOLD,
    ) -> None:
        self._root_dir = root_dir
        self._docs_dir = docs_dir
        self._reports_dir = reports_dir
        self._documents = documents
        self._sources = sources
        self._batches = batches
        self._missing_threshold = missing_threshold

    # -- ソース単位の同期 -----------------------------------------------------

    def _require_esa_source(self, source_id: str) -> dict[str, Any]:
        source = self._sources.get(source_id)
        if source is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"ソースが見つかりません: {source_id}")
        if source["type"] != "esa":
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=f"esa 以外のソースは sync source では扱えません: {source['type']}",
                exit_code=ExitCode.INVALID_INPUT,
            )
        connection = source.get("connection") or {}
        team = connection.get("team")
        token = connection.get("access_token")
        if not team or not token:
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message="ソースの接続設定に team/access_token が不足しています。",
                exit_code=ExitCode.INVALID_INPUT,
            )
        return source

    async def _sync_categories(
        self,
        *,
        client: EsaClient,
        runner: EsaSyncRunner,
        categories: list[str],
        summary: SyncSummary,
        prune_orphans: bool,
        emit: Any = None,
        check_lease: Any = None,
    ) -> None:
        # ここに到達するまでに一覧取得(search_posts)が例外を投げていれば
        # 呼び出し元(ジョブハンドラ)まで伝播して同期全体が失敗として終わる
        # (旧実装の「一覧取得の失敗は握りつぶさない」契約)。したがってこの関数が
        # 正常に完了した時点では、処理した全カテゴリの一覧取得は成功している。
        summary.full_sync_succeeded = True
        for category in categories:
            posts: list[dict[str, Any]] = []
            # 一覧取得(search_posts)の失敗は例外として上へ伝播させ、握りつぶさない
            # (旧実装のコメント通り: 欠落判定の前提が崩れるため)。
            for query in category_search_queries(category):
                found = await client.search_posts(query)
                if found:
                    posts = found
                    break

            if not posts:
                # 0件でも「カテゴリごと消えた」とは判定しない(設計原則5)。
                continue

            for index, post in enumerate(posts):
                # fix2: 1件ずつファイルを書く(`runner.save_post`)ループなので、
                # 次の書き込みを行う前に毎回リース生存確認を挟む
                # (`JobRunContext.check_lease` の契約、レビュー再現: リースを
                # 奪われた後も書き込みを続けてしまう不具合の直接の対象箇所)。
                if check_lease is not None:
                    check_lease()
                item = runner.save_post(post)
                record_sync_result(summary, item)
                if emit is not None:
                    emit(
                        phase="sync-esa",
                        current=index + 1,
                        total=len(posts),
                        message=f"{category}: {item.action}",
                        item=item.path or item.file_path,
                    )

            missing_items = await runner.detect_missing_posts(
                all_posts=posts,
                category_path=category,
                full_sync_succeeded=True,
                prune_orphans=prune_orphans,
                fetch_post=client.get_post,
                check_lease=check_lease,
            )
            for missing_item in missing_items:
                record_sync_result(summary, missing_item)

    async def _run_source_sync(
        self,
        source: Mapping[str, Any],
        *,
        categories: list[str],
        force: bool,
        dry_run: bool,
        prune_orphans: bool,
        emit: Any = None,
        check_lease: Any = None,
    ) -> SyncSummary:
        connection = source.get("connection") or {}
        summary = new_sync_summary("esa")
        summary.options = {
            "outputDir": source["output_dir"],
            "categories": categories,
            "force": force,
            "dryRun": dry_run,
            "pruneOrphans": prune_orphans,
        }
        runner = EsaSyncRunner(
            documents=self._documents,
            root_dir=self._root_dir,
            docs_dir=self._docs_dir,
            output_dir=source["output_dir"],
            force=force,
            dry_run=dry_run,
            missing_threshold=self._missing_threshold,
        )
        # `base_url` は接続設定の任意キー(既定は本物の esa.io API)。テストが
        # ローカルのモックサーバーへ向けるためだけに使う抜け道であり、通常運用の
        # ソース登録では設定しない。
        async with EsaClient(
            team=connection["team"],
            access_token=connection["access_token"],
            base_url=connection.get("base_url"),
        ) as client:
            await self._sync_categories(
                client=client,
                runner=runner,
                categories=categories,
                summary=summary,
                prune_orphans=prune_orphans and not dry_run,
                emit=emit,
                check_lease=check_lease,
            )
        summary.finished_at = datetime.now(UTC).isoformat()
        return summary

    def sync_source(
        self,
        source_id: str,
        *,
        categories: list[str] | None = None,
        force: bool = False,
        dry_run: bool = False,
        prune_orphans: bool = False,
        emit: Any = None,
        check_lease: Any = None,
    ) -> tuple[SyncSummary, Path | None]:
        """1つのソースを同期する(種別で分岐、esa 以外は `categories` を使わない)。

        web/git には esa の「カテゴリ」に相当する概念が無い(旧
        `batch-config.js` でも web/git エントリはURL/リポジトリを1件だけ持つ)。
        `sources.connection` そのものを1件のクロール/ミラー対象として扱う。
        """
        source = self._sources.get(source_id)
        if source is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"ソースが見つかりません: {source_id}")

        if source["type"] == "esa":
            source = self._require_esa_source(source_id)
            if not categories:
                raise AppError(
                    code=ErrorCode.INVALID_INPUT,
                    message="esa ソースの同期には --category が1つ以上必要です。",
                    exit_code=ExitCode.INVALID_INPUT,
                )
            summary = asyncio.run(
                self._run_source_sync(
                    source,
                    categories=categories,
                    force=force,
                    dry_run=dry_run,
                    prune_orphans=prune_orphans,
                    emit=emit,
                    check_lease=check_lease,
                )
            )
            if dry_run:
                return summary, None
            label = categories[0] if len(categories) == 1 else source["display_name"]
            report_path = write_sync_report(summary, reports_dir=self._reports_dir, label=label)
            return summary, report_path

        # web/git: ソース自体を1件だけの疑似アイテムとして扱う(バッチと同じ
        # `_sync_web_target`/`_sync_git_target` へ委譲し、経路を1本化する)。
        pseudo_item = {"options": dict(source.get("connection") or {})}
        if source["type"] == "web":
            return self._sync_web_target(
                items=[pseudo_item],
                batch_output_dir=source["output_dir"],
                label=source["display_name"],
                force=force,
                dry_run=dry_run,
                emit=emit,
                check_lease=check_lease,
            )
        if source["type"] == "git":
            return self._sync_git_target(
                items=[pseudo_item],
                batch_output_dir=source["output_dir"],
                label=source["display_name"],
                dry_run=dry_run,
                emit=emit,
                check_lease=check_lease,
            )
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"未知のソース種別です: {source['type']}",
            exit_code=ExitCode.INVALID_INPUT,
        )

    # -- バッチ単位の同期 -----------------------------------------------------

    def _resolve_batch_source_id(self, batch: Mapping[str, Any]) -> str:
        item_source_ids = {
            item.get("source_id") for item in batch.get("items", []) if item.get("source_id")
        }
        if len(item_source_ids) == 1:
            return next(iter(item_source_ids))
        esa_sources = [s for s in self._sources.list() if s["type"] == "esa" and s["enabled"]]
        if len(esa_sources) == 1:
            return esa_sources[0]["id"]
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message="バッチの対象に紐づく esa ソースを一意に決められません。",
            hint=(
                "`batch edit --items` で各対象に source_id を指定するか、"
                "有効な esa ソースを1件だけにしてください。"
            ),
            exit_code=ExitCode.INVALID_INPUT,
        )

    def _effective_options(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """バッチアイテムの実効オプションを組み立てる: `source_id` があれば参照
        先ソースの `connection`/`output_dir` を土台にし、アイテム自身の
        `options` で上書きする(schema docstring: 「web/gitバッチは対象1件に
        つき1行(source_id/optionsにURL・リポジトリ等を持たせ...)」)。
        両方とも無ければ `options` だけを使う(旧設定からの一方向インポートが
        作る、source参照を持たない自己完結バッチの形)。
        """
        base: dict[str, Any] = {}
        source_id = item.get("source_id")
        if source_id:
            source = self._sources.get(source_id)
            if source is not None:
                base.update(source.get("connection") or {})
                base.setdefault("output_dir", source.get("output_dir"))
        base.update(item.get("options") or {})
        return base

    def _sync_web_target(
        self,
        *,
        items: list[Mapping[str, Any]],
        batch_output_dir: str | None,
        label: str,
        force: bool = False,
        dry_run: bool = False,
        emit: Any = None,
        check_lease: Any = None,
    ) -> tuple[SyncSummary, Path | None]:
        """web バッチ/ソースの対象(1件以上のURL)を巡回する。"""
        summary = new_sync_summary("web")
        summary.options = {"force": force, "dryRun": dry_run}
        overall_ok = True
        truncated: list[str] = []

        async def _run() -> None:
            nonlocal overall_ok
            for item in items:
                options = self._effective_options(item)
                url = options.get("url")
                if not url:
                    raise AppError(
                        code=ErrorCode.INVALID_INPUT,
                        message="web バッチ/ソースの対象に url が設定されていません。",
                        exit_code=ExitCode.INVALID_INPUT,
                    )
                # バッチ/ソースの output_dir を優先する(`batch edit
                # --output-dir` の変更が確実に効くようにするため)。アイテムの
                # options.output_dir は旧設定インポート由来の値へのフォール
                # バックとしてのみ使う。
                output_dir = batch_output_dir or options.get("output_dir")
                if not output_dir:
                    raise AppError(
                        code=ErrorCode.INVALID_INPUT,
                        message="web バッチ/ソースの出力先(output_dir)が決まりません。",
                        exit_code=ExitCode.INVALID_INPUT,
                    )
                runner = WebSyncRunner(
                    documents=self._documents,
                    root_dir=self._root_dir,
                    docs_dir=self._docs_dir,
                    output_dir=output_dir,
                    base_domain=url,
                    force=force,
                    dry_run=dry_run,
                    max_depth=int(options.get("max_depth", web_module.DEFAULT_MAX_DEPTH)),
                    max_pages=int(options.get("max_pages", web_module.DEFAULT_MAX_PAGES)),
                    max_size_bytes=int(
                        options.get("max_size_bytes", web_module.DEFAULT_MAX_SIZE_BYTES)
                    ),
                    timeout_seconds=float(
                        options.get("timeout_seconds", web_module.DEFAULT_TIMEOUT_SECONDS)
                    ),
                )
                result = await runner.crawl(
                    url,
                    concurrency=int(options.get("concurrency", web_module.DEFAULT_CONCURRENCY)),
                    check_lease=check_lease,
                    emit=emit,
                )
                for web_item in result.items:
                    record_sync_result(summary, _web_item_payload(web_item))
                overall_ok = overall_ok and result.full_sync_succeeded
                if result.pages_over_limit:
                    truncated.append(
                        f"{url}: 最大ページ数の上限に達したため"
                        f"{result.pages_over_limit}件のURLが未処理です。"
                    )

        asyncio.run(_run())
        summary.full_sync_succeeded = overall_ok
        if truncated:
            summary.note = " / ".join(truncated)
        summary.finished_at = datetime.now(UTC).isoformat()
        if dry_run:
            return summary, None
        report_path = write_sync_report(summary, reports_dir=self._reports_dir, label=label)
        return summary, report_path

    def _sync_git_target(
        self,
        *,
        items: list[Mapping[str, Any]],
        batch_output_dir: str | None,
        label: str,
        dry_run: bool = False,
        emit: Any = None,
        check_lease: Any = None,
    ) -> tuple[SyncSummary, Path | None]:
        """git バッチ/ソースの対象(1件以上のリポジトリ)をミラーする。

        旧 `download-git.js` には `--dry-run`/`--force` に相当する機能が無い
        (`GitSyncRunner` も持たない)。`dry_run=True` を明示的に要求された場合、
        「実際には書き込むのに書き込まないと誤解させる」よりも即座に拒否する。
        """
        if dry_run:
            raise AppError(
                code=ErrorCode.INVALID_INPUT,
                message=(
                    "git バッチ/ソースは --dry-run に対応していません(旧実装にも無い機能です)。"
                ),
                exit_code=ExitCode.INVALID_INPUT,
            )
        summary = new_sync_summary("git")
        summary.options = {}
        overall_ok = True
        for item in items:
            options = self._effective_options(item)
            repository = options.get("repository")
            if not repository:
                raise AppError(
                    code=ErrorCode.INVALID_INPUT,
                    message="git バッチ/ソースの対象に repository が設定されていません。",
                    exit_code=ExitCode.INVALID_INPUT,
                )
            output_dir = options.get("output_dir") or batch_output_dir
            if not output_dir:
                raise AppError(
                    code=ErrorCode.INVALID_INPUT,
                    message="git バッチ/ソースの出力先(output_dir)が決まりません。",
                    exit_code=ExitCode.INVALID_INPUT,
                )
            runner = GitSyncRunner(
                documents=self._documents,
                root_dir=self._root_dir,
                docs_dir=self._docs_dir,
                output_dir=output_dir,
                repository=repository,
                branch=options.get("branch"),
            )
            result = runner.sync(check_lease=check_lease, emit=emit)
            for git_item in result.items:
                record_sync_result(summary, _git_item_payload(git_item))
            overall_ok = overall_ok and result.full_sync_succeeded
            if result.error:
                summary.note = f"{summary.note} / {result.error}" if summary.note else result.error
        summary.full_sync_succeeded = overall_ok
        summary.finished_at = datetime.now(UTC).isoformat()
        report_path = write_sync_report(summary, reports_dir=self._reports_dir, label=label)
        return summary, report_path

    def sync_batch(
        self,
        batch_id: str,
        *,
        force: bool = False,
        dry_run: bool = False,
        prune_orphans: bool = False,
        emit: Any = None,
        check_lease: Any = None,
    ) -> tuple[SyncSummary, Path | None]:
        """バッチに登録された全対象を、バッチ種別に応じたアダプターで同期する。"""
        batch = self._batches.get(batch_id)
        if batch is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"バッチが見つかりません: {batch_id}")

        if batch["type"] == "esa":
            categories = [item["target"] for item in batch["items"] if item.get("target")]
            source_id = self._resolve_batch_source_id(batch)
            source = self._require_esa_source(source_id)
            output_dir = batch.get("output_dir") or with_docs_prefix(source["output_dir"])
            source_for_sync = dict(source)
            source_for_sync["output_dir"] = output_dir

            summary = asyncio.run(
                self._run_source_sync(
                    source_for_sync,
                    categories=categories,
                    force=force,
                    dry_run=dry_run,
                    prune_orphans=prune_orphans,
                    emit=emit,
                    check_lease=check_lease,
                )
            )
            if dry_run:
                return summary, None
            report_path = write_sync_report(
                summary, reports_dir=self._reports_dir, label=batch["name"]
            )
            return summary, report_path

        if batch["type"] == "web":
            return self._sync_web_target(
                items=batch["items"],
                batch_output_dir=batch.get("output_dir"),
                label=batch["name"],
                force=force,
                dry_run=dry_run,
                emit=emit,
                check_lease=check_lease,
            )

        if batch["type"] == "git":
            return self._sync_git_target(
                items=batch["items"],
                batch_output_dir=batch.get("output_dir"),
                label=batch["name"],
                dry_run=dry_run,
                emit=emit,
                check_lease=check_lease,
            )

        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message=f"未知のバッチ種別です: {batch['type']}",
            exit_code=ExitCode.INVALID_INPUT,
        )

    def sync_all(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        prune_orphans: bool = False,
        emit: Any = None,
        check_lease: Any = None,
    ) -> list[dict[str, Any]]:
        """有効な全バッチ(esa/web/git)を同期する。

        **partial-failure 方針(task-5b の判断事項)**: 1バッチの失敗
        (到達不能なホスト・存在しないリポジトリ等)で残りのバッチを止めない。
        1件ずつ独立に実行し、失敗したバッチは `results` に `error` キー付きで
        記録して次のバッチへ進む。夜間バッチが十数件あるとき、1件の外部要因の
        失敗で残り全部を巻き込んで止めるべきではない(旧システムにこの種の
        「全バッチ一括実行」自体が無かったため、旧実装からの継承ではなく本タスクの
        新規判断)。呼び出し元(CLI `sync all`)は `results` に1件でも `error` が
        あれば `ExitCode.CONFLICT`(5、設計書の部分成功コード)で終了する。
        """
        results: list[dict[str, Any]] = []
        for batch in self._batches.list():
            if not batch["enabled"]:
                continue
            try:
                summary, report_path = self.sync_batch(
                    batch["id"],
                    force=force,
                    dry_run=dry_run,
                    prune_orphans=prune_orphans,
                    emit=emit,
                    check_lease=check_lease,
                )
            except AppError as exc:
                results.append(
                    {
                        "batch_id": batch["id"],
                        "batch_name": batch["name"],
                        "batch_type": batch["type"],
                        "error": exc.to_dict(),
                    }
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 1バッチの想定外failureで全体を止めない
                results.append(
                    {
                        "batch_id": batch["id"],
                        "batch_name": batch["name"],
                        "batch_type": batch["type"],
                        "error": {"code": "FAILURE", "message": str(exc)},
                    }
                )
                continue
            results.append(
                {
                    "batch_id": batch["id"],
                    "batch_name": batch["name"],
                    "batch_type": batch["type"],
                    "summary": summary.to_report_dict(),
                    "report_path": str(report_path) if report_path else None,
                }
            )
        return results


# --------------------------------------------------------------------------
# ジョブ基盤への配線(`docs-write` リースの下で実行する、brief Step 4)
# --------------------------------------------------------------------------


def _build_sync_service(
    conn: Any, *, root_dir: Path, docs_dir: Path, reports_dir: Path, missing_threshold: int
) -> SyncService:
    return SyncService(
        root_dir=root_dir,
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        documents=DocumentRepository(conn),
        sources=SourceRepository(conn),
        batches=BatchRepository(conn),
        missing_threshold=missing_threshold,
    )


def make_sync_job_handler(
    *,
    conn: Any,
    root_dir: Path,
    docs_dir: Path,
    reports_dir: Path,
    missing_threshold: int,
    outbox: dict[str, Any],
) -> Any:
    """`JobService(handlers={"sync": ...})` へ渡すハンドラを組み立てる。

    ジョブの `params` は `{"target": "source"|"batch"|"all", "id": str | None,
    "categories": list[str] | None, "force": bool, "dry_run": bool,
    "prune_orphans": bool}` を受け取る。実行はここで `docs-write` リース配下
    (`infrastructure.jobs.execution.run_job` 経由)に閉じ込められる。

    ジョブ基盤(`infrastructure.jobs`)は現時点でハンドラから `jobs.result` 列へ
    値を書き戻す経路を持たない(`run_job` の成功分岐は `repo.finish(..., state=
    SUCCEEDED)` を引数無しで呼ぶのみ)。インライン実行は同一プロセス・同一
    スレッドで完結するため、呼び出し側が渡す `outbox` 辞書へ直接書き込むことで
    サマリ・レポートパスを持ち帰る(ジョブ基盤自体には手を入れない)。
    """

    def handler(run: JobRunContext) -> None:
        params = run.job.params
        target = params.get("target", "source")
        service = _build_sync_service(
            conn,
            root_dir=root_dir,
            docs_dir=docs_dir,
            reports_dir=reports_dir,
            missing_threshold=missing_threshold,
        )
        force = bool(params.get("force", False))
        dry_run = bool(params.get("dry_run", False))
        prune_orphans = bool(params.get("prune_orphans", False))

        if target == "source":
            summary, report_path = service.sync_source(
                params["id"],
                categories=params.get("categories") or [],
                force=force,
                dry_run=dry_run,
                prune_orphans=prune_orphans,
                emit=run.emit,
                check_lease=run.check_lease,
            )
            outbox["summary"] = summary.to_report_dict()
            outbox["report_path"] = str(report_path) if report_path else None
            run.emit(
                phase=f"sync-{summary.source}",
                current=len(summary.items),
                total=len(summary.items),
                message="同期完了",
            )
        elif target == "batch":
            summary, report_path = service.sync_batch(
                params["id"],
                force=force,
                dry_run=dry_run,
                prune_orphans=prune_orphans,
                emit=run.emit,
                check_lease=run.check_lease,
            )
            outbox["summary"] = summary.to_report_dict()
            outbox["report_path"] = str(report_path) if report_path else None
        elif target == "all":
            results = service.sync_all(
                force=force,
                dry_run=dry_run,
                prune_orphans=prune_orphans,
                emit=run.emit,
                check_lease=run.check_lease,
            )
            outbox["results"] = results
            failed = [r for r in results if r.get("error")]
            # ここでは意図的に例外を送出しない(sync_all の partial-failure 方針:
            # 続行して結果へ記録する、モジュール docstring参照)。ジョブ自体は
            # SUCCEEDED として記録され、部分失敗を終了コードへ反映する責務は
            # CLI 層(`presentation.cli.sync_cmd.sync_all`)が `outbox["results"]`
            # を検査して負う。
            run.emit(
                phase="sync-all",
                current=len(results),
                total=len(results),
                message=(
                    f"{len(results)}バッチ中{len(failed)}件が失敗しました。"
                    if failed
                    else "全バッチ同期完了"
                ),
            )
        else:
            raise AppError(code=ErrorCode.INVALID_INPUT, message=f"未知の同期対象です: {target}")

    return handler


def run_sync_inline(
    conn: Any,
    *,
    root_dir: Path,
    docs_dir: Path,
    reports_dir: Path,
    missing_threshold: int = DEFAULT_MISSING_THRESHOLD,
    target: str,
    target_id: str | None = None,
    categories: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    prune_orphans: bool = False,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """CLI 既定の同期実行: `docs-write` リースの下でジョブ経由に実行する。"""
    # ローカル import: `application.job_service` は `infrastructure.jobs` へ
    # 依存するため、他サービスから常時 import すると循環しやすい箇所を避ける。
    from abist_kb.application.job_service import JobService

    outbox: dict[str, Any] = {}
    handler = make_sync_job_handler(
        conn=conn,
        root_dir=root_dir,
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        missing_threshold=missing_threshold,
        outbox=outbox,
    )
    job_service = JobService(
        conn,
        owner_id=owner_id or str(uuid.uuid4()),
        handlers={"sync": handler},
        resource_for_kind=BUILTIN_SYNC_RESOURCES,
    )
    job = job_service.run_inline(
        "sync",
        {
            "target": target,
            "id": target_id,
            "categories": categories,
            "force": force,
            "dry_run": dry_run,
            "prune_orphans": prune_orphans,
        },
    )
    return {
        "id": job.id,
        "kind": job.kind,
        "state": str(job.state),
        "params": job.params,
        "error": job.error,
        **outbox,
    }


__all__ = [
    "BUILTIN_SYNC_RESOURCES",
    "SyncService",
    "SyncSummary",
    "make_sync_job_handler",
    "new_sync_summary",
    "record_sync_result",
    "run_sync_inline",
    "write_sync_report",
]
