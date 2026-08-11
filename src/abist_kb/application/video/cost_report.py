"""利用量の集計（TTS 文字数・レンダリング時間・ディスク）。

外部 TTS は従量課金なので、**先に「どれだけ喋らせたか」を数えられる**ように
しておく。金額は provider ごとの単価が要るため、既定では文字数と時間だけを出し、
単価が与えられたときだけ概算を添える（**推測の金額を既定で出さない**）。

アカウント識別子やトークンは集計に含めない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.application.video.gc import directory_size
from abist_kb.application.video.project_store import load_project, read_state
from abist_kb.infrastructure.video.artifact_store import videos_dir

REPORT_DIR = Path("reports") / "benchmarks" / "video"


@dataclass(frozen=True, slots=True)
class VideoUsage:
    video_id: str
    state: str
    narration_chars: int
    scene_count: int
    duration_sec: float | None
    disk_bytes: int
    created_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "state": self.state,
            "narration_chars": self.narration_chars,
            "scene_count": self.scene_count,
            "duration_sec": self.duration_sec,
            "disk_bytes": self.disk_bytes,
            "disk_mb": round(self.disk_bytes / 1_048_576, 1),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CostReport:
    generated_at: str
    videos: list[VideoUsage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 単価が与えられたときだけ入る概算（通貨は呼び出し側の指定に従う）。
    estimated_cost: dict[str, Any] | None = None

    @property
    def total_narration_chars(self) -> int:
        return sum(v.narration_chars for v in self.videos)

    @property
    def total_disk_bytes(self) -> int:
        return sum(v.disk_bytes for v in self.videos)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "generated_at": self.generated_at,
            "video_count": len(self.videos),
            "total_narration_chars": self.total_narration_chars,
            "total_disk_bytes": self.total_disk_bytes,
            "total_disk_mb": round(self.total_disk_bytes / 1_048_576, 1),
            "videos": [v.to_dict() for v in self.videos],
            "estimated_cost": self.estimated_cost,
            "warnings": list(self.warnings),
        }


def narration_chars(spec: dict[str, Any]) -> int:
    """spec のナレーション文字数の合計（TTS の課金単位）。"""
    total = 0
    for scene in spec.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        text = (scene.get("narration") or {}).get("text")
        if isinstance(text, str):
            total += len(text)
    return total


def collect_usage(reports_dir: Path) -> tuple[list[VideoUsage], list[str]]:
    """`reports/videos/*` から利用量を集める。"""
    root = videos_dir(reports_dir)
    warnings: list[str] = []
    usages: list[VideoUsage] = []
    if not root.is_dir():
        return usages, ["reports/videos がありません"]

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        spec = load_project(entry)
        if spec is None:
            warnings.append(f"{entry.name}: project-spec.json を読めません")
            continue
        state = read_state(entry) or {}
        usages.append(
            VideoUsage(
                video_id=str(spec.get("video_id") or entry.name),
                state=str(state.get("state") or "unknown"),
                narration_chars=narration_chars(spec),
                scene_count=len(spec.get("scenes") or []),
                duration_sec=state.get("duration_sec"),
                disk_bytes=directory_size(entry),
                created_at=state.get("created_at"),
            )
        )
    return usages, warnings


def build_report(
    reports_dir: Path,
    *,
    tts_unit_price_per_1k_chars: float | None = None,
    currency: str = "JPY",
    now: datetime | None = None,
) -> CostReport:
    """利用量レポートを組む。

    **単価が無ければ金額を出さない。** 推測の単価で概算を出すと、それが
    そのまま「予算」として一人歩きする。
    """
    usages, warnings = collect_usage(reports_dir)
    stamp = (now or datetime.now(UTC)).isoformat()

    estimated = None
    if tts_unit_price_per_1k_chars is not None:
        total_chars = sum(u.narration_chars for u in usages)
        estimated = {
            "basis": "narration_chars",
            "unit_price_per_1k_chars": tts_unit_price_per_1k_chars,
            "currency": currency,
            "amount": round(total_chars / 1000 * tts_unit_price_per_1k_chars, 2),
            "note": "実際の請求は provider の課金単位に従います（これは目安です）",
        }
    else:
        warnings.append("TTS の単価が未設定のため金額は算出していません（文字数のみ）")

    return CostReport(
        generated_at=stamp, videos=usages, warnings=warnings, estimated_cost=estimated
    )


def write_report(report: CostReport, repo_root: Path, *, stem: str | None = None) -> Path:
    """`reports/benchmarks/video/<timestamp>.json` へ保存する。"""
    target_dir = repo_root / REPORT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    name = stem or report.generated_at.replace(":", "").replace("-", "").replace(".", "")[:15]
    path = target_dir / f"{name}.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = [
    "REPORT_DIR",
    "CostReport",
    "VideoUsage",
    "build_report",
    "collect_usage",
    "narration_chars",
    "write_report",
]
