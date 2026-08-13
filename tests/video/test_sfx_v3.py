"""効果音 v3: モーション同期の tick レーンと、繋ぎの whoosh。

意味イベント（章の切り替わり・要点・警告…）は従来どおり控えめな上限で運用する。
**tick は別レーン**にする —— ビートが出るたびに鳴る細かい音を同じ上限で数えると、
意味のある音のほうが間引かれてしまう。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.application.video.sound_events import (
    EVENT_TO_CATEGORY,
    MIN_TICK_INTERVAL_SEC,
    TICK_GAIN_DB,
    TICK_MAX_PER_SCENE,
    SoundAsset,
    resolve_sound_events,
)

MANIFEST = Path(__file__).parents[2] / "assets" / "sound-design" / "manifest.json"


def _asset(tmp_path: Path, asset_id: str, category: str) -> SoundAsset:
    path = tmp_path / f"{asset_id}.wav"
    path.write_bytes(b"wave")
    return SoundAsset(
        asset_id, (category,), str(path), "in-house", "", asset_id.ljust(64, "0"), 100
    )


@pytest.fixture
def palette(tmp_path: Path) -> list[SoundAsset]:
    return [
        _asset(tmp_path, "accent", "accent"),
        _asset(tmp_path, "tickA", "tick"),
        _asset(tmp_path, "tickB", "tick"),
        _asset(tmp_path, "whoosh", "whoosh"),
        _asset(tmp_path, "chartsound", "chart"),
    ]


# -- パレット -----------------------------------------------------------------------


def test_manifest_declares_the_new_categories() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    categories = {c for asset in manifest["assets"] for c in asset["categories"]}
    assert {"tick", "whoosh", "chart"} <= categories


def test_manifest_is_v3() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["preset"] == "professional-v3"


def test_every_asset_keeps_its_license_and_hash() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for asset in manifest["assets"]:
        assert asset["license"] and len(asset["sha256"]) == 64, asset["id"]


# -- イベントの割り当て ---------------------------------------------------------------


def test_new_events_map_to_the_new_categories() -> None:
    assert EVENT_TO_CATEGORY["beat_reveal"] == "tick"
    assert EVENT_TO_CATEGORY["scene_change"] == "whoosh"
    assert EVENT_TO_CATEGORY["chart_draw"] == "chart"


# -- tick は別レーン -----------------------------------------------------------------


def test_ticks_do_not_consume_the_semantic_budget(palette) -> None:
    """tick を大量に置いても、意味のある音は間引かれない。"""
    events = [
        {"scene_id": "s01", "event": "key_point", "anchor": "beat-1.reveal"},
        *[
            {"scene_id": "s01", "event": "beat_reveal", "anchor": f"beat-{i}.reveal"}
            for i in range(2, 8)
        ],
    ]
    result = resolve_sound_events(
        events,
        assets=palette,
        offsets={"s01": 0.0},
        durations={"s01": 30.0},
        beat_times={"s01": [1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 13.0]},
        intensity="normal",
    )
    kinds = [cue.event for cue in result.cues]
    assert "key_point" in kinds


def test_ticks_are_capped_per_scene(palette) -> None:
    events = [
        {"scene_id": "s01", "event": "beat_reveal", "anchor": f"beat-{i}.reveal"}
        for i in range(1, 15)
    ]
    result = resolve_sound_events(
        events,
        assets=palette,
        offsets={"s01": 0.0},
        durations={"s01": 60.0},
        beat_times={"s01": [float(i) * 2 for i in range(1, 15)]},
        intensity="normal",
    )
    ticks = [c for c in result.cues if c.event == "beat_reveal"]
    assert len(ticks) <= TICK_MAX_PER_SCENE


def test_ticks_keep_their_own_minimum_interval(palette) -> None:
    events = [
        {"scene_id": "s01", "event": "beat_reveal", "anchor": f"beat-{i}.reveal"}
        for i in range(1, 5)
    ]
    result = resolve_sound_events(
        events,
        assets=palette,
        offsets={"s01": 0.0},
        durations={"s01": 30.0},
        # 0.1 秒間隔で並べる（最小間隔より詰まっている）
        beat_times={"s01": [1.0, 1.1, 1.2, 1.3]},
        intensity="normal",
    )
    times = sorted(c.t_sec for c in result.cues if c.event == "beat_reveal")
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(gap >= MIN_TICK_INTERVAL_SEC - 0.001 for gap in gaps), gaps


def test_ticks_are_quiet(palette) -> None:
    """細かい音が主張すると耳障りになる。意味のある音より確実に小さく。"""
    result = resolve_sound_events(
        [
            {"scene_id": "s01", "event": "key_point", "anchor": "beat-1.reveal"},
            {"scene_id": "s01", "event": "beat_reveal", "anchor": "beat-2.reveal"},
        ],
        assets=palette,
        offsets={"s01": 0.0},
        durations={"s01": 30.0},
        beat_times={"s01": [1.0, 5.0]},
        intensity="normal",
    )
    by_event = {c.event: c.gain_db for c in result.cues}
    assert by_event["beat_reveal"] == TICK_GAIN_DB
    assert by_event["beat_reveal"] < by_event["key_point"]


def test_ticks_are_off_in_subtle_mode(palette) -> None:
    """控えめ設定では、細かい音は鳴らさない。"""
    result = resolve_sound_events(
        [{"scene_id": "s01", "event": "beat_reveal", "anchor": "beat-1.reveal"}],
        assets=palette,
        offsets={"s01": 0.0},
        durations={"s01": 30.0},
        beat_times={"s01": [1.0]},
        intensity="subtle",
    )
    assert not [c for c in result.cues if c.event == "beat_reveal"]


def test_tick_placement_is_deterministic(palette) -> None:
    events = [
        {"scene_id": "s01", "event": "beat_reveal", "anchor": f"beat-{i}.reveal"}
        for i in range(1, 4)
    ]
    kwargs = {
        "assets": palette,
        "offsets": {"s01": 0.0},
        "durations": {"s01": 30.0},
        "beat_times": {"s01": [1.0, 5.0, 9.0]},
        "intensity": "normal",
    }
    first = resolve_sound_events(events, **kwargs)
    second = resolve_sound_events(events, **kwargs)
    assert [(c.t_sec, c.sound_id) for c in first.cues] == [
        (c.t_sec, c.sound_id) for c in second.cues
    ]
