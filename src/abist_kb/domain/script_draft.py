"""LLM が返す台本下書き（`ScriptDraft`）の検証。

**LLM 出力を直接レンダリングしない。** ここを通ったものだけが
`VideoProjectSpec` の `scenes` になる。

境界で守ること:

1. 事実(`fact`)を述べる主張には出典が要る。無いものは落とす
2. 効果音のファイル名を LLM に選ばせない（`sound` / `path` / `file` が
   混入したら `INVALID_SOUND_EVENT`）
3. 未知の `event` / `anchor` 形式を通さない
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: シーンの役割。テンプレートの選択に使う。
SCENE_ROLES: tuple[str, ...] = (
    "intro",
    "problem",
    "body",
    "diagram",
    "summary",
    "cta",
    "ending",
)

#: 主張の種類。`fact` だけが出典必須。
CLAIM_KINDS: tuple[str, ...] = ("fact", "example", "opinion")

#: 意味イベント（音源名ではない）。Phase 6 の Resolver がカテゴリへ落とす。
SOUND_EVENTS: tuple[str, ...] = (
    "intro",
    "chapter_change",
    "key_point",
    "comparison_change",
    "decision",
    "warning",
    "success",
    "error",
    "outro",
)

#: 効果音の音源を指定しようとしたときに現れるキー（拒否対象）。
_FORBIDDEN_SOUND_KEYS: tuple[str, ...] = ("sound", "sound_id", "path", "file", "src", "asset")

#: `anchor` の形式。`scene.start` / `scene.end` / `chapter.enter` / `beat-N.reveal`。
_ANCHOR_RE = re.compile(r"^(scene\.(start|end)|chapter\.enter|beat-\d+\.reveal)$")

_NARRATION_MAX = 400
_ONSCREEN_MAX = 60
_TITLE_MAX = 80


@dataclass(frozen=True, slots=True)
class DraftError:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class DraftResult:
    ok: bool
    draft: dict[str, Any] | None = None
    errors: list[DraftError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _is_obj(v: Any) -> bool:
    return isinstance(v, dict)


def _validate_claims(
    claims: Any, at: str, source_ids: set[str], errors: list[DraftError]
) -> list[dict[str, Any]]:
    """出典の付いた主張だけを残す。`fact` で出典が無いものは落とす。"""
    if claims is None:
        return []
    if not isinstance(claims, list):
        errors.append(DraftError(f"{at}.claims", "invalid", "claims は配列です"))
        return []
    kept: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        path = f"{at}.claims[{index}]"
        if not _is_obj(claim):
            errors.append(DraftError(path, "invalid", "claim はオブジェクトです"))
            continue
        text = claim.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(DraftError(f"{path}.text", "invalid", "text を指定してください"))
            continue
        kind = claim.get("kind", "fact")
        if kind not in CLAIM_KINDS:
            errors.append(
                DraftError(f"{path}.kind", "invalid", f"kind は {' / '.join(CLAIM_KINDS)} です")
            )
            continue
        refs = [r for r in (claim.get("source_refs") or []) if isinstance(r, str)]
        unknown = [r for r in refs if r not in source_ids]
        if unknown:
            errors.append(
                DraftError(
                    f"{path}.source_refs",
                    "unknown_source_ref",
                    f"未知の出典 id: {' / '.join(unknown)}",
                )
            )
            continue
        if kind == "fact" and not refs:
            # 事実の主張で出典が無いものは**落とす**（創作を通さない）
            continue
        kept.append({"text": text, "kind": kind, "source_refs": refs})
    return kept


def _validate_sound_events(
    events: Any, scene_ids: set[str], errors: list[DraftError]
) -> list[dict[str, Any]]:
    """意味イベントだけを残す。音源指定が混入していたら弾く。"""
    if events is None:
        return []
    if not isinstance(events, list):
        errors.append(DraftError("sound_events", "invalid", "sound_events は配列です"))
        return []
    kept: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        at = f"sound_events[{index}]"
        if not _is_obj(event):
            errors.append(DraftError(at, "invalid", "オブジェクトで指定します"))
            continue
        forbidden = [k for k in _FORBIDDEN_SOUND_KEYS if k in event]
        if forbidden:
            errors.append(
                DraftError(
                    at,
                    "INVALID_SOUND_EVENT",
                    "効果音の音源は指定できません（意味イベントだけを書いてください）: "
                    + " / ".join(forbidden),
                )
            )
            continue
        name = event.get("event")
        if name not in SOUND_EVENTS:
            errors.append(
                DraftError(
                    f"{at}.event",
                    "INVALID_SOUND_EVENT",
                    f"event は {' / '.join(SOUND_EVENTS)} のいずれかです（指定: {name}）",
                )
            )
            continue
        scene_id = event.get("scene_id")
        if scene_id not in scene_ids:
            errors.append(
                DraftError(f"{at}.scene_id", "unknown_scene", f"未知のシーン: {scene_id}")
            )
            continue
        anchor = event.get("anchor", "scene.start")
        if not isinstance(anchor, str) or not _ANCHOR_RE.match(anchor):
            errors.append(
                DraftError(
                    f"{at}.anchor",
                    "INVALID_SOUND_EVENT",
                    "anchor は scene.start / scene.end / chapter.enter / beat-N.reveal です",
                )
            )
            continue
        entry: dict[str, Any] = {"scene_id": scene_id, "event": name, "anchor": anchor}
        intensity = event.get("intensity")
        if intensity in ("off", "subtle", "normal"):
            entry["intensity"] = intensity
        kept.append(entry)
    return kept


def validate_script_draft(draft: Any, *, source_ids: set[str]) -> DraftResult:
    """LLM 出力を検証し、レンダリング可能な形だけを残す。

    **出典不良の主張は落とし、warning を残す。** 中断はしない
    （purring の `verify_sources` と同じ剪定ポリシー）。
    """
    if not _is_obj(draft):
        return DraftResult(
            ok=False, errors=[DraftError("", "INVALID_SCRIPT_DRAFT", "台本はオブジェクトです")]
        )

    errors: list[DraftError] = []
    warnings: list[str] = []

    title = draft.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= _TITLE_MAX):
        errors.append(DraftError("title", "invalid", f"title は 1〜{_TITLE_MAX} 文字です"))

    raw_scenes = draft.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        errors.append(DraftError("scenes", "invalid", "scenes を1件以上指定してください"))
        return DraftResult(ok=False, errors=errors)

    scenes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, scene in enumerate(raw_scenes):
        at = f"scenes[{index}]"
        if not _is_obj(scene):
            errors.append(DraftError(at, "invalid", "シーンはオブジェクトです"))
            continue
        scene_id = scene.get("id")
        if not isinstance(scene_id, str) or not scene_id:
            scene_id = f"s{index + 1:02d}"
        if scene_id in seen_ids:
            errors.append(DraftError(f"{at}.id", "duplicate_id", f"シーン id 重複: {scene_id}"))
            continue
        seen_ids.add(scene_id)

        role = scene.get("role", "body")
        if role not in SCENE_ROLES:
            errors.append(
                DraftError(f"{at}.role", "invalid", f"role は {' / '.join(SCENE_ROLES)} です")
            )
            continue

        narration = scene.get("narration") or {}
        narration_text = narration.get("text") if _is_obj(narration) else None
        if narration_text is not None and (
            not isinstance(narration_text, str) or len(narration_text) > _NARRATION_MAX
        ):
            errors.append(
                DraftError(
                    f"{at}.narration.text",
                    "invalid",
                    f"ナレーションは {_NARRATION_MAX} 文字以内です",
                )
            )
            continue

        on_screen_raw = scene.get("on_screen_text") or []
        on_screen: list[str] = []
        if isinstance(on_screen_raw, list):
            for text in on_screen_raw:
                if isinstance(text, str) and 1 <= len(text) <= _ONSCREEN_MAX:
                    on_screen.append(text)
                elif isinstance(text, str):
                    warnings.append(
                        f"{at}: 画面表示が長すぎるため除外しました（{_ONSCREEN_MAX} 文字以内）"
                    )

        claims = _validate_claims(scene.get("claims"), at, source_ids, errors)
        narration_refs = [
            r
            for r in (narration.get("source_refs") or [] if _is_obj(narration) else [])
            if isinstance(r, str) and r in source_ids
        ]
        # ナレーションに出典が無く、主張も残らなかったシーンは「事実を語れない」
        if narration_text and not narration_refs and not claims:
            warnings.append(
                f"{at}: 出典が無いためナレーションを装飾扱いにしました（事実は述べません）"
            )

        scenes.append(
            {
                "id": scene_id,
                "role": role,
                "title": scene.get("title") if isinstance(scene.get("title"), str) else None,
                "narration": {"text": narration_text, "source_refs": narration_refs},
                "on_screen_text": on_screen,
                "claims": claims,
                "diagram": scene.get("diagram") if _is_obj(scene.get("diagram")) else None,
            }
        )

    if not scenes:
        errors.append(DraftError("scenes", "INVALID_SCRIPT_DRAFT", "有効なシーンがありません"))

    sound_events = _validate_sound_events(draft.get("sound_events"), seen_ids, errors)

    if errors:
        return DraftResult(ok=False, errors=errors, warnings=warnings)
    return DraftResult(
        ok=True,
        draft={"title": title, "scenes": scenes, "sound_events": sound_events},
        warnings=warnings,
    )


__all__ = [
    "CLAIM_KINDS",
    "SCENE_ROLES",
    "SOUND_EVENTS",
    "DraftError",
    "DraftResult",
    "validate_script_draft",
]
