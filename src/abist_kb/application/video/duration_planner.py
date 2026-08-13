"""`target_duration_sec` から動画の構成を逆算する。

**尺を伸ばすために入力文書を機械的に増やさない。** 目標尺から

    章数 → シーン数 → 1シーンの目標尺 → ナレーション目標文字数

を決め、**関連情報の範囲内で**その構成を満たせるかを判定する。満たせないなら
`INSUFFICIENT_CONTENT_FOR_DURATION` を返して止まる —— 水増しした説明で尺を
埋めるのは、出典必須ポリシー（KB に無いことを描かない）と正面から衝突する。

文字量の見積もりは `caption_timing.READING_CHARS_PER_SECOND` を**唯一の正本**として
参照する。ナレーション音声は無く、台本の文はテロップとして画面に出るので、基準は
「喋る速さ」ではなく「読める速さ」。読み上げ速度で割り当てると、視聴者が読み切れない
量のテロップを載せた構成が「目標尺どおり」として通ってしまう。

純関数のみ（ファイルにも DB にも触れない）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from abist_kb.application.video.caption_timing import (
    READING_CHARS_PER_SECOND as CHARS_PER_SECOND,
)

#: 役割ごとの尺の配分。合計 1.0。
#:
#: 導入と締めは尺を食わせない（情報密度が低いため）。本編（説明・図解・比較）へ
#: 8 割を割く。この比率が「章立てされた社内説明動画」の骨格そのもの。
ROLE_SHARE: dict[str, float] = {
    "intro": 0.06,
    "explain": 0.44,
    "diagram": 0.26,
    "comparison": 0.12,
    "summary": 0.12,
}

#: 1シーンの目標尺の下限・上限（秒）。
#: 下限より短いと字幕が読めず、上限より長いと1画面を見続けることになる。
MIN_SCENE_SEC = 8.0
MAX_SCENE_SEC = 22.0
#: 章あたりのシーン数の目安（章扉 + 本編）。
SCENES_PER_CHAPTER = 3
#: 章数の下限・上限。
MIN_CHAPTERS = 2
MAX_CHAPTERS = 12

#: ナレーションが載る割合（間・図の見せ場を差し引く）。
#: 1.0 にすると喋りっぱなしになり、`audio_sync` が毎シーンで pad/atempo を打つ。
NARRATION_FILL_RATIO = 0.82

#: 表紙・章扉・エンドカードの固定尺（秒）。
#: これらは「タイトルを見せる」だけの面で、伸ばしても情報が増えない。
CARD_TARGET_SEC = 8.0
#: 役割 -> どちらの尺を使うか。カード面は固定尺、本編は残りを配分した尺。
CARD_ROLES: frozenset[str] = frozenset({"intro", "chapter", "ending", "cta", "title"})

#: 1シーンあたりに必要な「素材」の最小数（statement / flow_step / 引用など）。
#: これを下回る素材しか無いのに尺だけ長い、が水増しの入口。
MIN_MATERIAL_PER_SCENE = 2


@dataclass(frozen=True, slots=True)
class DurationPlan:
    """目標尺から逆算した構成。"""

    ok: bool
    target_sec: float = 0.0
    min_sec: float = 0.0
    max_sec: float = 0.0
    chapter_count: int = 0
    scene_count: int = 0
    scene_target_sec: float = 0.0
    #: 表紙・章扉・エンドカードの固定尺。
    card_target_sec: float = 0.0
    #: 本編1シーンの目標尺（カードの余りを配分したもの）。
    body_target_sec: float = 0.0
    narration_chars_total: int = 0
    narration_chars_per_scene: int = 0
    #: 本編1シーンのナレーション目標文字数（`body_target_sec` から逆算）。
    narration_chars_per_body_scene: int = 0
    #: 役割ごとのシーン数（`intro` / `explain` / `diagram` / `comparison` / `summary`）。
    scenes_by_role: dict[str, int] = field(default_factory=dict)
    code: str | None = None
    message: str | None = None
    warnings: list[str] = field(default_factory=list)

    def target_for_kind(self, scene_kind: str) -> float:
        """シーン種別ごとの目標尺（カード面は固定尺、本編は配分された尺）。"""
        if scene_kind in CARD_ROLES:
            return self.card_target_sec or self.scene_target_sec
        return self.body_target_sec or self.scene_target_sec

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "target_sec": round(self.target_sec, 1),
            "min_sec": round(self.min_sec, 1),
            "max_sec": round(self.max_sec, 1),
            "chapter_count": self.chapter_count,
            "scene_count": self.scene_count,
            "scene_target_sec": round(self.scene_target_sec, 1),
            "card_target_sec": round(self.card_target_sec, 1),
            "body_target_sec": round(self.body_target_sec, 1),
            "narration_chars_total": self.narration_chars_total,
            "narration_chars_per_scene": self.narration_chars_per_scene,
            "narration_chars_per_body_scene": self.narration_chars_per_body_scene,
            "scenes_by_role": dict(self.scenes_by_role),
            "code": self.code,
            "message": self.message,
            "warnings": list(self.warnings),
        }


def narration_chars_for(seconds: float) -> int:
    """尺（秒）に対するテロップの目標文字数。

    `READING_CHARS_PER_SECOND`（= 4.5 文字/秒 ≒ 270 文字/分の黙読）を正本とし、
    間・図の見せ場の分だけ `NARRATION_FILL_RATIO` で割り引く。
    """
    return max(0, int(seconds * NARRATION_FILL_RATIO * CHARS_PER_SECOND))


def seconds_for_chars(chars: int) -> float:
    """文字数から推定尺（秒）。`narration_chars_for` の逆。"""
    if chars <= 0:
        return 0.0
    return chars / CHARS_PER_SECOND / NARRATION_FILL_RATIO


def _distribute(scene_count: int, chapter_count: int) -> dict[str, int]:
    """役割ごとのシーン数を決める（合計が `scene_count` に一致する）。

    固定枠は表紙 1 / まとめ 1 / エンドカード 1 / 章扉 `chapter_count`。
    残りを本編（説明・図解・比較）へ最大剰余法で配る。**章扉も1シーンとして数える**
    —— 数えないと実尺が逆算より必ず伸びる。
    """
    fixed = 3 + chapter_count  # intro + summary + ending + 章扉
    if scene_count <= fixed:
        return {
            "intro": 1,
            "summary": 1,
            "ending": 1,
            "chapter": max(0, scene_count - 3),
            "explain": 0,
            "diagram": 0,
            "comparison": 0,
        }

    body_roles = ("explain", "diagram", "comparison")
    assigned = {"intro": 1, "summary": 1, "ending": 1, "chapter": chapter_count}
    body_total = scene_count - fixed
    body_share_sum = sum(ROLE_SHARE[r] for r in body_roles)

    exact = {r: body_total * ROLE_SHARE[r] / body_share_sum for r in body_roles}
    floors = {r: int(exact[r]) for r in body_roles}
    remainder = body_total - sum(floors.values())
    # 端数の大きい順に 1 ずつ配る（同値のときは ROLE_SHARE の順で安定させる）
    order = sorted(body_roles, key=lambda r: (-(exact[r] - floors[r]), body_roles.index(r)))
    for role in order[:remainder]:
        floors[role] += 1
    assigned.update(floors)
    # explain は必ず1つ以上（本編が図と比較だけになるのを避ける）
    if assigned["explain"] == 0:
        for role in ("comparison", "diagram"):
            if assigned[role] > 0:
                assigned[role] -= 1
                assigned["explain"] = 1
                break
    return assigned


def plan_duration(
    target: dict[str, float] | None,
    *,
    available_materials: int | None = None,
) -> DurationPlan:
    """目標尺から構成を逆算する。

    `available_materials` は「関連情報から取り出せた素材の数」
    （statement / flow_step / timeline_point など、1シーンの中身になる単位）。
    構成に必要な数へ届かない場合は `INSUFFICIENT_CONTENT_FOR_DURATION` を返す。
    **足りないぶんを水増しして尺を埋めることはしない。**
    """
    warnings: list[str] = []
    spec = target or {}
    try:
        min_sec = float(spec.get("min", 120))
        max_sec = float(spec.get("max", 600))
    except (TypeError, ValueError):
        return DurationPlan(
            ok=False, code="INVALID_VIDEO_SPEC", message="target_duration_sec が数値ではありません"
        )
    if min_sec <= 0 or max_sec < min_sec:
        return DurationPlan(
            ok=False,
            code="INVALID_VIDEO_SPEC",
            message="target_duration_sec は 0 < min <= max である必要があります",
        )

    # 目標は下限寄り（min の 1.1 倍、ただし max を超えない）に置く。
    # 中央に置くと 5〜10 分レンジで常に 7 分超を狙うことになり、素材が尽きる。
    target_sec = min(max_sec, max(min_sec, min_sec * 1.1))

    scene_count = max(3, round(target_sec / ((MIN_SCENE_SEC + MAX_SCENE_SEC) / 2)))
    # 1シーンの尺が上下限に収まるようシーン数を詰める
    while scene_count > 3 and target_sec / scene_count < MIN_SCENE_SEC:
        scene_count -= 1
    while target_sec / scene_count > MAX_SCENE_SEC:
        scene_count += 1

    scene_target = target_sec / scene_count
    # 章扉・表紙・まとめ・エンドカードを除いた本編シーン数から章数を決める。
    body_estimate = max(1, scene_count - 3)
    chapter_count = max(
        MIN_CHAPTERS,
        min(MAX_CHAPTERS, body_estimate // (SCENES_PER_CHAPTER + 1) or MIN_CHAPTERS),
    )
    # 章扉のぶん本編が消えないように、章数は本編シーン数を超えない
    chapter_count = min(chapter_count, max(1, (scene_count - 3) // 2))

    scenes_by_role = _distribute(scene_count, chapter_count)
    narration_total = narration_chars_for(target_sec)

    # 素材が要るのは本編シーンだけ（表紙・章扉・エンドカードは素材を消費しない）
    body_scenes = sum(
        scenes_by_role.get(role, 0) for role in ("explain", "diagram", "comparison", "summary")
    )
    needed = max(1, body_scenes) * MIN_MATERIAL_PER_SCENE
    if available_materials is not None and available_materials < needed:
        reachable = seconds_for_chars(
            narration_chars_for((available_materials / MIN_MATERIAL_PER_SCENE) * scene_target)
        )
        return DurationPlan(
            ok=False,
            target_sec=target_sec,
            min_sec=min_sec,
            max_sec=max_sec,
            code="INSUFFICIENT_CONTENT_FOR_DURATION",
            message=(
                f"目標尺 {target_sec:.0f} 秒には {scene_count} シーン"
                f"（素材 {needed} 件）が必要ですが、関連情報から取り出せたのは "
                f"{available_materials} 件です。到達可能なのは概ね {reachable:.0f} 秒までです。"
                "尺を短くするか、関連する文書を追加してください"
                "（説明を水増しして尺を埋めることはしません）"
            ),
        )

    if available_materials is not None and available_materials < needed * 1.5:
        warnings.append(
            f"素材が {available_materials} 件で目安（{int(needed * 1.5)} 件）を下回るため、"
            "1シーンあたりの情報量が薄くなる可能性があります"
        )

    # 表紙・章扉・エンドカードは内容が決まっていて長くできないので固定尺にし、
    # **余った尺は本編へ回す。** 全シーンを一律 `scene_target` にすると、
    # 実際には短く終わるカードのぶんだけ完成尺が目標を割る（実測で 132 秒目標に
    # 対し 103 秒になった）。
    card_scenes = 1 + chapter_count + 1  # 表紙 + 章扉 + エンドカード
    body_target = (target_sec - CARD_TARGET_SEC * card_scenes) / max(1, body_scenes)
    body_target = max(MIN_SCENE_SEC, min(MAX_SCENE_SEC, body_target))

    return DurationPlan(
        ok=True,
        target_sec=target_sec,
        min_sec=min_sec,
        max_sec=max_sec,
        chapter_count=chapter_count,
        scene_count=scene_count,
        scene_target_sec=scene_target,
        card_target_sec=CARD_TARGET_SEC,
        body_target_sec=body_target,
        narration_chars_total=narration_total,
        narration_chars_per_scene=max(1, narration_total // scene_count),
        narration_chars_per_body_scene=narration_chars_for(body_target),
        scenes_by_role=scenes_by_role,
        warnings=warnings,
    )


__all__ = [
    "CARD_ROLES",
    "CARD_TARGET_SEC",
    "MAX_CHAPTERS",
    "MAX_SCENE_SEC",
    "MIN_CHAPTERS",
    "MIN_MATERIAL_PER_SCENE",
    "MIN_SCENE_SEC",
    "NARRATION_FILL_RATIO",
    "ROLE_SHARE",
    "SCENES_PER_CHAPTER",
    "DurationPlan",
    "narration_chars_for",
    "plan_duration",
    "seconds_for_chars",
]
