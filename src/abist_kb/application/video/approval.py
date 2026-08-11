"""社内プレビュー承認（`approval.json`）—— **成果物ハッシュにバインドする**。

`confirmed=true` だけの承認は意味がない。承認後に再レンダーやメタデータ編集が
走れば、承認したものと今あるものは別物になる。そこで

    output.mp4 / video-metadata.json / qa-report.json / distribution

の SHA-256 をレコードへ焼き込み、**1つでも変わったら自動的に無効**にする。

用途は社内配布確認と手動投稿パックの版固定であって、**外部送信のゲートではない**
（この仕組みは何も送信しない）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from abist_kb.infrastructure.video.artifact_store import sha256_file, sha256_text

APPROVAL_FILE = "approval.json"

#: 承認の既定有効期間（日）。無期限にすると「いつの版か」が曖昧になる。
DEFAULT_VALID_DAYS = 7

#: ハッシュを取る対象（相対パス）。1つでも欠けたら承認できない。
BOUND_FILES: tuple[tuple[str, str], ...] = (
    ("output_mp4_sha256", "output.mp4"),
    ("video_metadata_sha256", "video-metadata.json"),
    ("qa_report_sha256", "qa-report.json"),
)

PURPOSE_INTERNAL_PREVIEW = "internal_preview"


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    ok: bool
    record: dict[str, Any] | None = None
    path: Path | None = None
    code: str | None = None
    message: str | None = None
    #: 一致しなかった項目（`APPROVAL_STALE` のときに何が変わったかを示す）。
    mismatched: list[str] = field(default_factory=list)


def _output_path(project_dir: Path) -> Path:
    """承認対象の動画（音声つきがあればそちら）。"""
    with_audio = project_dir / "output-with-audio.mp4"
    return with_audio if with_audio.is_file() else project_dir / "output.mp4"


def _distribution_digest(project_dir: Path) -> str | None:
    """`distribution` スナップショットのハッシュ（spec から取る）。"""
    try:
        spec = json.loads((project_dir / "project-spec.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    dist = spec.get("distribution") or {}
    # キー順を固定して、辞書の並び替えだけでハッシュが変わらないようにする
    return sha256_text(json.dumps(dist, ensure_ascii=False, sort_keys=True))


def current_digests(project_dir: Path) -> tuple[dict[str, str], list[str]]:
    """現在の成果物ハッシュ一式（欠けているものの一覧も返す）。"""
    digests: dict[str, str] = {}
    missing: list[str] = []
    for key, name in BOUND_FILES:
        path = _output_path(project_dir) if name == "output.mp4" else project_dir / name
        if path.is_file():
            digests[key] = sha256_file(path)
        else:
            missing.append(name)
    distribution = _distribution_digest(project_dir)
    if distribution is None:
        missing.append("project-spec.json")
    else:
        digests["distribution_sha256"] = distribution
    return digests, missing


def approve(
    project_dir: Path,
    *,
    approver: str,
    purpose: str = PURPOSE_INTERNAL_PREVIEW,
    valid_days: int = DEFAULT_VALID_DAYS,
    now: datetime | None = None,
    qa_ok: bool | None = None,
) -> ApprovalResult:
    """承認レコードを書く。

    QA が FAIL のままなら承認しない（`QA_FAILED`）。成果物は残っているので、
    直してから承認し直せる。
    """
    stamp = now or datetime.now(UTC)
    digests, missing = current_digests(project_dir)
    if missing:
        return ApprovalResult(
            ok=False,
            code="MANUAL_PACK_INCOMPLETE",
            message="承認に必要な成果物が揃っていません: " + " / ".join(missing),
            mismatched=missing,
        )

    if qa_ok is None:
        try:
            qa = json.loads((project_dir / "qa-report.json").read_text(encoding="utf-8"))
            qa_ok = bool(qa.get("ok"))
        except (OSError, json.JSONDecodeError):
            qa_ok = False
    if not qa_ok:
        return ApprovalResult(
            ok=False,
            code="QA_FAILED",
            message="QA の自動検査が FAIL のままです。直してから承認してください"
            "（成果物は残っています）",
        )

    record = {
        "schema_version": "1.0",
        "approval_id": str(uuid.uuid4()),
        "approved_at": stamp.isoformat(),
        "approver": approver,
        "expires_at": (stamp + timedelta(days=valid_days)).isoformat(),
        "purpose": purpose,
        **digests,
    }
    path = project_dir / APPROVAL_FILE
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return ApprovalResult(ok=True, record=record, path=path)


def load_approval(project_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((project_dir / APPROVAL_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_approval(project_dir: Path, *, now: datetime | None = None) -> ApprovalResult:
    """承認がまだ有効か検証する。

    再レンダー・メタデータ編集・QA 再実行・distribution 変更のいずれが起きても
    ハッシュが変わるので、**自動的に `APPROVAL_STALE` になる**。
    """
    record = load_approval(project_dir)
    if record is None:
        return ApprovalResult(ok=False, code="NOT_APPROVED", message="承認レコードがありません")

    stamp = now or datetime.now(UTC)
    expires_raw = str(record.get("expires_at") or "")
    try:
        expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    except ValueError:
        return ApprovalResult(
            ok=False, code="APPROVAL_STALE", message="承認レコードの期限を読めません"
        )
    if stamp >= expires:
        return ApprovalResult(
            ok=False,
            code="APPROVAL_STALE",
            message=f"承認の有効期限が切れています（{expires_raw}）",
        )

    digests, missing = current_digests(project_dir)
    if missing:
        return ApprovalResult(
            ok=False,
            code="APPROVAL_STALE",
            message="承認時の成果物が欠けています: " + " / ".join(missing),
            mismatched=missing,
        )
    mismatched = [key for key, value in digests.items() if record.get(key) != value]
    if mismatched:
        return ApprovalResult(
            ok=False,
            code="APPROVAL_STALE",
            message="承認後に成果物が変更されています: " + " / ".join(sorted(mismatched)),
            mismatched=sorted(mismatched),
            record=record,
        )
    return ApprovalResult(ok=True, record=record, path=project_dir / APPROVAL_FILE)


def invalidate(project_dir: Path, reason: str) -> bool:
    """承認を明示的に無効化する（部分再生成の後などに呼ぶ）。

    ファイルを消さずに `revoked` を立てるのは、**いつ・なぜ無効になったか**を
    後から追えるようにするため。
    """
    record = load_approval(project_dir)
    if record is None:
        return False
    record["revoked"] = True
    record["revoked_reason"] = reason
    record["revoked_at"] = datetime.now(UTC).isoformat()
    (project_dir / APPROVAL_FILE).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True


__all__ = [
    "APPROVAL_FILE",
    "BOUND_FILES",
    "DEFAULT_VALID_DAYS",
    "PURPOSE_INTERNAL_PREVIEW",
    "ApprovalResult",
    "approve",
    "current_digests",
    "invalidate",
    "load_approval",
    "verify_approval",
]
