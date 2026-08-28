"""社内配布準備と公開候補判定（`distribution-report.json`）。

**このモジュールは何も送信しない。** YouTube API / OAuth / 自動アップロードは
実装しない（計画の将来バックログ）。ここが作るのは

1. `public_candidate` の機械判定（既定 false・降下のみ）
2. 人が手動アップロードするときに必要なファイル一式の所在

`public_candidate=true` の意味は「**公開審査へ提出できる**」であって
「公開してよい」ではない。文言も UI もそこを混同させない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DISTRIBUTION_REPORT_FILE = "distribution-report.json"

#: 手動投稿パックの必須ファイル（プロジェクトディレクトリからの相対）。
MANUAL_PACK_FILES: tuple[str, ...] = (
    "output.mp4",
    "thumbnail.png",
    "subtitles/narration.srt",
    "video-metadata.json",
    "citations.json",
    "qa-report.json",
)

#: 公開候補を強制的に false へ落とすソースの機微区分。
BLOCKING_SENSITIVITIES: frozenset[str] = frozenset(
    {"confidential", "pii", "internal_restricted", "secret"}
)

#: 審査状態の遷移。システムは記録するだけで、外部へは出さない。
REVIEW_NOT_REQUESTED = "not_requested"
REVIEW_SUBMITTED = "submitted"
REVIEW_APPROVED = "approved_for_manual_publish"
REVIEW_REJECTED = "rejected"
REVIEW_TRANSITIONS: dict[str, tuple[str, ...]] = {
    REVIEW_NOT_REQUESTED: (REVIEW_SUBMITTED,),
    REVIEW_SUBMITTED: (REVIEW_APPROVED, REVIEW_REJECTED, REVIEW_NOT_REQUESTED),
    REVIEW_APPROVED: (REVIEW_NOT_REQUESTED,),
    REVIEW_REJECTED: (REVIEW_NOT_REQUESTED,),
}

MANUAL_CHECKLIST: tuple[str, ...] = (
    "法務・広報・情報管理の社内確認（public_candidate=true の場合）",
    "会社 YouTube への手動アップロード（システム外）",
    "説明文とチャプターを video-metadata.json からコピー",
    "字幕 subtitles/narration.srt を手動で登録",
)


@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    path: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class DistributionDecision:
    classification: str
    public_candidate: bool
    public_review_status: str
    reasons: list[Reason] = field(default_factory=list)
    manual_publish_pack: list[str] = field(default_factory=list)
    missing_pack_files: list[str] = field(default_factory=list)
    code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "classification": self.classification,
            "public_candidate": self.public_candidate,
            # 「公開してよい」と読ませないための注記を成果物にも残す
            "public_candidate_meaning": "公開審査へ提出できる状態か（公開可否ではない）",
            "public_review_status": self.public_review_status,
            "reasons": [r.to_dict() for r in self.reasons],
            "manual_publish_pack": list(self.manual_publish_pack),
            "missing_pack_files": list(self.missing_pack_files),
            "manual_publish_checklist": list(MANUAL_CHECKLIST),
            "code": self.code,
        }


def build_manual_publish_pack(project_dir: Path) -> tuple[list[str], list[str]]:
    """手動投稿パックの実在確認（存在するもの / 欠けているもの）。

    `output.mp4` は音声つきがあればそちらを指す。
    """
    present: list[str] = []
    missing: list[str] = []
    for relative in MANUAL_PACK_FILES:
        candidate = project_dir / relative
        if relative == "output.mp4" and not candidate.is_file():
            with_audio = project_dir / "output-with-audio.mp4"
            if with_audio.is_file():
                present.append("output-with-audio.mp4")
                continue
        if candidate.is_file():
            present.append(relative)
        else:
            missing.append(relative)
    return present, missing


def evaluate_public_candidate(
    spec: dict[str, Any],
    *,
    qa: dict[str, Any] | None = None,
    citations: dict[str, Any] | None = None,
    requested: bool | None = None,
) -> tuple[bool, list[Reason]]:
    """公開候補かどうかを機械判定する。

    **既定は false で、判定は降下のみ。** 利用者が true を要求しても、
    機微ソース・秘密スキャンのヒット・ライセンス欠落のいずれかがあれば false。
    """
    reasons: list[Reason] = []
    dist = spec.get("distribution") or {}
    wanted = requested if requested is not None else bool(dist.get("public_candidate"))

    if not wanted:
        reasons.append(
            Reason("DEFAULT_INTERNAL", "distribution.public_candidate", "既定は社内限定です")
        )
        return False, reasons

    for index, source in enumerate(spec.get("sources") or []):
        if not isinstance(source, dict):
            continue
        sensitivity = source.get("sensitivity")
        if sensitivity in BLOCKING_SENSITIVITIES:
            reasons.append(
                Reason(
                    "SOURCE_SENSITIVITY",
                    f"sources[{index}]",
                    f"sensitivity={sensitivity} のソースを含みます",
                )
            )
    if dist.get("classification") == "confidential":
        reasons.append(
            Reason(
                "CLASSIFICATION",
                "distribution.classification",
                "classification=confidential では公開候補になれません",
            )
        )

    for check in (qa or {}).get("checks") or []:
        if check.get("id") == "secret_scan" and check.get("status") == "fail":
            reasons.append(Reason("SECRET_SCAN", "qa.secret_scan", check.get("detail", "")))
        if check.get("id") == "sfx_attribution" and check.get("status") == "fail":
            reasons.append(Reason("MISSING_LICENSE", "qa.sfx_attribution", check.get("detail", "")))

    for entry in (citations or {}).get("sound_attributions") or []:
        if not entry.get("license"):
            reasons.append(
                Reason(
                    "MISSING_LICENSE",
                    "citations.sound_attributions",
                    f"{entry.get('sound_id')} のライセンス記載がありません",
                )
            )

    if reasons:
        return False, reasons
    return True, [
        Reason(
            "ELIGIBLE_FOR_REVIEW",
            "distribution.public_candidate",
            "公開審査へ提出できます（公開可否は人が判断します）",
        )
    ]


def evaluate(
    spec: dict[str, Any],
    project_dir: Path,
    *,
    qa: dict[str, Any] | None = None,
    citations: dict[str, Any] | None = None,
    requested_public: bool | None = None,
) -> DistributionDecision:
    """配布判定一式（`distribution-report.json` の中身）。"""
    dist = spec.get("distribution") or {}
    public_candidate, reasons = evaluate_public_candidate(
        spec, qa=qa, citations=citations, requested=requested_public
    )
    present, missing = build_manual_publish_pack(project_dir)

    code = None
    if requested_public and not public_candidate:
        code = "PUBLIC_CANDIDATE_DENIED"
    elif missing:
        code = "MANUAL_PACK_INCOMPLETE"

    return DistributionDecision(
        classification=str(dist.get("classification") or "internal"),
        public_candidate=public_candidate,
        public_review_status=str(dist.get("public_review_status") or REVIEW_NOT_REQUESTED),
        reasons=reasons,
        manual_publish_pack=present,
        missing_pack_files=missing,
        code=code,
    )


def can_transition(current: str, target: str) -> bool:
    return target in REVIEW_TRANSITIONS.get(current, ())


def request_public_review(
    spec: dict[str, Any], project_dir: Path, *, qa: dict[str, Any] | None = None
) -> DistributionDecision:
    """審査提出へ状態を進める（**外部送信は一切しない**）。

    提出できるのは `public_candidate` が機械判定で true のときだけ。
    降下ルールをここで再実行するので、古い判定のまま提出されることがない。
    """
    decision = evaluate(spec, project_dir, qa=qa, requested_public=True)
    if not decision.public_candidate:
        return DistributionDecision(
            classification=decision.classification,
            public_candidate=False,
            public_review_status=decision.public_review_status,
            reasons=decision.reasons,
            manual_publish_pack=decision.manual_publish_pack,
            missing_pack_files=decision.missing_pack_files,
            code="PUBLIC_CANDIDATE_DENIED",
        )
    if not can_transition(decision.public_review_status, REVIEW_SUBMITTED):
        return DistributionDecision(
            classification=decision.classification,
            public_candidate=True,
            public_review_status=decision.public_review_status,
            reasons=[
                Reason(
                    "INVALID_TRANSITION",
                    "distribution.public_review_status",
                    f"{decision.public_review_status} から submitted へは進められません",
                )
            ],
            manual_publish_pack=decision.manual_publish_pack,
            missing_pack_files=decision.missing_pack_files,
            code="INVALID_TRANSITION",
        )
    return DistributionDecision(
        classification=decision.classification,
        public_candidate=True,
        public_review_status=REVIEW_SUBMITTED,
        reasons=decision.reasons,
        manual_publish_pack=decision.manual_publish_pack,
        missing_pack_files=decision.missing_pack_files,
    )


def write_report(decision: DistributionDecision, project_dir: Path) -> Path:
    path = project_dir / DISTRIBUTION_REPORT_FILE
    path.write_text(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_report(project_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((project_dir / DISTRIBUTION_REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_manual_publish_note(decision: DistributionDecision, project_dir: Path) -> Path:
    """`preview/MANUAL_PUBLISH.md` —— 人が手動投稿するときの手順書。"""
    target = project_dir / "preview" / "MANUAL_PUBLISH.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 手動アップロード手順",
        "",
        "**このシステムは動画を外部へ送信しません。** YouTube への登録は人が行います。",
        "",
        f"- 情報区分: `{decision.classification}`",
        f"- 公開審査へ提出できるか: `{decision.public_candidate}`"
        "（true でも「公開してよい」という意味ではありません）",
        f"- 審査状態: `{decision.public_review_status}`",
        "",
        "## 手元にあるファイル",
        "",
    ]
    lines += [f"- `{name}`" for name in decision.manual_publish_pack]
    if decision.missing_pack_files:
        lines += ["", "## 不足しているファイル", ""]
        lines += [f"- `{name}`" for name in decision.missing_pack_files]
    lines += ["", "## チェックリスト", ""]
    lines += [f"- [ ] {item}" for item in MANUAL_CHECKLIST]
    lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


__all__ = [
    "BLOCKING_SENSITIVITIES",
    "DISTRIBUTION_REPORT_FILE",
    "MANUAL_CHECKLIST",
    "MANUAL_PACK_FILES",
    "REVIEW_APPROVED",
    "REVIEW_NOT_REQUESTED",
    "REVIEW_REJECTED",
    "REVIEW_SUBMITTED",
    "DistributionDecision",
    "Reason",
    "build_manual_publish_pack",
    "can_transition",
    "evaluate",
    "evaluate_public_candidate",
    "load_report",
    "request_public_review",
    "write_manual_publish_note",
    "write_report",
]
