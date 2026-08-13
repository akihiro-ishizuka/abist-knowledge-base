"""縦型（9:16 / Shorts）の第一級対応。

縦型はフレーム幅が 4.5 単位しかない（横型は 14.222）。横型と同じ密度で組むと
`fit_to_frame` が図全体を等比縮小し、文字が読めない絵ができあがる。**描いてから
気付くのではなく、検証で止める。**
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from abist_kb.application.video.contact_sheet import build_contact_sheet
from abist_kb.domain.scene_spec import MAX_PORTRAIT_ELEMENTS, validate_scene_spec


def _source() -> dict[str, Any]:
    return {
        "id": "s1",
        "path": "a/b.md",
        "start_line": 1,
        "end_line": 5,
        "content_hash": "0" * 64,
    }


def _flow_spec(step_count: int, *, aspect: str) -> dict[str, Any]:
    steps = [
        {"type": "flow_step", "label": f"手順{i}", "source_refs": ["s1"]}
        for i in range(1, step_count + 1)
    ]
    transitions = [
        {"type": "transition", "from": f"手順{i}", "to": f"手順{i + 1}"}
        for i in range(1, step_count)
    ]
    return {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "mp4",
        "template": "data_flow_v1",
        "title": "手順",
        "frame": {"aspect_ratio": aspect},
        "sources": [_source()],
        "beats": [*steps, *transitions],
    }


# -- 密度の上限 ---------------------------------------------------------------------


def test_portrait_rejects_a_dense_diagram() -> None:
    result = validate_scene_spec(_flow_spec(MAX_PORTRAIT_ELEMENTS + 2, aspect="9:16"))
    assert not result.ok
    assert any(e.code == "PORTRAIT_TOO_DENSE" for e in result.errors)


def test_the_error_says_how_to_fix_it() -> None:
    result = validate_scene_spec(_flow_spec(MAX_PORTRAIT_ELEMENTS + 2, aspect="9:16"))
    message = next(e.message for e in result.errors if e.code == "PORTRAIT_TOO_DENSE")
    assert "分け" in message, "「シーンを分ける」という直し方が書かれていること"


def test_portrait_accepts_a_sparse_diagram() -> None:
    assert validate_scene_spec(_flow_spec(MAX_PORTRAIT_ELEMENTS, aspect="9:16")).ok


def test_landscape_keeps_its_own_limits() -> None:
    """横型は従来どおり。縦型の制約を持ち込まない。"""
    assert validate_scene_spec(_flow_spec(MAX_PORTRAIT_ELEMENTS + 2, aspect="16:9")).ok


def test_cards_are_not_subject_to_the_diagram_limit() -> None:
    """箇条書きカードは縦に積むだけなので、図と同じ上限は要らない。"""
    spec = {
        "schema_version": "1.0",
        "scene_kind": "key_points",
        "output_format": "mp4",
        "template": "key_points",
        "title": "要点",
        "frame": {"aspect_ratio": "9:16"},
        "sources": [_source()],
        "beats": [
            {"type": "statement", "text": f"要点{i}", "source_refs": ["s1"]} for i in range(6)
        ],
    }
    assert validate_scene_spec(spec).ok


@pytest.mark.parametrize(
    "kind,template,beat",
    [
        ("timeline", "timeline_v1", {"type": "timeline_point", "at": "4月", "label": "着手"}),
        ("domain", "domain_map_v1", {"type": "domain_entity", "name": "要素"}),
    ],
)
def test_every_node_like_diagram_is_capped(kind: str, template: str, beat: dict) -> None:
    beats = [{**beat, "source_refs": ["s1"]} for _ in range(MAX_PORTRAIT_ELEMENTS + 2)]
    if kind == "domain":
        beats = [
            {**beat, "name": f"要素{i}", "source_refs": ["s1"]}
            for i in range(MAX_PORTRAIT_ELEMENTS + 2)
        ]
    spec = {
        "schema_version": "1.0",
        "scene_kind": kind,
        "output_format": "mp4",
        "template": template,
        "title": "図",
        "frame": {"aspect_ratio": "9:16"},
        "sources": [_source()],
        "beats": beats,
    }
    result = validate_scene_spec(spec)
    assert not result.ok
    assert any(e.code == "PORTRAIT_TOO_DENSE" for e in result.errors)


# -- コンタクトシート -----------------------------------------------------------------


def test_portrait_contact_sheet_uses_portrait_tiles(tmp_path: Path, monkeypatch) -> None:
    """縦型動画のコンタクトシートを横長タイルで作ると、全部潰れて確認にならない。"""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    captured: list[str] = []

    def fake_extract(_video, _at, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"png")
        return True

    def fake_run(args, **_kwargs):
        captured.extend(args)
        Path(args[-1]).write_bytes(b"sheet")
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr("abist_kb.application.video.contact_sheet.extract_frame", fake_extract)
    monkeypatch.setattr("abist_kb.application.video.contact_sheet.run_ffmpeg", fake_run)

    result = build_contact_sheet(
        video,
        tmp_path,
        scene_ids=["s01", "s02"],
        offsets={"s01": 0.0, "s02": 10.0},
        durations={"s01": 10.0, "s02": 10.0},
        aspect_ratio="9:16",
    )

    assert result.ok
    command = " ".join(captured)
    assert "scale=270:480" in command, f"縦長タイルになっていない: {command}"
    assert "scale=480:270" not in command
