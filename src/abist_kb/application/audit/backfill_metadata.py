"""`backfill-metadata` の移植(旧 `tools/backfill-metadata.js`, M7 task-2)。

**唯一書込を行う監査。そのぶん防御的に作る**(旧実装のコメントをそのまま踏襲):

  - 既定は dry-run。`apply=True` を明示しない限りファイルにも DB にも書かない。
  - 書き込むのは `source` / `managed_by` / `document_type` / `status` の4キーのみ。
  - **既に frontmatter に値があるキーには触れない**(人間が決めた値を機械が
    上書きしない)。`documents` テーブルの値で埋めるのは frontmatter に
    そのキーが無い(または空)場合だけ。
  - 書き込み後に必ずファイルを読み直して本文ハッシュを照合し、一致しなければ
    その場で元に戻す(ロールバック)。

`documents` テーブルに記録が無い文書は対象にしない(何を書けばよいか機械的に
決められないため。旧実装の分類ロジック `classifyDocument` は今回移植の対象外
とし、既存の `documents` レコードを正とする方式へ簡略化している。詳細は
task-1 の報告書を参照)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.application.audit.runs import Finding, finish_run, record_findings, start_run
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.frontmatter import hash_body, parse_frontmatter, set_frontmatter_values
from abist_kb.infrastructure.db.documents_repo import DocumentRepository

#: 書込対象の4キー(旧実装 `BACKFILL_KEYS` と同一)。
BACKFILL_KEYS: tuple[str, ...] = ("source", "managed_by", "document_type", "status")


@dataclass(slots=True)
class BackfillPlan:
    path: str
    action: str  # "write" | "unchanged" | "missing_record" | "error"
    values: dict[str, Any] = field(default_factory=dict)
    body_hash: str | None = None
    error: str | None = None


def plan_document(path: str, content: str, record: dict[str, Any] | None) -> BackfillPlan:
    """1文書分の書込計画を作る(ファイルは書かない)。"""
    if record is None:
        return BackfillPlan(path=path, action="missing_record")

    existing = parse_frontmatter(content).data
    proposed = {
        key: record.get(key)
        for key in BACKFILL_KEYS
        if not existing.get(key) and record.get(key) is not None
    }
    if not proposed:
        return BackfillPlan(path=path, action="unchanged", body_hash=hash_body(content))

    return BackfillPlan(path=path, action="write", values=proposed, body_hash=hash_body(content))


@dataclass(slots=True)
class BackfillTotals:
    scanned: int = 0
    written: int = 0
    unchanged: int = 0
    missing_record: int = 0
    errors: int = 0
    rolled_back: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "written": self.written,
            "unchanged": self.unchanged,
            "missing_record": self.missing_record,
            "errors": self.errors,
            "rolled_back": self.rolled_back,
        }


@dataclass(slots=True)
class BackfillResult:
    run_id: str
    mode: str
    totals: BackfillTotals
    plans: list[BackfillPlan] = field(default_factory=list)


class BackfillMetadataService:
    """4キーの前方補完。`apply=False`(既定)では計画だけを返し何も書かない。"""

    def __init__(self, conn: sqlite3.Connection, *, docs_dir: Path) -> None:
        self._conn = conn
        self._docs_dir = docs_dir
        self._repo = DocumentRepository(conn)

    def run(
        self,
        *,
        paths: list[str] | None = None,
        apply: bool = False,
        check_lease: Callable[[], None] | None = None,
    ) -> BackfillResult:
        targets = paths if paths is not None else [row["path"] for row in self._repo.list()]
        run_id = start_run(
            self._conn,
            audit_type="backfill-metadata",
            mode="apply" if apply else "dry-run",
            params={"apply": apply, "path_count": len(targets)},
        )

        totals = BackfillTotals()
        plans: list[BackfillPlan] = []

        for path in targets:
            if check_lease is not None:
                check_lease()
            totals.scanned += 1
            absolute = self._docs_dir / path
            try:
                content = absolute.read_text(encoding="utf-8")
            except OSError as exc:
                totals.errors += 1
                plan = BackfillPlan(path=path, action="error", error=str(exc))
                plans.append(plan)
                continue

            record = self._repo.get(path)
            plan = plan_document(path, content, record)

            if plan.action == "unchanged":
                totals.unchanged += 1
            elif plan.action == "missing_record":
                totals.missing_record += 1
            elif plan.action == "write" and apply:
                self._apply_write(absolute, content, plan, totals)
                if plan.action == "write":
                    totals.written += 1
                    self._repo.upsert({"path": path, **plan.values})
            # dry-run(apply=False)では書込予定であっても totals.written は増やさない

            plans.append(plan)

        record_findings(
            self._conn,
            run_id,
            (
                Finding(
                    finding_type=p.action,
                    path=p.path,
                    details={"values": p.values, "error": p.error},
                )
                for p in plans
                if p.action in ("write", "error")
            ),
        )
        finish_run(self._conn, run_id, totals=totals.as_dict())
        return BackfillResult(
            run_id=run_id, mode="apply" if apply else "dry-run", totals=totals, plans=plans
        )

    def _apply_write(
        self, absolute: Path, original_content: str, plan: BackfillPlan, totals: BackfillTotals
    ) -> None:
        """書込 → 読み直し → ハッシュ照合 → 不一致ならロールバック(受入条件2)。"""
        result = set_frontmatter_values(original_content, plan.values)
        absolute.write_text(result.text, encoding="utf-8")
        written = absolute.read_text(encoding="utf-8")
        after_hash = hash_body(written)
        if after_hash != plan.body_hash:
            absolute.write_text(original_content, encoding="utf-8")
            totals.rolled_back += 1
            totals.errors += 1
            plan.action = "error"
            plan.error = "本文ハッシュ不一致のため書き込みを取り消しました(ロールバック済み)"


# --------------------------------------------------------------------------
# ジョブ基盤への配線(`docs-write` リースの下で実行する。書込を伴う唯一の監査)
# --------------------------------------------------------------------------


def run_backfill_inline(
    service: BackfillMetadataService,
    conn: sqlite3.Connection,
    *,
    apply: bool,
    paths: list[str] | None = None,
    confirmed: bool = False,
) -> BackfillResult:
    """CLI 既定の実行経路。`apply=True` は §12 の破壊的操作の作法に従う:
    呼び出し側が事前にスコープ(対象件数)を提示し確認を得てから、ここへは
    `confirmed=True` で渡すこと。ここでは最終防衛として再チェックするのみ。
    """
    if apply and not confirmed:
        raise AppError(
            code=ErrorCode.INVALID_INPUT,
            message="--apply の実行には確認が必要です(--yes を指定するか対話で確認してください)。",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return service.run(paths=paths, apply=apply)


__all__ = [
    "BACKFILL_KEYS",
    "BackfillMetadataService",
    "BackfillPlan",
    "BackfillResult",
    "BackfillTotals",
    "plan_document",
    "run_backfill_inline",
]
