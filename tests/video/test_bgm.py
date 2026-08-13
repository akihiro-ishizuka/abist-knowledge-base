"""BGM（社内制作のループ音源）。

効果音と同じ扱いにする: **ライセンスと sha256 が無い音源は使わない**。
外部素材を混ぜず、生成器から決定的に作った WAV だけをコミットする。

BGM は「敷く」もので「聴かせる」ものではない。効果音より確実に小さい床として
鳴らし、先頭と末尾はフェードで出入りする。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from abist_kb.application.video.bgm import (
    BGM_GAIN_DB,
    FADE_IN_SEC,
    FADE_OUT_SEC,
    MOODS,
    load_bgm,
    plan_bgm,
    select_mood,
)
from abist_kb.application.video.sound_events import DEFAULT_GAIN_DB

MANIFEST = Path(__file__).parents[2] / "assets" / "sound-design" / "manifest.json"


# -- マニフェスト --------------------------------------------------------------------


def test_manifest_declares_every_mood() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    moods = {entry["mood"] for entry in manifest.get("bgm") or []}
    assert set(MOODS) <= moods


def test_every_loop_has_a_license_and_hash() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for entry in manifest["bgm"]:
        assert entry["license"], entry["id"]
        assert len(entry["sha256"]) == 64, entry["id"]
        assert entry["attribution"], entry["id"]


def test_loop_files_match_their_recorded_hash() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    root = MANIFEST.parent
    for entry in manifest["bgm"]:
        payload = (root / entry["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"], entry["id"]


# -- 読み込み -----------------------------------------------------------------------


def test_load_returns_one_asset_per_mood() -> None:
    assets, warnings = load_bgm(MANIFEST)
    assert warnings == []
    assert {a.mood for a in assets} >= set(MOODS)


def test_tampered_loop_is_rejected(tmp_path: Path) -> None:
    """SHA が合わない音源は使わない（効果音と同じ扱い）。"""
    loop = tmp_path / "bgm" / "x.wav"
    loop.parent.mkdir(parents=True)
    loop.write_bytes(b"tampered")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "assets": [],
                "bgm": [
                    {
                        "id": "x",
                        "mood": "neutral",
                        "path": "bgm/x.wav",
                        "license": "in-house",
                        "attribution": "test",
                        "sha256": "0" * 64,
                        "duration_ms": 1000,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assets, warnings = load_bgm(manifest)
    assert assets == []
    assert warnings


def test_missing_license_is_rejected(tmp_path: Path) -> None:
    loop = tmp_path / "bgm" / "x.wav"
    loop.parent.mkdir(parents=True)
    loop.write_bytes(b"data")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "assets": [],
                "bgm": [
                    {
                        "id": "x",
                        "mood": "neutral",
                        "path": "bgm/x.wav",
                        "license": "",
                        "sha256": hashlib.sha256(b"data").hexdigest(),
                        "duration_ms": 1000,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assets, _warnings = load_bgm(manifest)
    assert assets == []


# -- ムード選択 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "purpose,expected",
    [
        ("2026年の活動報告", "neutral"),
        ("CATIA の操作手順を説明する", "upbeat"),
        ("新人向けの研修教材", "calm"),
        ("", "neutral"),
    ],
)
def test_mood_is_chosen_from_the_purpose(purpose: str, expected: str) -> None:
    assert select_mood(purpose) == expected


def test_explicit_mood_wins() -> None:
    assert select_mood("研修", override="upbeat") == "upbeat"


def test_off_disables_bgm() -> None:
    assert select_mood("研修", override="off") is None


def test_selection_is_deterministic() -> None:
    assert select_mood("進捗レポート") == select_mood("進捗レポート")


# -- 尺の計画 -----------------------------------------------------------------------


def test_plan_covers_the_whole_video() -> None:
    assets, _ = load_bgm(MANIFEST)
    asset = next(a for a in assets if a.mood == "neutral")
    plan = plan_bgm(180.0, asset)
    assert plan.total_sec == 180.0
    assert plan.loops * (asset.duration_ms / 1000) >= 180.0


def test_plan_fades_in_and_out() -> None:
    assets, _ = load_bgm(MANIFEST)
    plan = plan_bgm(180.0, assets[0])
    assert plan.fade_in_sec == FADE_IN_SEC
    assert plan.fade_out_sec == FADE_OUT_SEC
    assert plan.fade_out_start_sec == pytest.approx(180.0 - FADE_OUT_SEC)


def test_bed_sits_well_under_the_sound_effects() -> None:
    """BGM が効果音を覆うと、意味のある音が伝わらない。"""
    assert BGM_GAIN_DB < DEFAULT_GAIN_DB - 5


def test_short_video_still_gets_a_usable_fade() -> None:
    assets, _ = load_bgm(MANIFEST)
    plan = plan_bgm(3.0, assets[0])
    assert plan.fade_out_start_sec >= 0.0
    assert plan.fade_in_sec + plan.fade_out_sec <= 3.0
