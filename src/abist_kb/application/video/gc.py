"""動画成果物の世代管理（GC）。

**既定は dry-run。** 実削除は `confirmed=True` を明示したときだけ。動画1本は
数百 MB〜数 GB になるので消したくなるが、消した後に「あの版の承認済み動画が要る」
となっても復元できない。

削除しないもの:

- `data/` 配下（秘密・DB）は**そもそも走査対象にしない**
- 保護指定（`keep.txt` がある、または承認が有効）のプロジェクト
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from abist_kb.application.video.approval import verify_approval
from abist_kb.application.video.project_store import read_state
from abist_kb.infrastructure.video.artifact_store import videos_dir

#: 既定の保持世代数（最新 N 件は必ず残す）。
DEFAULT_KEEP_LATEST = 10
#: 既定の保持日数。
DEFAULT_TTL_DAYS = 90
#: このファイルがあるプロジェクトは削除しない。
KEEP_MARKER = "keep.txt"


@dataclass(frozen=True, slots=True)
class Candidate:
    video_id: str
    path: Path
    size_bytes: int
    updated_at: datetime | None
    protected: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "size_bytes": self.size_bytes,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "protected": self.protected,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class GcResult:
    dry_run: bool
    deleted: list[Candidate] = field(default_factory=list)
    kept: list[Candidate] = field(default_factory=list)
    freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "deleted": [c.to_dict() for c in self.deleted],
            "kept": [c.to_dict() for c in self.kept],
            "freed_bytes": self.freed_bytes,
            "freed_mb": round(self.freed_bytes / 1_048_576, 1),
            "errors": list(self.errors),
        }


def directory_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _updated_at(project_dir: Path) -> datetime | None:
    state = read_state(project_dir) or {}
    raw = state.get("updated_at") or state.get("created_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    try:
        return datetime.fromtimestamp(project_dir.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def collect(reports_dir: Path) -> list[Candidate]:
    """`reports/videos/*` を走査する（**`data/` には触れない**）。"""
    root = videos_dir(reports_dir)
    if not root.is_dir():
        return []
    found: list[Candidate] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        protected, reason = _protection(entry)
        found.append(
            Candidate(
                video_id=entry.name,
                path=entry,
                size_bytes=directory_size(entry),
                updated_at=_updated_at(entry),
                protected=protected,
                reason=reason,
            )
        )
    return found


def _protection(project_dir: Path) -> tuple[bool, str]:
    if (project_dir / KEEP_MARKER).is_file():
        return True, f"{KEEP_MARKER} により保護"
    if verify_approval(project_dir).ok:
        return True, "有効な承認レコードがあるため保護"
    return False, ""


def plan_gc(
    reports_dir: Path,
    *,
    keep_latest: int = DEFAULT_KEEP_LATEST,
    ttl_days: int = DEFAULT_TTL_DAYS,
    now: datetime | None = None,
) -> tuple[list[Candidate], list[Candidate]]:
    """削除候補と保持対象を返す（**この関数は何も消さない**）。"""
    stamp = now or datetime.now(UTC)
    cutoff = stamp - timedelta(days=max(0, ttl_days))
    candidates = collect(reports_dir)

    # 新しい順に並べ、先頭 keep_latest 件は無条件で残す
    ordered = sorted(
        candidates,
        key=lambda c: c.updated_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    keep: list[Candidate] = []
    remove: list[Candidate] = []
    for index, candidate in enumerate(ordered):
        recent = candidate.updated_at is not None and candidate.updated_at >= cutoff
        # 保護指定 / 最新 N 件 / TTL 内 のどれかに当たれば残す
        if candidate.protected or index < keep_latest or recent:
            keep.append(candidate)
        else:
            remove.append(candidate)
    return remove, keep


def run_gc(
    reports_dir: Path,
    *,
    keep_latest: int = DEFAULT_KEEP_LATEST,
    ttl_days: int = DEFAULT_TTL_DAYS,
    confirmed: bool = False,
    now: datetime | None = None,
) -> GcResult:
    """世代管理を実行する。`confirmed=False`（既定）なら削除しない。"""
    remove, keep = plan_gc(reports_dir, keep_latest=keep_latest, ttl_days=ttl_days, now=now)
    if not confirmed:
        return GcResult(
            dry_run=True,
            deleted=remove,
            kept=keep,
            freed_bytes=sum(c.size_bytes for c in remove),
        )

    deleted: list[Candidate] = []
    errors: list[str] = []
    for candidate in remove:
        try:
            shutil.rmtree(candidate.path)
        except OSError as exc:
            errors.append(f"{candidate.video_id}: {exc}")
            continue
        deleted.append(candidate)
    return GcResult(
        dry_run=False,
        deleted=deleted,
        kept=keep,
        freed_bytes=sum(c.size_bytes for c in deleted),
        errors=errors,
    )


__all__ = [
    "DEFAULT_KEEP_LATEST",
    "DEFAULT_TTL_DAYS",
    "KEEP_MARKER",
    "Candidate",
    "GcResult",
    "collect",
    "directory_size",
    "plan_gc",
    "run_gc",
]
