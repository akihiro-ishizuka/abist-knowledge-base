"""image_still: 静止画（アプリ画面キャプチャなど）の提示。

`image` beat の `path` は **scene-spec.json と同じディレクトリからの相対パス**。
絶対パスも `..` も SceneSpec の検証で弾かれるので、成果物ツリーの外は原理的に
参照できない（キャプチャ画像はレンダリング前にシーンディレクトリへ複製される）。

画像が無い／読めない場合も**シーンを失敗させない**。枠とキャプションだけの
プレースホルダに落とす（キャプチャ任意という Phase 7 の方針をここで担保する）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from manim import (
    DOWN,
    FadeIn,
    Group,
    ImageMobject,
    Rectangle,
    Restore,
    Scene,
    Text,
    VGroup,
    config,
    linear,
)

from templates import choreography
from templates.base import (
    COLOR_ACCENT,
    COLOR_BODY,
    COLOR_FOOTER,
    COLOR_RULE,
    card_title,
    content_width,
    resolve_font,
    source_footer,
    wrapped_text,
)

#: 画像に割り当てる高さの割合（フレーム高さに対して）。
IMAGE_HEIGHT_RATIO = 0.52
#: 画像が用意できなかったときの表示文言。
PLACEHOLDER_TEXT = "画面キャプチャは未取得です"


def resolve_asset(spec: dict, relative: str) -> Path | None:
    """`asset_base` からの相対パスを解決する（実在しなければ None）。"""
    base = spec.get("asset_base")
    if not base:
        return None
    candidate = Path(base) / relative
    return candidate if candidate.is_file() else None


def _image_or_placeholder(spec: dict, beat: dict, max_height: float):
    resolved = resolve_asset(spec, beat.get("path", ""))
    max_width = content_width()
    if resolved is not None:
        image = ImageMobject(str(resolved))
        image.height = max_height
        if image.width > max_width:
            image.width = max_width
        return image
    font = resolve_font(spec)
    frame = Rectangle(
        width=max_width * 0.8,
        height=max_height,
        color=COLOR_RULE,
        stroke_width=2,
        fill_opacity=0.05,
    )
    label = Text(PLACEHOLDER_TEXT, font=font, font_size=22, color=COLOR_FOOTER)
    label.move_to(frame.get_center())
    return VGroup(frame, label)


def build_final_layout(spec: dict) -> Group:
    font = resolve_font(spec)
    images = [b for b in spec["beats"] if b["type"] == "image"]
    max_height = config.frame_height * IMAGE_HEIGHT_RATIO / max(1, len(images))

    layout = Group(card_title(spec["title"], font, font_size=36))
    for beat in images:
        layout.add(_image_or_placeholder(spec, beat, max_height))
        if beat.get("caption"):
            layout.add(wrapped_text(beat["caption"], font, 22, COLOR_BODY, content_width()))
        credit = beat.get("license") or beat.get("attribution")
        if credit:
            layout.add(Text(credit, font=font, font_size=16, color=COLOR_ACCENT))
    layout.add(source_footer(spec, font))
    layout.arrange(DOWN, buff=0.3)
    if layout.height > config.frame_height - 0.8:
        layout.scale((config.frame_height - 0.8) / layout.height)
    return layout


#: Ken Burns の寄り幅。持ち込んだ図をゆっくり見せるための微速ズーム。
KEN_BURNS_SCALE = 1.06
#: 寄る速さ（秒）。1枚を読ませる時間に合わせる。
KEN_BURNS_SEC = 4.0


def _ken_burns(scene, target, spec: dict) -> None:
    """静止画をゆっくり寄る／引く（決定論的に向きを決める）。

    静止画をそのまま何秒も映すと、動画の中でそこだけ時間が止まって見える。
    **向きは scene_kind と title のハッシュで決める**（乱数を使うと同じ spec から
    同じ映像が出なくなり、成果物ハッシュでの再利用判定が壊れる）。
    """
    budget = choreography.budget_for(spec, beat_count=1)
    if not budget.get("focus"):  # minimal のときは動かさない
        return
    digest = hashlib.sha256(str(spec.get("title", "")).encode("utf-8")).digest()
    zoom_in = digest[0] % 2 == 0
    factor = KEN_BURNS_SCALE if zoom_in else 1.0 / KEN_BURNS_SCALE
    target.save_state()
    scene.play(target.animate.scale(factor), run_time=KEN_BURNS_SEC, rate_func=linear)
    # **元へ戻す。** 最終フレームは `build_final_layout` と一致していなければならない。
    scene.play(Restore(target), run_time=0.4)


def make_scene_classes(spec: dict):
    class ImageStillAnim(Scene):
        def construct(self):
            layout = build_final_layout(spec)
            choreography.enter_scene(self, spec)
            for part in layout:
                self.play(FadeIn(part), run_time=0.55)
                self.wait(0.5)
            # 画像そのものにゆっくり寄る（1枚だけの静止画面を動かす）。
            images = [m for m in layout if isinstance(m, ImageMobject)]
            if images:
                _ken_burns(self, images[0], spec)
            self.wait(1.6)
            choreography.finish(self, spec)

    class ImageStillStatic(Scene):
        def construct(self):
            self.add(build_final_layout(spec))

    return ImageStillAnim, ImageStillStatic
