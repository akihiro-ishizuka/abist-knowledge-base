"""効果音の解決と配置（SoundEventResolver）。

**利用者も LLM も音源ファイルを指定しない。** Phase 3 が出した意味イベント
（`event` + `anchor`）を、承認済みパレットから**決定的に**音源へ落とし、
実測タイムラインへ配置する。

過剰抑制の既定:

- `intensity: subtle`
- 1シーン最大 2〜3 回（subtle=2, normal=3）
- 最短間隔 0.8 秒
- ナレーションと重なる位置はずらす／音量を下げる

BGM と AI 音楽生成は**扱わない**（計画のスコープ外）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: 意味イベント -> 効果音カテゴリ（決定的な対応表）。
EVENT_TO_CATEGORY: dict[str, str] = {
    "intro": "startup",
    "chapter_change": "transition",
    "key_point": "accent",
    "comparison_change": "swipe",
    "decision": "branch",
    "warning": "caution",
    "success": "complete",
    "error": "error",
    "outro": "ending",
}

#: 強度ごとの1シーン上限。
MAX_PER_SCENE = {"off": 0, "subtle": 2, "normal": 3}
#: 最短間隔（秒）。これより近い cue は優先度の低い方を落とす。
MIN_INTERVAL_SEC = 0.8
#: 優先度（高いほど残す）。
EVENT_PRIORITY: dict[str, int] = {
    "error": 5,
    "warning": 5,
    "success": 4,
    "key_point": 3,
    "decision": 3,
    "chapter_change": 2,
    "comparison_change": 2,
    "intro": 1,
    "outro": 1,
}

#: 既定音量（dB）。ナレーション中はさらに下げる。
DEFAULT_GAIN_DB = -16.0
NARRATION_DUCK_DB = -6.0
#: ナレーション開始と重なったときにずらす秒数。
NARRATION_SHIFT_SEC = 0.2


@dataclass(frozen=True, slots=True)
class SoundAsset:
    id: str
    categories: tuple[str, ...]
    path: str
    license: str
    attribution: str
    sha256: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class SoundCue:
    scene_id: str
    event: str
    anchor: str
    t_sec: float
    sound_id: str
    gain_db: float
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    cues: list[SoundCue] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "cues": [c.to_dict() for c in self.cues],
            "dropped": self.dropped,
            "warnings": self.warnings,
        }


def load_palette(manifest_path: Path) -> tuple[list[SoundAsset], list[str]]:
    """承認済みパレットを読む。

    **ライセンスと SHA-256 が無い音源は使わない。** 帰属を成果物へ転記できない
    素材を動画に載せないため。
    """
    warnings: list[str] = []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], [f"効果音パレットを読めません: {manifest_path}"]

    assets: list[SoundAsset] = []
    base = manifest_path.parent
    for entry in payload.get("assets") or []:
        required = ("id", "categories", "path", "license", "sha256")
        if not all(entry.get(k) for k in required):
            warnings.append(f"必須項目が欠けた音源を除外しました: {entry.get('id')}")
            continue
        file_path = base / entry["path"]
        if not file_path.is_file():
            warnings.append(f"音源が見つかりません: {entry['path']}")
            continue
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            warnings.append(f"音源の SHA-256 が一致しません: {entry['id']}")
            continue
        assets.append(
            SoundAsset(
                id=entry["id"],
                categories=tuple(entry["categories"]),
                path=str(file_path),
                license=entry["license"],
                attribution=entry.get("attribution", ""),
                sha256=entry["sha256"],
                duration_ms=int(entry.get("duration_ms") or 0),
            )
        )
    return assets, warnings


def _pick(
    assets: list[SoundAsset], category: str, scene_id: str, recent: list[str]
) -> SoundAsset | None:
    """カテゴリから**決定的に**1音を選ぶ（乱数は使わない）。

    同一入力 → 同一音。直近で使った音は避けるが、その判断も
    `scene_id` のハッシュで安定化させるので再現性は崩れない。
    """
    candidates = sorted([a for a in assets if category in a.categories], key=lambda a: a.id)
    if not candidates:
        return None
    fresh = [a for a in candidates if a.id not in recent] or candidates
    seed = int(hashlib.sha256(scene_id.encode("utf-8")).hexdigest()[:8], 16)
    return fresh[seed % len(fresh)]


def _anchor_time(
    anchor: str, *, scene_start: float, scene_duration: float, beat_times: list[float] | None
) -> float | None:
    """`anchor` を実測タイムライン上の秒へ写す（解決できなければ None）。"""
    if anchor in ("scene.start", "chapter.enter"):
        return scene_start
    if anchor == "scene.end":
        return scene_start + max(0.0, scene_duration - 0.4)
    if anchor.startswith("beat-"):
        try:
            index = int(anchor.split("-", 1)[1].split(".", 1)[0]) - 1
        except (ValueError, IndexError):
            return None
        if beat_times and 0 <= index < len(beat_times):
            return scene_start + beat_times[index]
        # beat の実測が無いときはシーンを等分した位置に落とす
        if index >= 0:
            fraction = min(0.9, (index + 1) / 6)
            return scene_start + scene_duration * fraction
    return None


def resolve_sound_events(
    events: list[dict[str, Any]],
    *,
    assets: list[SoundAsset],
    offsets: dict[str, float],
    durations: dict[str, float],
    narration_starts: dict[str, float] | None = None,
    intensity: str = "subtle",
    enabled: bool = True,
    beat_times: dict[str, list[float]] | None = None,
) -> ResolveOutcome:
    """意味イベントを音源へ解決し、実測タイムラインへ配置する。"""
    if not enabled or intensity == "off":
        return ResolveOutcome(warnings=["効果音は無効化されています"])
    if not assets:
        return ResolveOutcome(warnings=["承認済みの効果音がありません（無音で継続します）"])

    limit = MAX_PER_SCENE.get(intensity, 2)
    dropped: list[dict[str, str]] = []
    warnings: list[str] = []
    by_scene: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_scene.setdefault(str(event.get("scene_id")), []).append(event)

    cues: list[SoundCue] = []
    recent: list[str] = []

    for scene_id in sorted(by_scene):
        start = offsets.get(scene_id)
        duration = durations.get(scene_id)
        if start is None or duration is None:
            for event in by_scene[scene_id]:
                dropped.append(
                    {"scene_id": scene_id, "event": event.get("event", ""), "reason": "no_timeline"}
                )
            continue

        # 優先度の高い順に見て、上限まで採る
        ordered = sorted(
            by_scene[scene_id],
            key=lambda e: -EVENT_PRIORITY.get(str(e.get("event")), 0),
        )
        placed: list[SoundCue] = []
        for event in ordered:
            name = str(event.get("event"))
            if len(placed) >= limit:
                dropped.append({"scene_id": scene_id, "event": name, "reason": "max_per_scene"})
                continue
            category = EVENT_TO_CATEGORY.get(name)
            if category is None:
                dropped.append({"scene_id": scene_id, "event": name, "reason": "unknown_event"})
                continue
            asset = _pick(assets, category, scene_id, recent)
            if asset is None:
                dropped.append({"scene_id": scene_id, "event": name, "reason": "no_asset"})
                continue

            anchor = str(event.get("anchor") or "scene.start")
            at = _anchor_time(
                anchor,
                scene_start=start,
                scene_duration=duration,
                beat_times=(beat_times or {}).get(scene_id),
            )
            if at is None:
                # 推測で真ん中に置いたりしない（誤爆防止）
                dropped.append({"scene_id": scene_id, "event": name, "reason": "unresolved_anchor"})
                continue

            gain = DEFAULT_GAIN_DB
            narration_start = (narration_starts or {}).get(scene_id)
            if narration_start is not None and abs(at - narration_start) < NARRATION_SHIFT_SEC:
                # ナレーションの出だしと重なるならずらして、さらに音量を下げる
                at += NARRATION_SHIFT_SEC
                gain += NARRATION_DUCK_DB
                warnings.append(f"{scene_id}: ナレーションと重なるため {name} をずらしました")

            # 最短間隔を守る（近すぎるものは優先度の低い方＝後から来た方を落とす）
            too_close = [c for c in placed if abs(c.t_sec - at) < MIN_INTERVAL_SEC]
            if too_close:
                dropped.append({"scene_id": scene_id, "event": name, "reason": "min_interval"})
                continue

            cue = SoundCue(
                scene_id=scene_id,
                event=name,
                anchor=anchor,
                t_sec=round(min(at, start + duration - 0.05), 3),
                sound_id=asset.id,
                gain_db=round(gain, 1),
                sha256=asset.sha256,
            )
            placed.append(cue)
            recent.append(asset.id)
            recent[:] = recent[-4:]

        cues.extend(sorted(placed, key=lambda c: c.t_sec))

    return ResolveOutcome(
        cues=sorted(cues, key=lambda c: c.t_sec), dropped=dropped, warnings=warnings
    )


def used_attributions(cues: list[SoundCue], assets: list[SoundAsset]) -> list[dict[str, str]]:
    """使用した音源のライセンス・帰属（`citations.json` と説明文へ転記する）。"""
    by_id = {a.id: a for a in assets}
    seen: dict[str, dict[str, str]] = {}
    for cue in cues:
        asset = by_id.get(cue.sound_id)
        if asset and asset.id not in seen:
            seen[asset.id] = {
                "sound_id": asset.id,
                "license": asset.license,
                "attribution": asset.attribution,
                "sha256": asset.sha256,
            }
    return list(seen.values())


__all__ = [
    "DEFAULT_GAIN_DB",
    "EVENT_TO_CATEGORY",
    "MAX_PER_SCENE",
    "MIN_INTERVAL_SEC",
    "ResolveOutcome",
    "SoundAsset",
    "SoundCue",
    "load_palette",
    "resolve_sound_events",
    "used_attributions",
]
