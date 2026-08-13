"""図に動きを付けるための共通語彙（Manim 依存）。

**不変条件: アニメ版の最終フレームは `build_final_layout(spec)` と一致する。**
静止画（PNG）経路は同じレイアウト関数をそのまま `add` するだけなので、ここで
減光やカメラ移動をしたら、`hold_to` の前に必ず元へ戻す。戻し忘れると
「動画の最後のコマ」と「サムネイル用の静止画」が食い違う。

演出をやるかどうかは `layout.choreo_budget`（manim 非依存・テスト済み）が決める。
このモジュールは決められたとおりに動かすだけで、判断は持たない。

決定論を壊さない: 乱数は使わず、run_time もすべて定数。同じ spec からは
同じ映像が出る（`resume` の内容ハッシュ再利用がこれに依存している）。
"""

from __future__ import annotations

from manim import (
    UP,
    AnimationGroup,
    FadeIn,
    Indicate,
    LaggedStart,
    Rectangle,
    ShowPassingFlash,
    config,
)

from templates.layout import choreo_budget, resolve_motion

#: 焦点から外れた要素の不透明度。消さずに沈める（文脈は残す）。
DIM_OPACITY = 0.35
#: 強調パルスの拡大率。
PULSE_SCALE = 1.06
#: シーン頭・尻のディップ（暗転）の既定秒数。
DIP_SEC = 0.35
#: 背景色（`render_scene._frame_config` が config へ入れる）。
_DEFAULT_BACKGROUND = "#000000"


def budget_for(spec: dict, *, beat_count: int, edge_count: int = 0) -> dict:
    """この spec で実行してよい演出を返す。"""
    motion = resolve_motion(
        spec.get("motion"),
        quality=str(spec.get("quality") or "standard"),
        beat_count=beat_count,
    )
    return choreo_budget(beat_count, motion, edge_count=edge_count)


class BeatClock:
    """beat が画面に出た**実時刻**を記録する。

    効果音の `beat-N.reveal` アンカーは、これまで「シーン尺を beat 数で等分」した
    推定値に載せていた。実測を返せば、音と絵がずれない。
    """

    def __init__(self, scene) -> None:
        self._scene = scene
        self.times: list[float] = []

    def mark(self) -> None:
        self.times.append(round(float(getattr(self._scene.renderer, "time", 0.0) or 0.0), 3))


def _opacity_animation(mobject, opacity: float):
    return mobject.animate.set_opacity(opacity)


def stagger_in(scene, items, *, shift=UP * 0.2, lag_ratio: float = 0.15, run_time: float = 0.9):
    """まとめて、少しずつずらして出す。

    1つずつ `play` すると呼び出し回数ぶんの固定コストが積み上がる。
    `LaggedStart` なら1回の再生で同じ「順に現れる」印象が出る。
    """
    items = [m for m in items if m is not None]
    if not items:
        return
    scene.play(
        LaggedStart(*[FadeIn(m, shift=shift) for m in items], lag_ratio=lag_ratio),
        run_time=run_time,
    )


def focus(scene, all_items, current, *, budget: dict) -> None:
    """今見てほしい要素以外を沈める。

    `budget["focus_run_time"] == 0` のときはアニメーションせず即座に切り替える
    （要素が多い図で毎回アニメーションすると、それだけで尺が倍になる）。
    """
    if not budget.get("focus"):
        return
    others = [m for m in all_items if m is not current and m is not None]
    if not others:
        return
    run_time = float(budget.get("focus_run_time") or 0.0)
    if run_time <= 0:
        for mobject in others:
            mobject.set_opacity(DIM_OPACITY)
        return
    scene.play(
        AnimationGroup(*[_opacity_animation(m, DIM_OPACITY) for m in others]),
        run_time=run_time,
    )


def unfocus_all(scene, all_items) -> None:
    """減光を戻す。**`hold_to` の前に必ず呼ぶ**（静止画と最終フレームを揃えるため）。"""
    for mobject in all_items:
        if mobject is not None:
            mobject.set_opacity(1.0)


def pulse(scene, mobject, *, run_time: float = 0.4) -> None:
    scene.play(Indicate(mobject, scale_factor=PULSE_SCALE), run_time=run_time)


def flow_pulse(scene, edge, *, color: str, budget: dict, run_time: float = 0.6) -> None:
    """エッジに沿って光を流す（データが流れる感じを出す）。

    `ShowPassingFlash` は元の Mobject を変えないので、最終フレームは変わらない。
    """
    if not budget.get("flow_pulse") or edge is None:
        return
    target = edge[0] if hasattr(edge, "__getitem__") and len(edge) > 0 else edge
    try:
        trace = target.copy().set_stroke(color=color, width=6)
    except Exception:  # noqa: BLE001 - 演出は本質ではない。描けなければ黙って飛ばす
        return
    scene.play(ShowPassingFlash(trace, time_width=0.3), run_time=run_time)


def _full_frame_rect(color: str) -> Rectangle:
    return Rectangle(
        width=config.frame_width + 1.0,
        height=config.frame_height + 1.0,
        stroke_width=0,
        fill_color=color,
        fill_opacity=1.0,
    )


def _dip_seconds(spec: dict) -> float:
    transition = spec.get("transition") or {}
    if transition.get("style") == "none":
        return 0.0
    try:
        return max(0.0, min(1.0, float(transition.get("duration_sec", DIP_SEC))))
    except (TypeError, ValueError):
        return DIP_SEC


def enter_scene(scene, spec: dict) -> None:
    """背景色から明ける。

    シーン間の繋ぎは **各シーン自身の頭と尻**でやる。ffmpeg の xfade で繋ぐと
    全シーンの開始時刻がずれ、効果音・字幕・チャプターのタイムラインが総崩れになる。
    """
    seconds = _dip_seconds(spec)
    if seconds <= 0:
        return
    cover = _full_frame_rect(str(config.background_color))
    scene.add(cover)
    scene.play(cover.animate.set_opacity(0.0), run_time=seconds)
    scene.remove(cover)


def exit_scene(scene, spec: dict) -> None:
    """背景色へ落とす。**`hold_to` の後**に呼ぶ（読ませてから消す）。"""
    seconds = _dip_seconds(spec)
    if seconds <= 0:
        return
    cover = _full_frame_rect(str(config.background_color))
    cover.set_opacity(0.0)
    scene.add(cover)
    scene.play(cover.animate.set_opacity(1.0), run_time=seconds)


def finish(scene, spec: dict, *, items=(), clock: BeatClock | None = None) -> None:
    """締めの定型: 減光を戻す → 尺を埋める → 暗転 → beat 実時刻を残す。

    テンプレートの `construct` はこれを最後に呼ぶだけでよい。**戻す前に
    尺を埋めない**（減光したまま止まった絵が静止画と食い違う）。
    """
    from templates.base import hold_to

    unfocus_all(scene, items)
    hold_to(scene, spec)
    exit_scene(scene, spec)
    if clock is not None:
        scene.kb_beat_times = clock.times


__all__ = [
    "DIM_OPACITY",
    "DIP_SEC",
    "PULSE_SCALE",
    "BeatClock",
    "budget_for",
    "enter_scene",
    "exit_scene",
    "finish",
    "flow_pulse",
    "focus",
    "pulse",
    "stagger_in",
    "unfocus_all",
]
