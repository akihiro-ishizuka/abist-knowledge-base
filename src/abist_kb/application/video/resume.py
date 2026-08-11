"""再開と部分再生成。

**やり直す範囲を最小にする。** シーン描画は1本あたり数十秒かかるので、
1シーンだけ直したいときに全部を描き直すのは現実的でない。

再開の判断は `state.json` の段階ポインタと、シーンごとの
`scenes/<id>/scene-spec.json` の内容ハッシュで行う:

- spec が変わっていないシーン → 既存の `output.mp4` を再利用
- spec が変わった／成果物が無いシーン → そのシーンだけ描き直す
- どのシーンが変わっても**結合・音声・字幕・QA はやり直す**（尺が変わるため）

`state.json` が無い旧プロジェクトはフル再実行に落とす。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.application.video.project_store import load_project, read_state
from abist_kb.infrastructure.video.artifact_store import scene_dir, sha256_text

#: 段階の並び（`state.json` の `phase`）。前から順に進む。
PHASES: tuple[str, ...] = (
    "planned",
    "rendered",
    "narrated",
    "subtitled",
    "composed",
    "qa",
    "done",
)


@dataclass(frozen=True, slots=True)
class SceneReuse:
    scene_id: str
    reusable: bool
    video: Path | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """どこから再開するか。"""

    ok: bool
    full_rerun: bool = False
    scenes: list[SceneReuse] = field(default_factory=list)
    reason: str = ""

    @property
    def reusable_ids(self) -> list[str]:
        return [s.scene_id for s in self.scenes if s.reusable]

    @property
    def rerender_ids(self) -> list[str]:
        return [s.scene_id for s in self.scenes if not s.reusable]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "full_rerun": self.full_rerun,
            "reason": self.reason,
            "reusable": self.reusable_ids,
            "rerender": self.rerender_ids,
        }


def scene_spec_digest(scene_spec: dict[str, Any]) -> str:
    """SceneSpec の内容ハッシュ（キー順を固定して安定させる）。

    `min_duration_sec` も含める —— 尺が変われば映像も変わるため、
    「同じ内容だから使い回せる」とは言えない。
    """
    return sha256_text(json.dumps(scene_spec, ensure_ascii=False, sort_keys=True))


def plan_resume(project_dir: Path, *, force_scene_ids: set[str] | None = None) -> ResumePlan:
    """既存の成果物のうち、どれを再利用できるかを判定する。"""
    spec = load_project(project_dir)
    if spec is None:
        return ResumePlan(ok=False, reason="project-spec.json がありません")
    if read_state(project_dir) is None:
        return ResumePlan(
            ok=True,
            full_rerun=True,
            reason="state.json が無い旧プロジェクトのためフル再実行します",
        )

    forced = force_scene_ids or set()
    results: list[SceneReuse] = []
    for scene in spec.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        scene_id = str(scene.get("id"))
        target = scene_dir(project_dir, scene_id)
        video = target / "output.mp4"
        recorded = target / "resume-digest.txt"

        if scene_id in forced:
            results.append(SceneReuse(scene_id, False, reason="明示的な再生成指定"))
            continue
        if not video.is_file():
            results.append(SceneReuse(scene_id, False, reason="シーン動画がありません"))
            continue
        if not recorded.is_file():
            results.append(SceneReuse(scene_id, False, reason="内容ハッシュの記録がありません"))
            continue
        want = scene_spec_digest(scene.get("scene_spec") or {})
        if recorded.read_text(encoding="utf-8").strip() != want:
            results.append(SceneReuse(scene_id, False, reason="SceneSpec が変わっています"))
            continue
        results.append(SceneReuse(scene_id, True, video=video, reason="内容が同じため再利用"))

    return ResumePlan(ok=True, scenes=results)


def record_scene_digest(project_dir: Path, scene_id: str, scene_spec: dict[str, Any]) -> Path:
    """描画したシーンの内容ハッシュを記録する（次回の再開判定に使う）。"""
    target = scene_dir(project_dir, scene_id)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "resume-digest.txt"
    path.write_text(scene_spec_digest(scene_spec), encoding="utf-8")
    return path


def savings(plan: ResumePlan) -> dict[str, Any]:
    """再開でどれだけ描画を省けるか（ベンチと報告に使う）。"""
    total = len(plan.scenes)
    reused = len(plan.reusable_ids)
    return {
        "scene_total": total,
        "scene_reused": reused,
        "scene_rerendered": total - reused,
        "reuse_ratio": round(reused / total, 3) if total else 0.0,
    }


__all__ = [
    "PHASES",
    "ResumePlan",
    "SceneReuse",
    "plan_resume",
    "record_scene_digest",
    "savings",
    "scene_spec_digest",
]
