"""動画へ出す文言の正規化と、レンダリング前の意味品質検査。"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

FORBIDDEN_HEADINGS = frozenset(
    {
        "凡例",
        "目次",
        "参照先",
        "参考",
        "参考資料",
        "関連資料",
        "会議情報",
        "会議概要",
        "報告概要",
        "基本情報",
    }
)

_HTML_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))+\)")
_ANCHOR_RE = re.compile(r"<a\s+[^>]*id\s*=.*?</a>", re.I)
_CHECKBOX_RE = re.compile(r"^\s*(?:[-*+]\s*)?\[[ xX]\]\s*")
_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}|>|[-*+])\s*")
_SECTION_RE = re.compile(r"^\s*(?:第\s*)?\d+(?:\.\d+)*(?:\s*章|[.)、:]\s*)?")
_MARKDOWN_RESIDUE = re.compile(
    r"(?:<[^>]+>|\[[ xX]\]|\[[^\]]+\]\([^)]+\)|\*\*|__|`{1,3}|^\s*#{1,6}\s)", re.M
)
_CONTEXTLESS_NUMBER = re.compile(r"^\s*(?:第\s*)?\d+(?:\.\d+)*(?:\s*章)?\s*$")


def clean_display_text(value: str) -> str:
    """Markdown/HTML/アンカーを除去し、画面に出せるプレーンテキストへする。"""
    text = html.unescape(str(value or ""))
    text = _ANCHOR_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HTML_RE.sub("", text)
    text = _CHECKBOX_RE.sub("", text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: m.group(1) or m.group(2), text)
    text = _PREFIX_RE.sub("", text)
    text = text.replace("`", "")
    text = re.sub(r"\s+", " ", text).strip(" |\t-–—")
    return text


def clean_heading(value: str) -> str:
    """章番号を外して見出しを正規化する。"""
    return _SECTION_RE.sub("", clean_display_text(value)).strip()


def is_forbidden_heading(value: str) -> bool:
    cleaned = clean_heading(value)
    return cleaned in FORBIDDEN_HEADINGS or any(
        cleaned.startswith(f"{name}（") or cleaned.startswith(f"{name}(")
        for name in FORBIDDEN_HEADINGS
    )


def is_semantic_text(value: str) -> bool:
    """単独で表示して意味が通る素材かを保守的に判定する。"""
    cleaned = clean_display_text(value)
    if len(cleaned) < 3 or is_forbidden_heading(cleaned):
        return False
    if _CONTEXTLESS_NUMBER.fullmatch(cleaned):
        return False
    lowered = cleaned.casefold()
    if any(token in lowered for token in ("https://", "http://", "<a id=", "../../")):
        return False
    return cleaned not in {"以上", "以下", "詳細", "概要", "内容", "備考", "状態", "担当"}


def iter_scene_texts(scenes: Iterable[dict[str, Any]]) -> Iterable[tuple[str, str]]:
    """検査対象の全表示文・ナレーション相当文を列挙する。"""
    for scene in scenes:
        scene_id = str(scene.get("id") or "?")
        for field, value in (
            ("title", scene.get("title")),
            ("narration", (scene.get("narration") or {}).get("text")),
        ):
            if isinstance(value, str) and value:
                yield f"{scene_id}.{field}", value
        for index, value in enumerate(scene.get("on_screen_text") or []):
            if isinstance(value, str):
                yield f"{scene_id}.on_screen_text[{index}]", value
        for index, beat in enumerate((scene.get("scene_spec") or {}).get("beats") or []):
            if not isinstance(beat, dict):
                continue
            for key in ("text", "label", "description", "side", "aspect", "at"):
                value = beat.get(key)
                if isinstance(value, str) and value:
                    yield f"{scene_id}.beats[{index}].{key}", value


def _validate_requirements(
    scenes: list[dict[str, Any]],
    sound_events: list[dict[str, Any]] | None,
    requirements: dict[str, Any],
) -> list[dict[str, str]]:
    """spec が自己申告した構成要件を検査する。

    かつては動画タイトルの部分一致で特定案件向けの要件（必須の顧客名など）を暗黙に
    適用していた。同種のタイトルを持つ別動画が無関係な要件を継承してしまうため、
    要件は spec 側の宣言に移した。ここは宣言されたものだけを見る。
    """
    errors: list[dict[str, str]] = []

    topics = requirements.get("required_topics") or []
    if topics:
        corpus = "\n".join(value for _path, value in iter_scene_texts(scenes))
        for topic in topics:
            if str(topic) not in corpus:
                errors.append(
                    {
                        "path": "scenes",
                        "code": "MISSING_STORY_TOPIC",
                        "message": f"必須テーマがありません: {topic}",
                    }
                )

    for kind in requirements.get("required_scene_kinds") or []:
        if not any(scene.get("kind") == kind for scene in scenes):
            errors.append(
                {
                    "path": "scenes",
                    "code": "MISSING_REQUIRED_SCENE_KIND",
                    "message": f"必須シーン種別がありません: {kind}",
                }
            )

    scene_count = requirements.get("scene_count")
    if isinstance(scene_count, dict):
        low, high = scene_count.get("min"), scene_count.get("max")
        if isinstance(low, int) and isinstance(high, int) and not low <= len(scenes) <= high:
            errors.append(
                {
                    "path": "scenes",
                    "code": "INVALID_STORY_SCENE_COUNT",
                    "message": f"{low}〜{high}シーンが必要です（現在 {len(scenes)}）",
                }
            )

    pattern = requirements.get("source_path_pattern")
    if isinstance(pattern, str) and pattern:
        try:
            compiled = re.compile(pattern)
        except re.error:
            compiled = None
        if compiled is not None:
            for scene in scenes:
                for source in (scene.get("scene_spec") or {}).get("sources") or []:
                    source_path = str(source.get("path") or "")
                    if not compiled.search(source_path):
                        errors.append(
                            {
                                "path": f"{scene.get('id')}.sources",
                                "code": "SOURCE_OUTSIDE_TARGET_PERIOD",
                                "message": f"対象期間外の資料です: {source_path}",
                            }
                        )

    event_range = requirements.get("sound_events")
    if isinstance(event_range, dict):
        low, high = event_range.get("min"), event_range.get("max")
        count = len(sound_events or [])
        if isinstance(low, int) and isinstance(high, int) and not low <= count <= high:
            errors.append(
                {
                    "path": "sound_events",
                    "code": "INVALID_SOUND_EVENT_COUNT",
                    "message": f"{low}〜{high}件が必要です（現在 {count}）",
                }
            )
    return errors


def validate_story_content(
    scenes: list[dict[str, Any]],
    sound_events: list[dict[str, Any]] | None = None,
    *,
    requirements: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """レンダリング前に、破綻した章・記法断片・構成不足を検出する。

    汎用検査（記法断片・禁止見出し・文脈のない章番号）は常に走る。構成の要件
    （必須テーマ・シーン数・出典期間など）は `requirements` が宣言されたときだけ。
    """
    errors: list[dict[str, str]] = []
    for path, value in iter_scene_texts(scenes):
        if _MARKDOWN_RESIDUE.search(value):
            errors.append(
                {"path": path, "code": "MARKUP_IN_VIDEO_TEXT", "message": f"記法断片: {value}"}
            )
        if path.endswith(".title") and is_forbidden_heading(value):
            errors.append(
                {"path": path, "code": "FORBIDDEN_VIDEO_HEADING", "message": f"禁止見出し: {value}"}
            )
        if path.endswith(".title") and _CONTEXTLESS_NUMBER.fullmatch(clean_display_text(value)):
            errors.append({"path": path, "code": "CONTEXTLESS_SECTION_NUMBER", "message": value})

    if requirements:
        errors.extend(_validate_requirements(scenes, sound_events, requirements))
    return errors


def write_storyboard_review(
    project_dir: Path,
    scenes: list[dict[str, Any]],
    sound_events: list[dict[str, Any]],
) -> tuple[Path, Path]:
    """レンダリング前に確認できる全表示文一覧を JSON/Markdown で残す。"""
    preview = project_dir / "preview"
    preview.mkdir(parents=True, exist_ok=True)
    items = []
    for scene in scenes:
        items.append(
            {
                "id": scene.get("id"),
                "kind": scene.get("kind"),
                "title": scene.get("title"),
                "on_screen_text": scene.get("on_screen_text") or [],
                "narration": (scene.get("narration") or {}).get("text"),
                "display_beats": [
                    {
                        k: b[k]
                        for k in ("type", "text", "label", "description", "at", "side", "aspect")
                        if k in b
                    }
                    for b in ((scene.get("scene_spec") or {}).get("beats") or [])
                    if isinstance(b, dict)
                ],
                "sources": (scene.get("scene_spec") or {}).get("sources") or [],
            }
        )
    payload = {"scene_count": len(items), "sound_event_count": len(sound_events), "scenes": items}
    json_path = preview / "storyboard-review.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = ["# レンダリング前 絵コンテ確認", ""]
    for item in items:
        md_lines += [f"## {item['id']} — {item['title']}", "", f"- 種別: {item['kind']}"]
        if item["on_screen_text"]:
            md_lines.append("- 表示文: " + " / ".join(item["on_screen_text"]))
        if item["narration"]:
            md_lines.append("- ナレーション相当文: " + item["narration"])
        md_lines.append("")
    md_path = preview / "storyboard-review.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    return json_path, md_path


__all__ = [
    "FORBIDDEN_HEADINGS",
    "clean_display_text",
    "clean_heading",
    "is_forbidden_heading",
    "is_semantic_text",
    "iter_scene_texts",
    "validate_story_content",
    "write_storyboard_review",
]
