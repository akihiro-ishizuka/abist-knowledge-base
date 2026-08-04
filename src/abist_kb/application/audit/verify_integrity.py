"""`verify-integrity` の移植(旧 `tools/verify-integrity.js`, M7 task-2)。

`documents` テーブルの記録と実ファイルを突き合わせ、6状態のいずれかに分類する:

  ok               本文ハッシュが一致
  modified_local   自動管理文書(`managed_by` が human 以外)の本文がローカルで変更された
  manual_edited    手書き文書(`managed_by == "human"` または未設定)の本文が変更された
  metadata_changed 本文は同じだが frontmatter の `status` が DB と異なる
  missing_local    DB にはあるがファイルが無い
  untracked        ファイルはあるが DB に記録が無い

**設計原則5(削除誤判定の防止)を必ず守る**: ローカルにファイルが無いことは
「文書が削除された」ことの証拠ではない(一時的な読み取り失敗・退避作業中の
移動でも起こりうる)。ここでは `missing_local` という *候補* として記録するだけで、
`sync_status` を `deleted` 等へ遷移させることは絶対にしない
(`sync_status='error'` にして人間の確認を促すのみ)。

旧実装にあった `--check-source esa`(取得元一覧との突合)と `--prune-orphans`
(リネーム起因の取り残し検出・削除)はネットワーク依存・削除操作であり、
今回の移植スコープでは行わない(設計上の理由は §7 の全体申し送りを参照)。
6状態分類と「不在は削除の証拠ではない」の原則がこの監査の核であり、そこを
最優先で再現している。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.application.audit.runs import Finding, finish_run, record_findings, start_run
from abist_kb.domain.frontmatter import hash_body, parse_frontmatter
from abist_kb.domain.metadata_schema import to_posix_path
from abist_kb.infrastructure.db.documents_repo import DocumentRepository

STATES: tuple[str, ...] = (
    "ok",
    "modified_local",
    "manual_edited",
    "metadata_changed",
    "missing_local",
    "untracked",
)


@dataclass(slots=True)
class VerifyTotals:
    tracked: int = 0
    ok: int = 0
    modified_local: int = 0
    manual_edited: int = 0
    metadata_changed: int = 0
    missing_local: int = 0
    untracked: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "tracked": self.tracked,
            "ok": self.ok,
            "modified_local": self.modified_local,
            "manual_edited": self.manual_edited,
            "metadata_changed": self.metadata_changed,
            "missing_local": self.missing_local,
            "untracked": self.untracked,
            "errors": self.errors,
        }


@dataclass(slots=True)
class VerifyResult:
    run_id: str
    totals: VerifyTotals
    findings: list[dict[str, Any]] = field(default_factory=list)


def _collect_markdown_files(docs_dir: Path) -> Iterator[str]:
    if not docs_dir.is_dir():
        return
    for path in sorted(docs_dir.rglob("*.md")):
        if path.is_file():
            yield to_posix_path(str(path.relative_to(docs_dir)))


def classify_tracked_document(
    record: dict[str, Any], *, read_file: Callable[[str], str | None]
) -> tuple[str, dict[str, Any]]:
    """1件の DB 記録を実ファイルと突き合わせ、`(state, finding_details)` を返す。

    `read_file` はテスト差し替え用。存在しないファイルは `None` を返す契約
    (`missing_local` と、読み取り自体の失敗を区別しない— 旧実装の `ENOENT` 分岐と
    同じく、どちらも「取得元の欠落を意味しない候補」として扱う)。
    """
    path = record["path"]
    content = read_file(path)
    if content is None:
        return "missing_local", {
            "source": record.get("source"),
            "managed_by": record.get("managed_by"),
            "note": "DB に記録があるがローカルにファイルがありません(取得元の欠落とは限りません)",
        }

    actual_hash = hash_body(content)
    expected_hash = record.get("local_content_hash")
    is_managed = bool(record.get("managed_by")) and record.get("managed_by") != "human"

    if expected_hash and actual_hash == expected_hash:
        file_status = parse_frontmatter(content).data.get("status")
        db_status = record.get("status")
        if db_status and file_status and file_status != db_status:
            return "metadata_changed", {
                "source": record.get("source"),
                "managed_by": record.get("managed_by"),
                "db_status": db_status,
                "file_status": file_status,
                "note": (
                    f"frontmatter の status が DB と異なります"
                    f"(DB: {db_status} / ファイル: {file_status})"
                ),
            }
        return "ok", {}

    if is_managed:
        return "modified_local", {
            "source": record.get("source"),
            "managed_by": record.get("managed_by"),
            "expected_hash": expected_hash,
            "actual_hash": actual_hash,
            "note": "自動同期で上書きすると失われます",
        }
    return "manual_edited", {
        "source": record.get("source"),
        "managed_by": record.get("managed_by"),
        "expected_hash": expected_hash,
        "actual_hash": actual_hash,
        "note": "手書き文書のため同期対象外です",
    }


class VerifyIntegrityService:
    """`documents` と `docs_dir` を突き合わせる読み取り中心の監査。

    `update_db=True`(既定)のとき、実ファイルから判明した事実(本文ハッシュ・
    frontmatter の status)で DB を追従させる。これは「削除の確定」ではなく
    既に確認できた事実の反映であり、設計原則5には抵触しない。副作用のある
    ループなので `check_lease` を毎件呼べるようにしてある。
    """

    def __init__(self, conn: sqlite3.Connection, *, docs_dir: Path) -> None:
        self._conn = conn
        self._docs_dir = docs_dir
        self._repo = DocumentRepository(conn)

    def run(
        self,
        *,
        update_db: bool = True,
        include_untracked: bool = True,
        check_lease: Callable[[], None] | None = None,
    ) -> VerifyResult:
        run_id = start_run(
            self._conn,
            audit_type="verify-integrity",
            mode="apply" if update_db else "report",
            params={"update_db": update_db},
        )

        def read_file(relative_path: str) -> str | None:
            absolute = self._docs_dir / relative_path
            try:
                return absolute.read_text(encoding="utf-8")
            except OSError:
                return None

        totals = VerifyTotals()
        findings: list[dict[str, Any]] = []
        tracked = self._repo.list()
        totals.tracked = len(tracked)
        tracked_paths = {row["path"] for row in tracked}

        for record in tracked:
            if check_lease is not None:
                check_lease()
            state, details = classify_tracked_document(record, read_file=read_file)
            setattr(totals, state, getattr(totals, state) + 1)
            if state != "ok":
                findings.append({"type": state, "path": record["path"], **details})

            if not update_db:
                continue
            if state == "missing_local":
                # 設計原則5: 欠落は候補。sync_status は 'error' に留め、削除扱いにしない。
                self._repo.upsert(
                    {
                        "path": record["path"],
                        "sync_status": "error",
                        "sync_error": "local file missing",
                    }
                )
            elif state == "metadata_changed":
                self._repo.upsert(
                    {
                        "path": record["path"],
                        "status": details["file_status"],
                        "sync_status": "synced",
                        "sync_error": None,
                    }
                )
            elif state == "modified_local":
                self._repo.upsert({"path": record["path"], "sync_status": "modified_local"})
            elif state == "manual_edited":
                self._repo.upsert(
                    {"path": record["path"], "local_content_hash": details["actual_hash"]}
                )
            elif state == "ok" and record.get("sync_status") != "synced":
                self._repo.upsert(
                    {"path": record["path"], "sync_status": "synced", "sync_error": None}
                )

        if include_untracked:
            for relative_path in _collect_markdown_files(self._docs_dir):
                if check_lease is not None:
                    check_lease()
                if relative_path in tracked_paths:
                    continue
                totals.untracked += 1
                findings.append(
                    {
                        "type": "untracked",
                        "path": relative_path,
                        "note": (
                            "documents に記録がありません"
                            "(backfill 未実行、または新規に置かれたファイル)"
                        ),
                    }
                )

        record_findings(
            self._conn,
            run_id,
            (Finding(finding_type=f["type"], path=f.get("path"), details=f) for f in findings),
        )
        finish_run(self._conn, run_id, totals=totals.as_dict())

        return VerifyResult(run_id=run_id, totals=totals, findings=findings)


__all__ = [
    "STATES",
    "VerifyIntegrityService",
    "VerifyResult",
    "VerifyTotals",
    "classify_tracked_document",
]
