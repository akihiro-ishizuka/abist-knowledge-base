"""ナレーション尺と映像尺の同期。

**黙って切らない。** 音声が映像より長いときの手順は次の順で、
どれも成立しなければ**明示エラー**にする:

1. **分割** — シーンを複数に割って全部を流す（情報を失わない）
2. **atempo** — 許容範囲（0.9〜1.15倍）だけ話速を変える
3. **明示エラー** — `NARRATION_TOO_LONG` を返す

音声を無言で切り詰めると「言ったはずの内容が動画に無い」状態になり、
出典付きで作った意味が消えるので、その選択肢は用意しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 話速を変えてよい範囲（これを超えると聞き取りにくい）。
MIN_TEMPO = 0.9
MAX_TEMPO = 1.15
#: シーン尺の下限（短すぎると読めない）。
MIN_SCENE_SEC = 2.0
#: 映像の後ろに足す余白。
DEFAULT_PAD_SEC = 0.3


@dataclass(frozen=True, slots=True)
class SceneTiming:
    """1シーンの最終的な尺と、そこへ至った手段。"""

    scene_id: str
    video_sec: float
    narration_sec: float
    final_sec: float
    strategy: str  # pad | split | atempo | as_is
    tempo: float = 1.0
    split_into: int = 1
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SyncError:
    scene_id: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.scene_id, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class SyncResult:
    ok: bool
    timings: list[SceneTiming] = field(default_factory=list)
    total_sec: float = 0.0
    errors: list[SyncError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def plan_scene_timing(
    scene_id: str,
    *,
    video_sec: float,
    narration_sec: float,
    max_scene_sec: float | None = None,
    pad_sec: float = DEFAULT_PAD_SEC,
) -> tuple[SceneTiming | None, SyncError | None]:
    """1シーンの尺を決める。

    - ナレーションが映像以下 → 映像尺のまま（`as_is`）
    - 少し長い → 映像を伸ばす（`pad`）。Manim は最終フレームを保持できる
    - 上限を超える → 分割（`split`）、それでも無理なら話速調整（`atempo`）
    - どれも無理 → `NARRATION_TOO_LONG`
    """
    video_sec = max(video_sec, MIN_SCENE_SEC)
    if narration_sec <= 0:
        return SceneTiming(scene_id, video_sec, 0.0, video_sec, "as_is"), None

    needed = narration_sec + pad_sec
    if needed <= video_sec:
        return SceneTiming(scene_id, video_sec, narration_sec, video_sec, "as_is"), None

    if max_scene_sec is None or needed <= max_scene_sec:
        # 映像側を伸ばして受け止める（情報は一切削らない）
        return SceneTiming(scene_id, video_sec, narration_sec, needed, "pad"), None

    # 上限を超える: まず分割を試す
    parts = int(needed // max_scene_sec) + 1
    per_part = needed / parts
    if per_part <= max_scene_sec and parts <= 6:
        return (
            SceneTiming(
                scene_id,
                video_sec,
                narration_sec,
                needed,
                "split",
                split_into=parts,
                warnings=[f"{scene_id}: ナレーションが長いため {parts} 分割しました"],
            ),
            None,
        )

    # 次に話速調整（許容範囲だけ）
    required_tempo = needed / max_scene_sec
    if required_tempo <= MAX_TEMPO:
        return (
            SceneTiming(
                scene_id,
                video_sec,
                narration_sec,
                max_scene_sec,
                "atempo",
                tempo=round(required_tempo, 3),
                warnings=[f"{scene_id}: 話速を {required_tempo:.2f} 倍にしました"],
            ),
            None,
        )

    # 黙って切らない。明示的に失敗させる。
    return None, SyncError(
        scene_id,
        "NARRATION_TOO_LONG",
        f"ナレーション {narration_sec:.1f} 秒がシーン上限 {max_scene_sec:.1f} 秒に収まりません"
        f"（分割・話速調整 {MAX_TEMPO} 倍でも不足）。台本を短くするか上限を上げてください",
    )


def plan_timeline(
    scenes: list[dict[str, Any]],
    *,
    scene_durations: dict[str, float],
    narration_durations: dict[str, float],
    max_scene_sec: float | None = None,
    pad_sec: float = DEFAULT_PAD_SEC,
) -> SyncResult:
    """全シーンの尺を決め、通しのタイムラインを作る。"""
    timings: list[SceneTiming] = []
    errors: list[SyncError] = []
    warnings: list[str] = []

    for scene in scenes:
        scene_id = str(scene.get("id"))
        timing, error = plan_scene_timing(
            scene_id,
            video_sec=scene_durations.get(scene_id, MIN_SCENE_SEC),
            narration_sec=narration_durations.get(scene_id, 0.0),
            max_scene_sec=max_scene_sec,
            pad_sec=pad_sec,
        )
        if error is not None:
            errors.append(error)
            continue
        assert timing is not None
        timings.append(timing)
        warnings.extend(timing.warnings)

    if errors:
        return SyncResult(ok=False, timings=timings, errors=errors, warnings=warnings)
    return SyncResult(
        ok=True,
        timings=timings,
        total_sec=round(sum(t.final_sec for t in timings), 3),
        warnings=warnings,
    )


def scene_offsets(timings: list[SceneTiming]) -> dict[str, float]:
    """各シーンの開始時刻（通しタイムライン上）。効果音・字幕の配置に使う。"""
    offsets: dict[str, float] = {}
    cursor = 0.0
    for timing in timings:
        offsets[timing.scene_id] = round(cursor, 3)
        cursor += timing.final_sec
    return offsets


def atempo_filter(tempo: float) -> str | None:
    """ffmpeg の `atempo` フィルタ文字列（範囲外なら None）。"""
    if abs(tempo - 1.0) < 1e-6:
        return None
    if not (MIN_TEMPO <= tempo <= MAX_TEMPO):
        return None
    return f"atempo={tempo:.3f}"


__all__ = [
    "DEFAULT_PAD_SEC",
    "MAX_TEMPO",
    "MIN_SCENE_SEC",
    "MIN_TEMPO",
    "SceneTiming",
    "SyncError",
    "SyncResult",
    "atempo_filter",
    "plan_scene_timing",
    "plan_timeline",
    "scene_offsets",
]
