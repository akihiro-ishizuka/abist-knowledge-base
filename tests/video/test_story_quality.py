"""動画へ出す文言の品質と、ゴールデン台本の回帰。

かつて `activity_story.py` / `fiscal_year_story.py` に台本そのものが Python の
文字列リテラルとしてハードコードされ、タイトルの部分一致で選ばれていた。台本は
エージェントが書くものになったので、その2本は
`tests/video/fixtures/golden/*.json` へ移し、**検証チェーンを通る台本の見本**
かつ回帰の基準として残す。
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from abist_kb.application.video.contact_sheet import build_contact_sheet
from abist_kb.application.video.content_quality import (
    clean_display_text,
    is_forbidden_heading,
    validate_story_content,
)
from abist_kb.application.video.input_resolver import ResolvedInput
from abist_kb.application.video.script_planner import (
    author_script,
    build_scenes,
    outline_document,
)
from abist_kb.application.video.sound_events import SoundAsset, resolve_sound_events
from abist_kb.domain.script_draft import validate_script_draft

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
GOLDEN_NAMES = ("activity_story", "fiscal_year_story")


def _load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _write_docs(docs_dir: Path, paths: list[str], *, lines: int = 1000) -> list[ResolvedInput]:
    """出典の行範囲を解決できるだけの実体を持つ題材を用意する。"""
    resolved: list[ResolvedInput] = []
    for path in paths:
        target = docs_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(
            f"# 見出し{n}" if n % 40 == 1 else f"- 項目{n}の内容です" for n in range(1, lines + 1)
        )
        target.write_text(body, encoding="utf-8")
        resolved.append(
            ResolvedInput(
                path=path,
                selection="explicit_primary",
                require_usage=True,
                origin={"type": "kb_path"},
                content_hash="a" * 64,
            )
        )
    return resolved


# -- 表示文の正規化 --------------------------------------------------------------


def test_display_text_removes_markdown_html_and_checkbox() -> None:
    assert clean_display_text('- [ ] **参照** <a id="x"></a>') == "参照"
    assert clean_display_text("[工数](https://example.invalid)を確認") == "工数を確認"


def test_forbidden_heading_variants_are_rejected() -> None:
    assert is_forbidden_heading("## 5. 参照先（esa階層）")
    assert is_forbidden_heading("会議概要")


def test_semantic_gate_rejects_bad_heading_and_markup() -> None:
    scenes = [
        {
            "id": "s01",
            "title": "設計効率化 2026年活動まとめ",
            "narration": {"text": "本文です。"},
            "scene_spec": {"beats": [], "sources": []},
        },
        {
            "id": "s02",
            "kind": "timeline",
            "title": "凡例",
            "on_screen_text": ['[ ] **作業** <a id="x"></a>'],
            "scene_spec": {"beats": [], "sources": []},
        },
    ]
    codes = {error["code"] for error in validate_story_content(scenes, [])}
    assert "FORBIDDEN_VIDEO_HEADING" in codes
    assert "MARKUP_IN_VIDEO_TEXT" in codes


# -- ゴールデン台本の回帰 ---------------------------------------------------------


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_script_passes_the_draft_validator(name: str) -> None:
    golden = _load_golden(name)
    result = validate_script_draft(golden["script"], source_ids={"s1"})
    assert result.ok, [error.to_dict() for error in result.errors]


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_script_satisfies_its_own_declared_requirements(name: str, tmp_path: Path) -> None:
    """自己申告した story_requirements を、その台本自身が満たしている。"""
    golden = _load_golden(name)
    docs_dir = tmp_path / "docs"
    resolved = _write_docs(docs_dir, golden["inputs"]["kb_paths"])
    outlines = [o for o in (outline_document(docs_dir, item) for item in resolved) if o]

    validated = validate_script_draft(golden["script"], source_ids={"s1"})
    plan = build_scenes(validated, outlines, docs_dir=docs_dir)
    assert plan.ok, plan.errors

    errors = validate_story_content(
        plan.scenes, plan.sound_events, requirements=golden["story_requirements"]
    )
    assert errors == []


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_script_keeps_its_scene_and_sound_event_count(name: str) -> None:
    golden = _load_golden(name)
    span = golden["story_requirements"]["scene_count"]
    events = golden["story_requirements"]["sound_events"]
    assert span["min"] <= len(golden["script"]["scenes"]) <= span["max"]
    assert events["min"] <= len(golden["script"]["sound_events"]) <= events["max"]


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_golden_script_carries_no_markup_residue(name: str) -> None:
    serialized = json.dumps(_load_golden(name)["script"], ensure_ascii=False)
    assert "<a " not in serialized
    assert "**" not in serialized
    assert "[ ]" not in serialized


# -- タイトルによる暗黙のディスパッチが無いこと ------------------------------------


def test_the_title_no_longer_selects_a_baked_in_script(tmp_path: Path) -> None:
    """「設計効率化 2026」というタイトルに反応する専用経路が無いこと。

    以前は `is_target_story()` がタイトル部分一致でハードコード台本を返していた。
    今は台本を渡さなければ何も作られない（機械生成する経路そのものが無い）。
    """
    import abist_kb.application.video.script_planner as planner

    assert not hasattr(planner, "plan_video"), "台本を機械生成する経路が残っている"
    assert not hasattr(planner, "plan_draft_from_documents")
    assert not (pathlib.Path("src/abist_kb/application/video/chapter_planner.py").exists()), (
        "章立ての自動組み立てが残っている"
    )


def test_declared_requirements_stop_the_render(tmp_path: Path) -> None:
    """宣言された要件を満たせない台本は、レンダリング前に止まる。"""
    golden = _load_golden("activity_story")
    docs_dir = tmp_path / "docs"
    resolved = _write_docs(docs_dir, golden["inputs"]["kb_paths"])

    result = author_script(
        golden["script"],
        resolved,
        docs_dir=docs_dir,
        story_requirements={"required_topics": ["決して現れない語句"]},
    )

    assert not result.ok
    assert any(e["code"] == "MISSING_STORY_TOPIC" for e in result.errors)


# -- 音まわり（最終ミックスは tests/video/test_silent_pipeline.py が見る） ------------


def test_sound_interval_is_enforced_across_scene_boundaries(tmp_path: Path) -> None:
    complete = tmp_path / "complete.wav"
    transition = tmp_path / "transition.wav"
    complete.write_bytes(b"wave")
    transition.write_bytes(b"wave")
    assets = [
        SoundAsset("complete", ("complete",), str(complete), "in-house", "", "a" * 64, 100),
        SoundAsset("transition", ("transition",), str(transition), "in-house", "", "b" * 64, 100),
    ]
    result = resolve_sound_events(
        [
            {"scene_id": "s01", "event": "success", "anchor": "scene.end"},
            {"scene_id": "s02", "event": "chapter_change", "anchor": "scene.start"},
        ],
        assets=assets,
        offsets={"s01": 0.0, "s02": 10.0},
        durations={"s01": 10.0, "s02": 10.0},
        intensity="normal",
    )
    assert len(result.cues) == 1
    assert result.dropped == [
        {"scene_id": "s02", "event": "chapter_change", "reason": "min_interval"}
    ]


def test_sound_tail_stays_inside_scene(tmp_path: Path) -> None:
    ending = tmp_path / "ending.wav"
    ending.write_bytes(b"wave")
    asset = SoundAsset("ending", ("ending",), str(ending), "in-house", "", "a" * 64, 1650)
    result = resolve_sound_events(
        [{"scene_id": "s01", "event": "outro", "anchor": "scene.end"}],
        assets=[asset],
        offsets={"s01": 0.0},
        durations={"s01": 10.0},
        intensity="normal",
    )
    assert result.cues[0].t_sec == 8.35


def test_contact_sheet_contains_one_frame_per_scene(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    def fake_extract(_video, _at, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"png")
        return True

    def fake_run(args, **_kwargs):
        Path(args[-1]).write_bytes(b"sheet")
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr("abist_kb.application.video.contact_sheet.extract_frame", fake_extract)
    monkeypatch.setattr("abist_kb.application.video.contact_sheet.run_ffmpeg", fake_run)
    result = build_contact_sheet(
        video,
        tmp_path,
        scene_ids=["s01", "s02", "s03"],
        offsets={"s01": 0.0, "s02": 10.0, "s03": 20.0},
        durations={"s01": 10.0, "s02": 10.0, "s03": 20.0},
    )
    assert result.ok and result.frame_count == 3
    assert result.path == tmp_path / "preview" / "contact-sheet.png"
