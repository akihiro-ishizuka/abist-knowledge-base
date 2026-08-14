"""利用量の集計（テロップ文字数・レンダリング時間・ディスク）。

かつては外部 TTS の従量課金を見積もるための集計だった。**ナレーション音声を
作らなくなったので、課金するものが無い**（文字数 x 単価という、請求の来ない
請求計算が残っていた）。

今も測る価値があるのは:

- **レンダリング時間** —— 描画は RENDER リースで直列化されるので、伸びると
  他のジョブまで待たされる
- **ディスク** —— 成果物は消さない方針なので、放っておくと溜まる
- **テロップ文字数** —— 課金ではなく**尺の目安**として（読速から尺が決まる）

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
    caption_chars: int
    scene_count: int
    duration_sec: float | None
    disk_bytes: int
    created_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "state": self.state,
            "caption_chars": self.caption_chars,
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

    @property
    def total_caption_chars(self) -> int:
        return sum(v.caption_chars for v in self.videos)

    @property
    def total_disk_bytes(self) -> int:
        return sum(v.disk_bytes for v in self.videos)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "generated_at": self.generated_at,
            "video_count": len(self.videos),
            "total_caption_chars": self.total_caption_chars,
            "total_disk_bytes": self.total_disk_bytes,
            "total_disk_mb": round(self.total_disk_bytes / 1_048_576, 1),
            "videos": [v.to_dict() for v in self.videos],
            "warnings": list(self.warnings),
        }


def caption_chars(spec: dict[str, Any]) -> int:
    """spec のテロップ文字数の合計（尺の目安。課金単位ではない）。"""
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
                caption_chars=caption_chars(spec),
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
    now: datetime | None = None,
) -> CostReport:
    """利用量レポートを組む。

    **金額は出さない。** 有料のプロバイダを使わない設計なので、見積もる対象が
    そもそも無い（かつては TTS の従量課金を見積もっていた）。
    """
    usages, warnings = collect_usage(reports_dir)
    stamp = (now or datetime.now(UTC)).isoformat()

    return CostReport(generated_at=stamp, videos=usages, warnings=warnings)


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
    "caption_chars",
    "write_report",
]
