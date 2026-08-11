"""台本・絵コンテ・シーン別 SceneSpec の生成。

**LLM 出力は直接レンダリングしない。** 経路は必ずこの順:

```
ResolvedInput（Phase 1）
  → ScriptDraft（LLM もしくはルールベース）
  → validate_script_draft（出典不良の主張を落とす）
  → build_scene_specs（SceneSpec 1.0 として検証）
  → VideoProjectSpec.scenes
```

LLM が使えない環境でも**ルールベースで台本を組める**ので、
`ChatProvider` の設定はブロッカーにならない。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.application.video.chapter_planner import build_chaptered_draft, count_materials
from abist_kb.application.video.duration_planner import plan_duration
from abist_kb.application.video.input_resolver import ResolvedInput
from abist_kb.domain.line_range import range_hash
from abist_kb.domain.scene_spec import validate_scene_spec
from abist_kb.domain.script_draft import DraftResult, validate_script_draft
from abist_kb.domain.video_project_spec import SELECTION_EXPLICIT_PRIMARY

#: 見出し行（`#` の数と本文）。ルールベースの章立てに使う。
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
#: 箇条書き（手順の抽出に使う）。
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")

#: 1シーンに載せる statement の上限（読める密度に保つ）。
MAX_STATEMENTS_PER_SCENE = 4
#: flow シーンに載せるステップの上限。
MAX_FLOW_STEPS = 6


@dataclass(frozen=True, slots=True)
class DocumentOutline:
    """Markdown から機械的に取り出した骨格（LLM 無しでも作れる）。"""

    path: str
    title: str
    content_hash: str
    total_lines: int
    headings: list[tuple[int, str, int]] = field(default_factory=list)  # (level, text, line)
    bullets: list[tuple[str, int]] = field(default_factory=list)  # (text, line)


def outline_document(docs_dir: Path, item: ResolvedInput) -> DocumentOutline | None:
    """Markdown を読み、見出しと箇条書きを行番号つきで取り出す。"""
    path = docs_dir / item.path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    headings: list[tuple[int, str, int]] = []
    bullets: list[tuple[str, int]] = []
    for number, line in enumerate(lines, start=1):
        heading = _HEADING_RE.match(line)
        if heading:
            headings.append((len(heading.group(1)), heading.group(2), number))
            continue
        bullet = _BULLET_RE.match(line)
        if bullet and len(bullet.group(1)) <= 60:
            bullets.append((bullet.group(1), number))
    title = headings[0][1] if headings else Path(item.path).stem
    return DocumentOutline(
        path=item.path,
        title=title,
        content_hash=item.content_hash or "",
        total_lines=max(len(lines), 1),
        headings=headings,
        bullets=bullets,
    )


def _source_entry(outline: DocumentOutline, docs_dir: Path, start: int, end: int) -> dict[str, Any]:
    """行範囲の出典（`content_hash` は必ず実ファイルから計算する）。"""
    text = (docs_dir / outline.path).read_text(encoding="utf-8")
    lo = max(1, min(start, outline.total_lines))
    hi = max(lo, min(end, outline.total_lines))
    result = range_hash(text, lo, hi)
    return {
        "id": "s1",
        "path": outline.path,
        "start_line": lo,
        "end_line": hi,
        "content_hash": result.hash if result.ok else "",
    }


def plan_draft_from_documents(
    outlines: list[DocumentOutline],
    *,
    title: str,
    purpose: str | None = None,
) -> dict[str, Any]:
    """LLM を使わずに台本下書きを組む（決定的・出典つき）。

    `ChatProvider` が無い環境でも動画を作れるようにするための経路。
    LLM 版と**同じ `ScriptDraft` 形式**を返すので、後段は共通。
    """
    scenes: list[dict[str, Any]] = []
    sound_events: list[dict[str, Any]] = []

    scenes.append(
        {
            "id": "s01",
            "role": "intro",
            "title": title,
            "narration": {"text": purpose or f"{title}について説明します。", "source_refs": []},
            "on_screen_text": [title],
            "claims": [],
        }
    )
    sound_events.append({"scene_id": "s01", "event": "intro", "anchor": "scene.start"})

    index = 2
    for outline in outlines:
        scene_id = f"s{index:02d}"
        # 見出しがあれば要点、無ければ箇条書きの先頭を使う
        points = [text for _level, text, _line in outline.headings[1:5]]
        if not points:
            points = [text for text, _line in outline.bullets[:MAX_STATEMENTS_PER_SCENE]]
        points = points[:MAX_STATEMENTS_PER_SCENE]
        if not points:
            index += 1
            continue

        first_line = outline.headings[0][2] if outline.headings else 1
        last_line = min(outline.total_lines, first_line + 40)
        scenes.append(
            {
                "id": scene_id,
                "role": "body",
                "title": outline.title,
                "narration": {
                    "text": f"{outline.title}について、要点を確認します。",
                    "source_refs": ["s1"],
                },
                "on_screen_text": points[:2],
                "claims": [{"text": p, "kind": "fact", "source_refs": ["s1"]} for p in points],
                "diagram": {
                    "kind": "explain",
                    "source": {"path": outline.path, "start": first_line, "end": last_line},
                },
            }
        )
        sound_events.append(
            {"scene_id": scene_id, "event": "chapter_change", "anchor": "scene.start"}
        )
        if len(points) >= 3:
            sound_events.append(
                {"scene_id": scene_id, "event": "key_point", "anchor": "beat-2.reveal"}
            )
        index += 1

        # 手順が並んでいれば flow シーンを足す
        steps = [text for text, _line in outline.bullets[:MAX_FLOW_STEPS]]
        if len(steps) >= 2:
            flow_id = f"s{index:02d}"
            scenes.append(
                {
                    "id": flow_id,
                    "role": "diagram",
                    "title": f"{outline.title}の流れ",
                    "narration": {
                        "text": "手順の流れは次のとおりです。",
                        "source_refs": ["s1"],
                    },
                    "on_screen_text": [],
                    "claims": [],
                    "diagram": {
                        "kind": "flow",
                        "steps": steps,
                        "source": {"path": outline.path, "start": first_line, "end": last_line},
                    },
                }
            )
            index += 1

    scenes.append(
        {
            "id": f"s{index:02d}",
            "role": "summary",
            "title": "まとめ",
            "narration": {
                "text": "以上が概要です。詳細は出典の資料を参照してください。",
                "source_refs": [],
            },
            "on_screen_text": ["まとめ"],
            "claims": [],
        }
    )
    sound_events.append({"scene_id": f"s{index:02d}", "event": "success", "anchor": "scene.start"})
    sound_events.append({"scene_id": f"s{index:02d}", "event": "outro", "anchor": "scene.end"})
    return {"title": title, "scenes": scenes, "sound_events": sound_events}


def parse_llm_draft(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """LLM の応答から JSON を取り出す（```json フェンス対応）。"""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON として解釈できません: {exc}"
    if not isinstance(parsed, dict):
        return None, "台本はオブジェクトである必要があります"
    return parsed, None


#: 動画専用カードの kind -> template。SceneSpec 側の SCENE_KINDS と対応する。
_CARD_TEMPLATES: dict[str, str] = {
    "title": "title_card",
    "chapter": "chapter_card",
    "key_points": "key_points",
    "quote": "quote_card",
    "summary": "summary_card",
    "cta": "cta_card",
    "ending": "ending_card",
    "image": "image_still",
}
#: 事実を述べない（＝装飾でよい）カード。表紙・行動喚起・エンドカードだけ。
_DECORATIVE_CARDS = frozenset({"title", "cta", "ending"})


def _card_beats(
    scene: dict[str, Any], diagram: dict[str, Any], kind: str, has_source: bool
) -> list[dict[str, Any]]:
    """カード kind ごとの beats を組む（出典の有無で decorative を切り替える）。"""
    decorative = kind in _DECORATIVE_CARDS or not has_source
    refs = [] if decorative else ["s1"]

    if kind == "quote":
        # 引用は decorative を認めない（出典が無いなら描かない）
        if not has_source:
            return []
        return [
            {
                "type": "quote",
                "text": text[:240],
                "attribution": diagram.get("attribution"),
                "source_refs": ["s1"],
            }
            for text in (diagram.get("quotes") or [])
            if isinstance(text, str) and text
        ]

    if kind == "image":
        path = diagram.get("image_path")
        if not isinstance(path, str) or not path:
            return []
        beat: dict[str, Any] = {"type": "image", "path": path}
        if diagram.get("caption"):
            beat["caption"] = str(diagram["caption"])[:200]
        if decorative:
            beat["decorative"] = True
        else:
            beat["source_refs"] = ["s1"]
        return [beat]

    texts: list[str] = []
    if kind in ("key_points", "summary"):
        texts = [c["text"] for c in (scene.get("claims") or []) if isinstance(c.get("text"), str)]
    if not texts:
        texts = [t for t in (scene.get("on_screen_text") or []) if isinstance(t, str)]
    if not texts and kind in ("chapter", "title", "ending", "cta"):
        # 章扉・表紙は本文が無くてもタイトルだけで成立する（beat 0 件を許す kind）
        return []

    beats: list[dict[str, Any]] = []
    for text in texts[:MAX_STATEMENTS_PER_SCENE]:
        entry: dict[str, Any] = {"type": "statement", "text": text[:200]}
        if decorative:
            entry["decorative"] = True
        else:
            entry["source_refs"] = refs
        beats.append(entry)
    return beats


def _card_spec_from(
    scene: dict[str, Any],
    diagram: dict[str, Any],
    kind: Any,
    title: str,
    sources: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """動画専用カードの SceneSpec（対象外の kind なら None）。"""
    template = _CARD_TEMPLATES.get(kind) if isinstance(kind, str) else None
    if template is None:
        return None
    beats = _card_beats(scene, diagram, kind, has_source=bool(sources))
    if kind in ("quote", "image", "key_points", "summary") and not beats:
        return None  # 中身の無いカードは作らない（空の画面を出さない）
    spec: dict[str, Any] = {
        "schema_version": "1.0",
        "scene_kind": kind,
        "output_format": "mp4",
        "template": template,
        "title": title,
        "sources": sources,
        "beats": beats,
    }
    for key in ("chapter_index", "chapter_total"):
        if isinstance(diagram.get(key), int):
            spec[key] = diagram[key]
    return spec


def _scene_spec_from(
    scene: dict[str, Any], outline_by_path: dict[str, DocumentOutline], docs_dir: Path
) -> dict[str, Any] | None:
    """検証済みシーンから SceneSpec 1.0 を組む（描けないシーンは None）。"""
    diagram = scene.get("diagram") or {}
    source_info = diagram.get("source") or {}
    outline = outline_by_path.get(source_info.get("path"))
    title = scene.get("title") or scene["id"]

    if outline is None:
        # 出典に紐づかないシーン（表紙・エンドカード）は装飾のみで描く
        card = _card_spec_from(scene, diagram, diagram.get("kind"), title, [])
        if card is not None:
            return card
        texts = scene.get("on_screen_text") or [title]
        return {
            "schema_version": "1.0",
            "scene_kind": "explain",
            "output_format": "mp4",
            "template": "step_explanation",
            "title": title,
            "sources": [],
            "beats": [{"type": "statement", "text": t, "decorative": True} for t in texts[:3]],
        }

    source = _source_entry(
        outline, docs_dir, source_info.get("start", 1), source_info.get("end", 1)
    )
    kind = diagram.get("kind", "explain")

    card = _card_spec_from(scene, diagram, kind, title, [source])
    if card is not None:
        return card

    if kind == "flow":
        steps = [s for s in (diagram.get("steps") or []) if isinstance(s, str)][:MAX_FLOW_STEPS]
        if len(steps) < 2:
            return None
        beats: list[dict[str, Any]] = [
            {"type": "flow_step", "label": s[:24], "source_refs": ["s1"]} for s in steps
        ]
        beats += [
            {"type": "transition", "from": steps[i][:24], "to": steps[i + 1][:24]}
            for i in range(len(steps) - 1)
        ]
        return {
            "schema_version": "1.0",
            "scene_kind": "flow",
            "output_format": "mp4",
            "template": "data_flow_v1",
            "title": title,
            "sources": [source],
            "beats": beats,
        }

    claims = scene.get("claims") or []
    beats = [
        {"type": "statement", "text": c["text"][:200], "source_refs": ["s1"]}
        for c in claims[:MAX_STATEMENTS_PER_SCENE]
    ]
    if not beats:
        beats = [
            {"type": "statement", "text": t[:200], "decorative": True}
            for t in (scene.get("on_screen_text") or [title])[:3]
        ]
    return {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "mp4",
        "template": "step_explanation",
        "title": title,
        "sources": [source],
        "beats": beats,
    }


@dataclass(frozen=True, slots=True)
class PlanResult:
    ok: bool
    scenes: list[dict[str, Any]] = field(default_factory=list)
    sound_events: list[dict[str, Any]] = field(default_factory=list)
    used_paths: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    #: 目標尺から逆算した構成（`target_duration_sec` を渡したときだけ入る）。
    duration_plan: dict[str, Any] | None = None


def build_scenes(
    draft_result: DraftResult,
    outlines: list[DocumentOutline],
    *,
    docs_dir: Path,
) -> PlanResult:
    """検証済み台本から `VideoProjectSpec.scenes` を組む。

    **SceneSpec は必ず `validate_scene_spec` を通す。** 通らないシーンは
    落として warning にする（動画全体は失敗させない）。
    """
    if not draft_result.ok or draft_result.draft is None:
        return PlanResult(
            ok=False,
            errors=[e.to_dict() for e in draft_result.errors],
            warnings=draft_result.warnings,
        )

    outline_by_path = {o.path: o for o in outlines}
    scenes: list[dict[str, Any]] = []
    used: set[str] = set()
    warnings = list(draft_result.warnings)

    for scene in draft_result.draft["scenes"]:
        scene_spec = _scene_spec_from(scene, outline_by_path, docs_dir)
        if scene_spec is None:
            warnings.append(f"{scene['id']}: 描画できる内容が無いため除外しました")
            continue
        validated = validate_scene_spec(scene_spec)
        if not validated.ok:
            warnings.append(
                f"{scene['id']}: SceneSpec 検証に失敗したため除外しました "
                f"({validated.errors[0].code})"
            )
            continue
        for source in scene_spec.get("sources") or []:
            used.add(source["path"])
        entry: dict[str, Any] = {
            "id": scene["id"],
            "role": scene["role"],
            "kind": scene_spec["scene_kind"],
            "title": scene_spec.get("title"),
            "on_screen_text": scene.get("on_screen_text") or [],
            "narration": scene.get("narration") or {"text": None, "source_refs": []},
            "scene_spec": scene_spec,
        }
        # 章番号は動画メタデータ（チャプター）の元になるので scenes[] にも残す
        for key in ("chapter_index", "chapter_total"):
            if isinstance(scene_spec.get(key), int):
                entry[key] = scene_spec[key]
        scenes.append(entry)

    if not scenes:
        return PlanResult(
            ok=False,
            errors=[
                {
                    "path": "scenes",
                    "code": "INVALID_SCRIPT_DRAFT",
                    "message": "描画できるシーンが1つも残りませんでした",
                }
            ],
            warnings=warnings,
        )
    kept_ids = {s["id"] for s in scenes}
    events = [e for e in draft_result.draft["sound_events"] if e["scene_id"] in kept_ids]
    return PlanResult(
        ok=True, scenes=scenes, sound_events=events, used_paths=used, warnings=warnings
    )


def plan_video(
    resolved_inputs: list[ResolvedInput],
    *,
    docs_dir: Path,
    title: str,
    purpose: str | None = None,
    chat_fn: Any | None = None,
    target_duration_sec: dict[str, float] | None = None,
) -> tuple[PlanResult, dict[str, Any] | None]:
    """題材から台本を作り、SceneSpec まで組む。

    `chat_fn` が無ければ**ルールベース**で組む（外部設定はブロッカーにしない）。
    LLM を使う場合も出力は `validate_script_draft` を必ず通す。

    `target_duration_sec` を渡すと**目標尺から逆算した章立て**で組む。
    関連情報だけでは目標尺に届かない場合は `INSUFFICIENT_CONTENT_FOR_DURATION`
    を返して止まる（水増しした説明で尺を埋めない）。
    """
    outlines = [o for o in (outline_document(docs_dir, i) for i in resolved_inputs) if o]
    if not outlines:
        return (
            PlanResult(
                ok=False,
                errors=[
                    {
                        "path": "inputs",
                        "code": "INSUFFICIENT_SOURCES",
                        "message": "読み取れる Markdown がありません",
                    }
                ],
            ),
            None,
        )

    # 明示主入力を先頭へ（本編の骨格にするため）
    primary = [i for i in resolved_inputs if i.selection == SELECTION_EXPLICIT_PRIMARY]
    primary_paths = {i.path for i in primary}
    outlines.sort(key=lambda o: (o.path not in primary_paths, o.path))

    raw_draft: dict[str, Any] | None = None
    warnings: list[str] = []
    if chat_fn is not None:
        try:
            raw = chat_fn(outlines=outlines, title=title, purpose=purpose)
            parsed, error = parse_llm_draft(raw) if isinstance(raw, str) else (raw, None)
            if error:
                warnings.append(f"LLM 応答を解釈できないためルールベースへ切り替えます: {error}")
            else:
                raw_draft = parsed
        except Exception as exc:  # noqa: BLE001 - provider の失敗で動画生成を止めない
            warnings.append(f"LLM が利用できないためルールベースへ切り替えます: {exc}")

    duration_plan = None
    if raw_draft is None and target_duration_sec is not None:
        # 目標尺から逆算した章立て。素材が足りなければここで止まる
        # （入力文書を機械的に増やして尺を埋めることはしない）。
        duration_plan = plan_duration(
            target_duration_sec, available_materials=count_materials(outlines)
        )
        if not duration_plan.ok:
            return (
                PlanResult(
                    ok=False,
                    errors=[
                        {
                            "path": "format.target_duration_sec",
                            "code": duration_plan.code or "INVALID_VIDEO_SPEC",
                            "message": duration_plan.message or "",
                        }
                    ],
                    warnings=warnings,
                    duration_plan=duration_plan.to_dict(),
                ),
                None,
            )
        warnings.extend(duration_plan.warnings)
        raw_draft = build_chaptered_draft(outlines, duration_plan, title=title, purpose=purpose)

    if raw_draft is None:
        raw_draft = plan_draft_from_documents(outlines, title=title, purpose=purpose)

    validated = validate_script_draft(raw_draft, source_ids={"s1"})
    result = build_scenes(validated, outlines, docs_dir=docs_dir)
    return (
        PlanResult(
            ok=result.ok,
            scenes=result.scenes,
            sound_events=result.sound_events,
            used_paths=result.used_paths,
            warnings=[*warnings, *result.warnings],
            errors=result.errors,
            duration_plan=duration_plan.to_dict() if duration_plan else None,
        ),
        raw_draft,
    )


__all__ = [
    "DocumentOutline",
    "PlanResult",
    "build_scenes",
    "outline_document",
    "parse_llm_draft",
    "plan_draft_from_documents",
    "plan_video",
]
