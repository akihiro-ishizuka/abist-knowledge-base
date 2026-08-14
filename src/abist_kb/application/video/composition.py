"""構成の単調さを測る（純関数）。

QA が見ているのは技術的な正しさ（解像度・fps・焼き込み・出典・秘密）だけで、
**動画として単調かどうかを誰も見ていなかった**。同じ見た目のカードが延々続いても、
検査は全部 pass する。実際、手書きの高品質台本でさえ 14シーン中 10 が key_points で、
同じ種別が 6 連続していた。

判定は `scenes[]` だけで完結させる。そうすると**描く前**（`validate_video_script`）と
**描いた後**（`qa`）の両方から同じ関数を呼べる。描いてから気付くのでは遅い
（1本描くのに数分かかる）ので、本命は描く前のほう。

**既定は warn。** 短い動画やチェックリスト形式では単調さが正解のこともあり、
機械が断定できる種類の判断ではない。役目は断定ではなく、**具体的な数字を出して
人間の目を誘導する**こと。書き手が `story_requirements` で明示的に約束した場合だけ、
QA がそれを fail で執行する（`content_quality.validate_story_content` と同じ流儀）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 同じ種別を続けてよい上限。これを超えると「同じ絵が続く」と感じ始める。
MAX_SAME_KIND_RUN = 4
#: 種別の数の下限（`MIN_SCENES_FOR_VARIETY` 以上のシーン数のときだけ見る）。
MIN_DISTINCT_KINDS = 3
#: 種別の多様性を要求し始めるシーン数。短い動画で種別が少ないのは妥当。
MIN_SCENES_FOR_VARIETY = 6
#: 図・グラフを伴わない「カードだけ」のシーンが占めてよい割合の上限。
CARD_RATIO_LIMIT = 0.75

#: 図・グラフとして情報を見せる scene_kind。これ以外はカード（文字を並べる面）。
#: `title` / `chapter` / `ending` のような節目の面もカードに数える —— 節目が多いのは
#: 構成の問題ではないが、**それしか無い**なら単調であることに変わりはない。
DIAGRAM_KINDS: frozenset[str] = frozenset(
    {"flow", "timeline", "comparison", "domain", "explain", "chart", "image", "code", "formula"}
)


@dataclass(frozen=True, slots=True)
class CompositionReport:
    """構成の測定結果。数字と、そこから出た指摘。"""

    scene_count: int
    distinct_kinds: int
    longest_same_run: int
    card_ratio: float
    #: `{code, message, hint, declared}`。`declared=True` は書き手が約束した分
    #: （QA はこれだけを fail にする）。
    findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """書き手が約束した条件を満たしているか（既定の指摘は ok を崩さない）。"""
        return not any(f["declared"] for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sceneCount": self.scene_count,
            "distinctKinds": self.distinct_kinds,
            "longestSameRun": self.longest_same_run,
            "cardRatio": self.card_ratio,
            "findings": list(self.findings),
        }


def _longest_run(kinds: list[str]) -> int:
    longest = current = 0
    previous: str | None = None
    for kind in kinds:
        current = current + 1 if kind == previous else 1
        previous = kind
        longest = max(longest, current)
    return longest


def _finding(code: str, message: str, hint: str, *, declared: bool) -> dict[str, Any]:
    return {"code": code, "message": message, "hint": hint, "declared": declared}


def score_composition(
    scenes: list[dict[str, Any]], *, requirements: dict[str, Any] | None = None
) -> CompositionReport:
    """シーンの並びから単調さを測り、指摘を返す。

    `requirements` に `min_distinct_scene_kinds` / `max_same_kind_run` があれば、
    その分は `declared=True` として返す（QA が fail にする対象）。
    """
    kinds = [str(scene.get("kind") or "") for scene in scenes]
    scene_count = len(kinds)
    if scene_count == 0:
        return CompositionReport(0, 0, 0, 0.0, [])

    distinct = len({k for k in kinds if k})
    longest = _longest_run(kinds)
    cards = sum(1 for k in kinds if k not in DIAGRAM_KINDS)
    card_ratio = round(cards / scene_count, 3)

    declared = requirements or {}
    declared_run = declared.get("max_same_kind_run")
    declared_kinds = declared.get("min_distinct_scene_kinds")
    findings: list[dict[str, Any]] = []

    run_limit = declared_run if isinstance(declared_run, int) else MAX_SAME_KIND_RUN
    if longest > run_limit:
        findings.append(
            _finding(
                "MONOTONOUS_RUN",
                f"同じ種別のシーンが {longest} 連続しています（上限 {run_limit}）",
                "間に別の見せ方を挟んでください。数値なら chart、経緯なら timeline、"
                "手順なら flow が使えます",
                declared=isinstance(declared_run, int),
            )
        )

    kind_floor = declared_kinds if isinstance(declared_kinds, int) else MIN_DISTINCT_KINDS
    watch_variety = isinstance(declared_kinds, int) or scene_count >= MIN_SCENES_FOR_VARIETY
    if watch_variety and distinct < kind_floor:
        findings.append(
            _finding(
                "TOO_FEW_SCENE_KINDS",
                f"{scene_count} シーンに対し種別が {distinct} 種類しかありません"
                f"（下限 {kind_floor}）",
                "内容に合った種別を選び直してください（list_scene_kinds の preview で"
                "見た目を確認できます）",
                declared=isinstance(declared_kinds, int),
            )
        )

    if card_ratio > CARD_RATIO_LIMIT:
        findings.append(
            _finding(
                "SLIDESHOW_RISK",
                f"図やグラフを伴わないシーンが {card_ratio:.0%} を占めています"
                f"（上限 {CARD_RATIO_LIMIT:.0%}）",
                "文字を並べるだけの面が続くと、動画である意味が薄れます。"
                "図で見せられる内容を探してください",
                declared=False,
            )
        )

    return CompositionReport(scene_count, distinct, longest, card_ratio, findings)


__all__ = [
    "CARD_RATIO_LIMIT",
    "DIAGRAM_KINDS",
    "MAX_SAME_KIND_RUN",
    "MIN_DISTINCT_KINDS",
    "MIN_SCENES_FOR_VARIETY",
    "CompositionReport",
    "score_composition",
]
