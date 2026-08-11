"""目標尺から逆算した**章立て台本**を組む（LLM 非依存・決定的）。

`duration_planner` が決めた「章数 / シーン数 / 1シーンの目標尺 / ナレーション
目標文字数 / 役割の配分」に、関連文書から取り出した素材を**そのまま**流し込む。

**水増しをしない**ことがこのモジュールの設計制約:

- 素材が足りなければシーンを作らない（同じ文を言い換えて増やさない）
- ナレーションは素材の文から組み立て、目標文字数に**足りなくてもそのまま**返す
  （`duration_planner` が事前に `INSUFFICIENT_CONTENT_FOR_DURATION` で止める）
- 出典行範囲は素材が実際にあった行を指す（章単位でまとめて広い範囲にしない）

出力は `script_draft.validate_script_draft` が受け取る形。LLM 版と同形なので
後段（`build_scenes`）は共通のまま。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from abist_kb.application.video.duration_planner import DurationPlan

if TYPE_CHECKING:  # 実行時に import すると script_planner と循環する
    from abist_kb.application.video.script_planner import DocumentOutline

#: 1シーンに載せる要点の上限（読める密度）。
MAX_POINTS_PER_SCENE = 4
#: flow シーンに載せるステップ数の範囲。
MIN_FLOW_STEPS = 2
MAX_FLOW_STEPS = 6
#: 画面表示1行の上限（`script_draft._ONSCREEN_MAX` と揃える）。
ONSCREEN_MAX = 60
#: 引用の上限（`scene_spec.MAX_QUOTE_CHARS` と揃える）。
QUOTE_MAX = 240
#: ナレーション1シーンの上限（`script_draft._NARRATION_MAX` と揃える）。
NARRATION_MAX = 400
#: 表紙・章扉・エンドカードのナレーション文字数の目安。
#: これらは内容が決まっていて長くできないので、予算は本編へ回す。
FIXED_CARD_NARRATION_CHARS = 40


@dataclass(frozen=True, slots=True)
class Material:
    """1件の素材（シーンの中身になる最小単位）。必ず出典行を持つ。"""

    path: str
    text: str
    line: int
    #: 見出し由来なら True（章の切れ目に使える）。
    is_heading: bool = False
    #: 見出しの深さ（1 が最上位）。
    level: int = 0


def collect_materials(outline: DocumentOutline) -> list[Material]:
    """Markdown の骨格から素材を取り出す（行番号つき）。

    見出しと箇条書きだけを拾うのは、本文段落から機械的に文を切り出すと
    「文脈を失った断片」が並んで、出典はあるのに意味が通らない図になるため。
    """
    materials: list[Material] = []
    for level, text, line in outline.headings[1:]:
        cleaned = text.strip()
        if 1 <= len(cleaned) <= ONSCREEN_MAX:
            materials.append(Material(outline.path, cleaned, line, is_heading=True, level=level))
    for text, line in outline.bullets:
        cleaned = text.strip()
        if 1 <= len(cleaned) <= ONSCREEN_MAX:
            materials.append(Material(outline.path, cleaned, line))
    materials.sort(key=lambda m: m.line)
    return materials


def count_materials(outlines: list[DocumentOutline]) -> int:
    """全文書から取り出せる素材の総数（`plan_duration` の入力）。"""
    return sum(len(collect_materials(o)) for o in outlines)


def _chunks(items: list[Material], count: int) -> list[list[Material]]:
    """素材を `count` 個の塊へ均等に割る（余りは前から1つずつ）。"""
    if count <= 0 or not items:
        return []
    size, remainder = divmod(len(items), count)
    chunks: list[list[Material]] = []
    start = 0
    for index in range(count):
        take = size + (1 if index < remainder else 0)
        if take == 0:
            continue
        chunks.append(items[start : start + take])
        start += take
    return chunks


def _line_range(items: list[Material], outline_by_path: dict[str, DocumentOutline]):
    """素材の並びから出典の行範囲を作る（実際にあった行だけを指す）。"""
    path = items[0].path
    outline = outline_by_path.get(path)
    total = outline.total_lines if outline else items[-1].line
    start = max(1, min(m.line for m in items))
    end = min(total, max(m.line for m in items) + 2)
    return path, start, max(start, end)


def _narration(sentences: list[str], budget: int) -> str:
    """目標文字数に収まる範囲でナレーションを組む（**足りなくても水増ししない**）。"""
    limit = min(budget, NARRATION_MAX)
    text = ""
    for sentence in sentences:
        candidate = sentence if sentence.endswith("。") else f"{sentence}。"
        if len(text) + len(candidate) > limit:
            break
        text += candidate
    return text or (sentences[0][:limit] if sentences else "")


def _looks_like_steps(items: list[Material]) -> bool:
    """手順として並べられるか（見出しではない箇条書きが2件以上）。"""
    steps = [m for m in items if not m.is_heading]
    return len(steps) >= MIN_FLOW_STEPS


def build_chaptered_draft(
    outlines: list[DocumentOutline],
    plan: DurationPlan,
    *,
    title: str,
    purpose: str | None = None,
) -> dict[str, Any]:
    """章立ての台本を組む。

    構成は `plan.scenes_by_role` に従う:

        表紙 → [章扉 → 本編(説明/図解/比較)] x 章数 → まとめ → エンドカード
    """
    outline_by_path = {o.path: o for o in outlines}
    materials: list[Material] = []
    for outline in outlines:
        materials.extend(collect_materials(outline))

    body_total = (
        plan.scenes_by_role.get("explain", 0)
        + plan.scenes_by_role.get("diagram", 0)
        + plan.scenes_by_role.get("comparison", 0)
    )
    body_chunks = _chunks(materials, max(1, body_total))
    # ナレーション予算は `duration_planner` が本編シーンの目標尺から逆算した値を使う。
    # 表紙・章扉・エンドカードは内容が決まっていて長くできないので固定尺・固定予算。
    per_scene_chars = plan.narration_chars_per_body_scene or plan.narration_chars_per_scene

    scenes: list[dict[str, Any]] = []
    sound_events: list[dict[str, Any]] = []
    index = 1

    def next_id() -> str:
        nonlocal index
        scene_id = f"s{index:02d}"
        index += 1
        return scene_id

    # --- 表紙 ---
    cover_id = next_id()
    scenes.append(
        {
            "id": cover_id,
            "role": "intro",
            "title": title,
            "narration": {
                "text": _narration(
                    [purpose or f"{title}について説明します", "内容は社内資料に基づいています"],
                    FIXED_CARD_NARRATION_CHARS,
                ),
                "source_refs": [],
            },
            "on_screen_text": [(purpose or "社内向け説明")[:ONSCREEN_MAX]],
            "claims": [],
            "diagram": {"kind": "title"},
        }
    )
    sound_events.append({"scene_id": cover_id, "event": "intro", "anchor": "scene.start"})

    # --- 章ごとに「章扉 + 本編」を並べる ---
    chapter_count = max(1, plan.chapter_count)
    per_chapter = _chunks(body_chunks, chapter_count)
    kinds = (
        ["explain"] * plan.scenes_by_role.get("explain", 0)
        + ["diagram"] * plan.scenes_by_role.get("diagram", 0)
        + ["comparison"] * plan.scenes_by_role.get("comparison", 0)
    )
    kind_iter = iter(kinds)

    for chapter_index, chunk_group in enumerate(per_chapter, start=1):
        if not chunk_group:
            continue
        lead = chunk_group[0][0]
        chapter_title = (lead.text if lead.is_heading else outline_by_path[lead.path].title)[:60]
        chapter_id = next_id()
        path, start, end = _line_range([m for chunk in chunk_group for m in chunk], outline_by_path)
        scenes.append(
            {
                "id": chapter_id,
                "role": "chapter",
                "title": chapter_title,
                "narration": {
                    "text": _narration(
                        [f"第 {chapter_index} 章、{chapter_title}"], FIXED_CARD_NARRATION_CHARS
                    ),
                    "source_refs": ["s1"],
                },
                "on_screen_text": [],
                "claims": [],
                "diagram": {
                    "kind": "chapter",
                    # 章番号は diagram に載せる。`validate_script_draft` はシーン直下の
                    # 未知キーを落とすが、diagram はそのまま通すため。
                    "chapter_index": chapter_index,
                    "chapter_total": len(per_chapter),
                    "source": {"path": path, "start": start, "end": end},
                },
            }
        )
        sound_events.append(
            {"scene_id": chapter_id, "event": "chapter_change", "anchor": "scene.start"}
        )

        for chunk in chunk_group:
            if not chunk:
                continue
            kind = next(kind_iter, "explain")
            scene_id = next_id()
            path, start, end = _line_range(chunk, outline_by_path)
            source = {"path": path, "start": start, "end": end}
            # 画面に載せるのは先頭 N 件だけだが、**ナレーションは chunk の素材を
            # 予算いっぱいまで読む。** 同じ文を言い換えて増やすのではなく、
            # 既にある素材を使い切ることで目標尺へ近づける。
            points = [m.text for m in chunk][:MAX_POINTS_PER_SCENE]
            narration = _narration([m.text for m in chunk], per_scene_chars)

            if kind == "diagram" and _looks_like_steps(chunk):
                steps = [m.text for m in chunk if not m.is_heading][:MAX_FLOW_STEPS]
                scenes.append(
                    {
                        "id": scene_id,
                        "role": "diagram",
                        "title": f"{chapter_title}の流れ"[:60],
                        "narration": {"text": narration, "source_refs": ["s1"]},
                        "on_screen_text": [],
                        "claims": [],
                        "diagram": {"kind": "flow", "steps": steps, "source": source},
                    }
                )
            elif kind == "comparison" and len(points) >= 2:
                # 比較は「観点 x 対象」。原文の並びを2列へ割り、
                # **原文の文言をそのまま**セルにする（要約で意味を変えない）。
                scenes.append(
                    {
                        "id": scene_id,
                        "role": "quote",
                        "title": f"{chapter_title}の要点"[:60],
                        "narration": {"text": narration, "source_refs": ["s1"]},
                        "on_screen_text": [],
                        "claims": [],
                        "diagram": {
                            "kind": "quote",
                            "quotes": [p[:QUOTE_MAX] for p in points[:2]],
                            "attribution": path.rsplit("/", 1)[-1],
                            "source": source,
                        },
                    }
                )
            else:
                scenes.append(
                    {
                        "id": scene_id,
                        "role": "body",
                        "title": chapter_title,
                        "narration": {"text": narration, "source_refs": ["s1"]},
                        "on_screen_text": points[:2],
                        "claims": [
                            {"text": p, "kind": "fact", "source_refs": ["s1"]} for p in points
                        ],
                        "diagram": {"kind": "key_points", "source": source},
                    }
                )
            if len(points) >= 3:
                sound_events.append(
                    {"scene_id": scene_id, "event": "key_point", "anchor": "beat-2.reveal"}
                )

    # --- まとめ ---
    summary_points = [m.text for m in materials if m.is_heading][:3] or [
        m.text for m in materials[:3]
    ]
    summary_id = next_id()
    if summary_points:
        path, start, end = _line_range(
            [m for m in materials if m.text in summary_points][:3] or materials[:3],
            outline_by_path,
        )
        scenes.append(
            {
                "id": summary_id,
                "role": "summary",
                "title": "まとめ",
                "narration": {
                    "text": _narration(["以上が概要です", *summary_points], per_scene_chars),
                    "source_refs": ["s1"],
                },
                "on_screen_text": [],
                "claims": [
                    {"text": p, "kind": "fact", "source_refs": ["s1"]} for p in summary_points
                ],
                "diagram": {
                    "kind": "summary",
                    "source": {"path": path, "start": start, "end": end},
                },
            }
        )
        sound_events.append({"scene_id": summary_id, "event": "success", "anchor": "scene.start"})

    # --- エンドカード（出典一覧と社内限定の注意書き） ---
    ending_id = next_id()
    scenes.append(
        {
            "id": ending_id,
            "role": "ending",
            "title": "出典と注意事項",
            "narration": {
                "text": _narration(
                    ["詳細は出典の資料を参照してください", "この動画は社内限定です"],
                    FIXED_CARD_NARRATION_CHARS,
                ),
                "source_refs": [],
            },
            "on_screen_text": ["社外公開しないこと"],
            "claims": [],
            "diagram": {"kind": "ending"},
        }
    )
    sound_events.append({"scene_id": ending_id, "event": "outro", "anchor": "scene.end"})

    return {"title": title, "scenes": scenes, "sound_events": sound_events}


__all__ = [
    "MAX_FLOW_STEPS",
    "MAX_POINTS_PER_SCENE",
    "MIN_FLOW_STEPS",
    "NARRATION_MAX",
    "ONSCREEN_MAX",
    "QUOTE_MAX",
    "Material",
    "build_chaptered_draft",
    "collect_materials",
    "count_materials",
]
