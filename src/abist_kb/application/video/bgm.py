"""BGM（社内制作のループ音源）の選択と配置。

効果音（`sound_events`）と同じ原則で扱う:

- **利用者もエージェントも音源ファイルを指定しない。** 用途（purpose）から
  ムードを決定論的に選ぶ
- **ライセンスと SHA-256 が無い音源は使わない。** 帰属を成果物へ転記できない
  素材は動画に載せない

BGM は「敷く」もので「聴かせる」ものではない。効果音より十分に低い床として鳴らし、
先頭と末尾はフェードで出入りする。サイドチェインでのダッキングは入れていない
（-26dB の床は効果音の -10〜-16dB より十分下で、実用上ぶつからない）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

#: 用意しているムード。
MOODS: tuple[str, ...] = ("neutral", "upbeat", "calm")
DEFAULT_MOOD = "neutral"

#: BGM の音量（dB）。効果音（既定 -16dB）より十分に低い床にする。
BGM_GAIN_DB = -26.0
#: 冒頭のフェードイン・末尾のフェードアウト（秒）。
FADE_IN_SEC = 1.5
FADE_OUT_SEC = 2.5

#: 用途の語からムードを決める（先に一致したものを採る。順序に意味がある）。
_MOOD_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("calm", ("研修", "教材", "教育", "学習", "オンボーディング")),
    ("upbeat", ("手順", "チュートリアル", "操作", "使い方", "セットアップ", "導入方法")),
    ("neutral", ("報告", "進捗", "レポート", "活動", "まとめ", "定例")),
)


@dataclass(frozen=True, slots=True)
class BgmAsset:
    id: str
    mood: str
    path: str
    license: str
    attribution: str
    sha256: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class BgmPlan:
    """1本の動画へどう敷くか。`audio/bgm.json` に残して再現可能にする。"""

    asset: BgmAsset
    total_sec: float
    loops: int
    gain_db: float
    fade_in_sec: float
    fade_out_sec: float
    fade_out_start_sec: float

    def to_dict(self) -> dict:
        return {
            "sound_id": self.asset.id,
            "mood": self.asset.mood,
            "license": self.asset.license,
            "attribution": self.asset.attribution,
            "sha256": self.asset.sha256,
            "total_sec": self.total_sec,
            "loops": self.loops,
            "gain_db": self.gain_db,
            "fade_in_sec": self.fade_in_sec,
            "fade_out_sec": self.fade_out_sec,
            "fade_out_start_sec": self.fade_out_start_sec,
        }


def load_bgm(manifest_path: Path) -> tuple[list[BgmAsset], list[str]]:
    """承認済みの BGM を読む（`load_palette` と同じ検査）。"""
    warnings: list[str] = []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], [f"BGM パレットを読めません: {manifest_path}"]

    assets: list[BgmAsset] = []
    base = manifest_path.parent
    for entry in payload.get("bgm") or []:
        required = ("id", "mood", "path", "license", "sha256")
        if not all(entry.get(key) for key in required):
            warnings.append(f"必須項目が欠けた BGM を除外しました: {entry.get('id')}")
            continue
        file_path = base / entry["path"]
        if not file_path.is_file():
            warnings.append(f"BGM が見つかりません: {entry['path']}")
            continue
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            warnings.append(f"BGM の SHA-256 が一致しません: {entry['id']}")
            continue
        assets.append(
            BgmAsset(
                id=entry["id"],
                mood=str(entry["mood"]),
                path=str(file_path),
                license=entry["license"],
                attribution=entry.get("attribution", ""),
                sha256=entry["sha256"],
                duration_ms=int(entry.get("duration_ms") or 0),
            )
        )
    return assets, warnings


def select_mood(purpose: str | None, *, override: str | None = None) -> str | None:
    """用途からムードを決める（`off` なら None＝BGM 無し）。

    決定論的なキーワード一致。曖昧なら既定（neutral）へ落とす。
    """
    if override == "off":
        return None
    if override in MOODS:
        return override
    text = str(purpose or "")
    for mood, keywords in _MOOD_KEYWORDS:
        if any(word in text for word in keywords):
            return mood
    return DEFAULT_MOOD


def pick_asset(assets: list[BgmAsset], mood: str) -> BgmAsset | None:
    """ムードから1本選ぶ（同一ムードが複数あれば id 順で先頭＝決定的）。"""
    candidates = sorted([a for a in assets if a.mood == mood], key=lambda a: a.id)
    return candidates[0] if candidates else None


def plan_bgm(total_sec: float, asset: BgmAsset) -> BgmPlan:
    """動画全体へ敷く計画を立てる。"""
    loop_sec = max(0.001, asset.duration_ms / 1000)
    loops = max(1, int(total_sec / loop_sec) + 1)
    # 短い動画でフェードが重ならないよう、尺に収まる範囲へ切り詰める。
    fade_in = min(FADE_IN_SEC, max(0.0, total_sec * 0.3))
    fade_out = min(FADE_OUT_SEC, max(0.0, total_sec * 0.3))
    return BgmPlan(
        asset=asset,
        total_sec=round(total_sec, 3),
        loops=loops,
        gain_db=BGM_GAIN_DB,
        fade_in_sec=round(fade_in, 3),
        fade_out_sec=round(fade_out, 3),
        fade_out_start_sec=round(max(0.0, total_sec - fade_out), 3),
    )


__all__ = [
    "BGM_GAIN_DB",
    "DEFAULT_MOOD",
    "FADE_IN_SEC",
    "FADE_OUT_SEC",
    "MOODS",
    "BgmAsset",
    "BgmPlan",
    "load_bgm",
    "pick_asset",
    "plan_bgm",
    "select_mood",
]
