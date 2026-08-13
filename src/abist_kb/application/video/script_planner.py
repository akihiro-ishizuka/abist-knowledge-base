"""台本の受け入れと、シーン別 SceneSpec の生成。

**台本はエージェントが書く。** 見出しや箇条書きを機械的に拾って台本を組み立てる
経路は持たない —— 何を語り、どう見せるかは書き手の判断であって、正規表現で
決められることではなかった。

**書かれた台本をそのまま描画はしない。** 経路は必ずこの順:

```
ResolvedInput（題材の解決）
  → ScriptDraft（作者が書く）
  → validate_script_draft（出典不良の主張を落とす）
  → build_scene_specs（SceneSpec 1.0 として検証）
  → validate_story_content（レンダリング前の意味品質検査）
  → VideoProjectSpec.scenes
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.application.video.caption_timing import caption_chars, estimate_caption_duration
from abist_kb.application.video.content_quality import (
    clean_display_text,
    clean_heading,
    is_forbidden_heading,
    is_semantic_text,
    validate_story_content,
)
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
    semantic_lines: list[tuple[str, int, str]] = field(default_factory=list)


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
    semantic_lines: list[tuple[str, int, str]] = []
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    for number, line in enumerate(lines, start=1):
        if in_frontmatter:
            if number > 1 and line.strip() == "---":
                in_frontmatter = False
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            cleaned = clean_heading(heading.group(2))
            if cleaned and not is_forbidden_heading(cleaned):
                headings.append((len(heading.group(1)), cleaned, number))
                semantic_lines.append((cleaned, number, "heading"))
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            cleaned = clean_display_text(bullet.group(1))
            if len(cleaned) <= 180 and is_semantic_text(cleaned):
                bullets.append((cleaned, number))
                semantic_lines.append((cleaned, number, "action"))
            continue
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [clean_display_text(c) for c in stripped.strip("|").split("|")]
            cells = [c for c in cells if c and not re.fullmatch(r":?-{2,}:?", c)]
            if len(cells) >= 2:
                candidate = "：".join(cells[:4])
                if len(candidate) <= 180 and is_semantic_text(candidate):
                    semantic_lines.append((candidate, number, "table"))
            continue
        if stripped.startswith(">"):
            cleaned = clean_display_text(stripped)
            if len(cleaned) <= 180 and is_semantic_text(cleaned):
                semantic_lines.append((cleaned, number, "decision"))
    title = headings[0][1] if headings else clean_heading(Path(item.path).stem)
    return DocumentOutline(
        path=item.path,
        title=title,
        content_hash=item.content_hash or "",
        total_lines=max(len(lines), 1),
        headings=headings,
        bullets=bullets,
        semantic_lines=semantic_lines,
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

    source_specs = diagram.get("sources") if isinstance(diagram.get("sources"), list) else None
    sources: list[dict[str, Any]] = []
    if source_specs:
        for index, info in enumerate(source_specs, start=1):
            if not isinstance(info, dict):
                continue
            source_outline = outline_by_path.get(info.get("path"))
            if source_outline is None:
                continue
            entry = _source_entry(
                source_outline, docs_dir, info.get("start", 1), info.get("end", 1)
            )
            entry["id"] = f"s{index}"
            sources.append(entry)
            if outline is None:
                outline = source_outline

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
    if not sources:
        sources = [source]
    kind = diagram.get("kind", "explain")

    card = _card_spec_from(scene, diagram, kind, title, sources)
    if card is not None:
        return card

    if kind == "timeline":
        beats = []
        for point in diagram.get("points") or []:
            if not isinstance(point, dict):
                continue
            source_index = int(point.get("source_index", 0))
            ref = f"s{source_index + 1}"
            beat = {
                "type": "timeline_point",
                "at": clean_display_text(str(point.get("at") or ""))[:24],
                "label": clean_display_text(str(point.get("label") or ""))[:40],
                "source_refs": [ref],
            }
            description = clean_display_text(str(point.get("description") or ""))
            if description:
                beat["description"] = description[:200]
            beats.append(beat)
        if len(beats) < 2:
            return None
        return {
            "schema_version": "1.0",
            "scene_kind": "timeline",
            "output_format": "mp4",
            "template": "timeline_v1",
            "title": title,
            "sources": sources,
            "beats": beats,
        }

    if kind == "comparison":
        beats = [
            {
                "type": "comparison_item",
                "side": clean_display_text(str(item.get("side") or ""))[:20],
                "aspect": clean_display_text(str(item.get("aspect") or ""))[:20],
                "text": clean_display_text(str(item.get("text") or ""))[:60],
                "source_refs": ["s1"],
            }
            for item in (diagram.get("items") or [])
            if isinstance(item, dict)
        ]
        if len(beats) < 2:
            return None
        return {
            "schema_version": "1.0",
            "scene_kind": "comparison",
            "output_format": "mp4",
            "template": "comparison_v1",
            "title": title,
            "sources": sources,
            "beats": beats,
        }

    if kind == "domain":
        beats: list[dict[str, Any]] = []
        for entity in diagram.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            beat: dict[str, Any] = {
                "type": "domain_entity",
                "name": clean_display_text(str(entity.get("name") or ""))[:24],
                "source_refs": ["s1"],
            }
            description = clean_display_text(str(entity.get("description") or ""))
            group = clean_display_text(str(entity.get("group") or ""))
            if description:
                beat["description"] = description[:60]
            if group:
                beat["group"] = group[:20]
            beats.append(beat)
        for relation in diagram.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            beat = {
                "type": "domain_relation",
                "from": clean_display_text(str(relation.get("from") or ""))[:24],
                "to": clean_display_text(str(relation.get("to") or ""))[:24],
                "source_refs": ["s1"],
            }
            label = clean_display_text(str(relation.get("label") or ""))
            if label:
                beat["label"] = label[:16]
            beats.append(beat)
        if len([beat for beat in beats if beat["type"] == "domain_entity"]) < 2:
            return None
        return {
            "schema_version": "1.0",
            "scene_kind": "domain",
            "output_format": "mp4",
            "template": "domain_map_v1",
            "title": title,
            "sources": sources,
            "beats": beats,
        }

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
            "sources": sources,
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
        "sources": sources,
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
    strict: bool = False,
) -> PlanResult:
    """検証済み台本から `VideoProjectSpec.scenes` を組む。

    **SceneSpec は必ず `validate_scene_spec` を通す。**

    `strict=False`（無人経路の既定）では、通らないシーンを落として warning にする
    （1シーンの不備で動画全体を失敗させない）。`strict=True`（作者がいる経路）では
    落とさずにエラーとして返す。作者にとっては「黙って消えたシーン」より
    「どこがなぜ描けないか」の方が要るため。
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
    errors: list[dict[str, str]] = []

    for scene in draft_result.draft["scenes"]:
        scene_spec = _scene_spec_from(scene, outline_by_path, docs_dir)
        if scene_spec is None:
            if strict:
                errors.append(
                    {
                        "sceneId": scene["id"],
                        "path": f"scenes[{scene['id']}]",
                        "code": "EMPTY_SCENE",
                        "message": "描画できる内容がありません",
                    }
                )
            else:
                warnings.append(f"{scene['id']}: 描画できる内容が無いため除外しました")
            continue
        validated = validate_scene_spec(scene_spec)
        if not validated.ok:
            if strict:
                errors.extend(
                    {
                        "sceneId": scene["id"],
                        "path": f"scenes[{scene['id']}].{error.path}"
                        if error.path
                        else scene["id"],
                        "code": error.code,
                        "message": error.message,
                    }
                    for error in validated.errors
                )
            else:
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

    if errors:
        return PlanResult(ok=False, errors=errors, warnings=warnings)
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


#: エラーコードごとの直し方。エージェントが検証結果だけを見て自力で直せるように、
#: 「何が悪いか」に加えて「どう直すか」を必ず添える。
FIX_HINTS: dict[str, str] = {
    "EMPTY_SCENE": "そのシーンに描ける中身がありません。diagram の points / items / steps か、"
    "on_screen_text を埋めてください",
    "INVALID_SCRIPT_DRAFT": "台本の形が ScriptDraft と合っていません。scenes[] の各要素に "
    "id / role / title を入れてください",
    "MISSING_SOURCE_REF": "事実を述べる beat には source_refs が要ります。装飾なら "
    "decorative: true を付けてください",
    "UNKNOWN_SOURCE_REF": "存在しない出典 id を参照しています。sources に無い id は使えません",
    "TOO_MANY_BEATS": "1シーンに載せすぎです。シーンを分けてください",
    "INVALID_BEAT_TYPE": "その scene_kind では使えない beat 種別です。list_scene_kinds で"
    "使える beat を確認してください",
    "TEXT_TOO_LONG": "画面に収まりません。短く言い換えてください",
    "INVALID_SCENE_SPEC": "SceneSpec の制約に反しています。list_scene_kinds の上限"
    "（要素数・文字数）を確認してください",
    "MARKUP_IN_VIDEO_TEXT": "Markdown / HTML の断片が残っています。素のテキストにしてください",
    "FORBIDDEN_VIDEO_HEADING": "「凡例」「目次」など資料の構造見出しは動画に出せません。"
    "内容を表す見出しに書き換えてください",
    "CONTEXTLESS_SECTION_NUMBER": "章番号だけの見出しは意味が伝わりません。内容を書いてください",
    "MISSING_STORY_TOPIC": "story_requirements で宣言した必須テーマが台本に出てきません。"
    "触れるか、要件から外してください",
    "MISSING_REQUIRED_SCENE_KIND": "宣言した必須シーン種別がありません。該当する種別の"
    "シーンを足してください",
    "INVALID_STORY_SCENE_COUNT": "宣言したシーン数の範囲から外れています",
    "SOURCE_OUTSIDE_TARGET_PERIOD": "宣言した対象期間の外の資料を引いています",
    "INVALID_SOUND_EVENT_COUNT": "宣言した効果音イベント数の範囲から外れています",
    "INSUFFICIENT_SOURCES": "読み取れる Markdown がありません。inputs を見直してください",
    "UNKNOWN_IMAGE_ASSET": "未登録の画像を参照しています。image_assets に登録してから "
    "asset_id で参照してください",
}

#: 直し方が分からないコードに使う汎用の案内。
DEFAULT_FIX_HINT = "検証エラーの message を読んで該当箇所を直してください"


def fix_hint_for(code: str | None) -> str:
    return FIX_HINTS.get(str(code or ""), DEFAULT_FIX_HINT)


@dataclass(frozen=True, slots=True)
class AuthorResult:
    """作者（エージェント）が書いた台本の受け入れ結果。"""

    ok: bool
    scenes: list[dict[str, Any]] = field(default_factory=list)
    sound_events: list[dict[str, Any]] = field(default_factory=list)
    used_paths: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    #: `{sceneId?, path, code, message, fixHint}`。作者が自力で直せる粒度で返す。
    errors: list[dict[str, str]] = field(default_factory=list)
    #: シーンごとの `{id, kind, role, title, captionChars, estimatedSec}`。
    scene_estimates: list[dict[str, Any]] = field(default_factory=list)
    estimated_duration_sec: float = 0.0
    duration_plan: dict[str, Any] | None = None


def _with_fix_hints(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**error, "fixHint": error.get("fixHint") or fix_hint_for(error.get("code"))}
        for error in errors
    ]


def _estimate_scenes(scenes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    """テロップ量からシーン別の尺を見積もる（ナレーション音声は無い）。"""
    estimates: list[dict[str, Any]] = []
    total = 0.0
    for scene in scenes:
        text = (scene.get("narration") or {}).get("text") or ""
        seconds = estimate_caption_duration(text)
        total += seconds
        estimates.append(
            {
                "id": scene.get("id"),
                "kind": scene.get("kind"),
                "role": scene.get("role"),
                "title": scene.get("title"),
                "captionChars": caption_chars(text),
                "estimatedSec": seconds,
                "sourcePaths": sorted(
                    {
                        str(source.get("path"))
                        for source in (scene.get("scene_spec") or {}).get("sources") or []
                    }
                ),
            }
        )
    return estimates, round(total, 2)


def author_script(
    raw_draft: dict[str, Any],
    resolved_inputs: list[ResolvedInput],
    *,
    docs_dir: Path,
    target_duration_sec: dict[str, float] | None = None,
    story_requirements: dict[str, Any] | None = None,
    strict: bool = True,
) -> AuthorResult:
    """作者が書いた台本を検証し、`VideoProjectSpec.scenes` まで組む。

    **台本をそのまま描画はしない。** 経路はルールベースの下書きと完全に同じ:

    ```
    ScriptDraft（作者）
      → validate_script_draft（出典不良の主張を落とす）
      → build_scenes → validate_scene_spec（SceneSpec 1.0 として検証）
      → validate_story_content（レンダリング前の意味品質検査）
    ```

    出典の `content_hash` は作者から受け取らない。作者が宣言した `{path, start, end}`
    から実ファイルを読んで計算する（作者が書いたハッシュを信じると、文書が変わっても
    検証を素通りしてしまう）。
    """
    outlines = [o for o in (outline_document(docs_dir, item) for item in resolved_inputs) if o]
    if not outlines:
        return AuthorResult(
            ok=False,
            errors=_with_fix_hints(
                [
                    {
                        "path": "inputs",
                        "code": "INSUFFICIENT_SOURCES",
                        "message": "読み取れる Markdown がありません",
                    }
                ]
            ),
        )

    primary_paths = {i.path for i in resolved_inputs if i.selection == SELECTION_EXPLICIT_PRIMARY}
    outlines.sort(key=lambda o: (o.path not in primary_paths, o.path))

    duration_plan = None
    if target_duration_sec is not None:
        # 尺の目安（何シーン・テロップ何字）を作者へ返すためだけに使う。
        # 「素材が足りるか」の門番はルールベースで組み立てていたとき固有の関心なので、
        # ここでは見ない（何を語るかは作者が決める）。
        duration_plan = plan_duration(target_duration_sec)

    validated = validate_script_draft(raw_draft, source_ids={"s1"})
    result = build_scenes(validated, outlines, docs_dir=docs_dir, strict=strict)
    if not result.ok:
        return AuthorResult(
            ok=False,
            warnings=result.warnings,
            errors=_with_fix_hints(result.errors),
            duration_plan=duration_plan.to_dict() if duration_plan else None,
        )

    semantic_errors = validate_story_content(
        result.scenes, result.sound_events, requirements=story_requirements
    )
    estimates, total_sec = _estimate_scenes(result.scenes)
    if semantic_errors:
        return AuthorResult(
            ok=False,
            scenes=result.scenes,
            sound_events=result.sound_events,
            used_paths=result.used_paths,
            warnings=result.warnings,
            errors=_with_fix_hints(semantic_errors),
            scene_estimates=estimates,
            estimated_duration_sec=total_sec,
            duration_plan=duration_plan.to_dict() if duration_plan else None,
        )

    return AuthorResult(
        ok=True,
        scenes=result.scenes,
        sound_events=result.sound_events,
        used_paths=result.used_paths,
        warnings=result.warnings,
        scene_estimates=estimates,
        estimated_duration_sec=total_sec,
        duration_plan=duration_plan.to_dict() if duration_plan else None,
    )


__all__ = [
    "DEFAULT_FIX_HINT",
    "FIX_HINTS",
    "AuthorResult",
    "DocumentOutline",
    "PlanResult",
    "author_script",
    "build_scenes",
    "fix_hint_for",
    "outline_document",
]
