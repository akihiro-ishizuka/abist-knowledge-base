"""目標尺の逆算と章立て台本（水増しをしないことの回帰テスト）。

守る性質:

1. `target_duration_sec` から章数・シーン数・1シーンの目標尺・ナレーション
   目標文字数が決まる
2. 関連情報が足りなければ `INSUFFICIENT_CONTENT_FOR_DURATION` を返して**止まる**
3. 素材を言い換えて増やさない（ナレーションは原文の素材からのみ組む）
4. カード面は固定尺、本編は残りを配分（一律にすると完成尺が目標を割る）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.video.chapter_planner import (
    build_chaptered_draft,
    collect_materials,
    count_materials,
)
from abist_kb.application.video.duration_planner import (
    CARD_TARGET_SEC,
    MAX_SCENE_SEC,
    MIN_SCENE_SEC,
    narration_chars_for,
    plan_duration,
    seconds_for_chars,
)
from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.application.video.script_planner import outline_document, plan_video

_DOC = """# 設計効率化の定例

## 決まったこと

- 編集画面はビューオンリー方針を確認した
- 進む・戻るをパターンごとに50履歴まで保持する
- 抽出から出力までを単一アプリへ統合する

## 次にやること

- VDI 起動遅延への対応を要件化する
- 操作手順の HTML を同梱する
"""


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "notes").mkdir(parents=True)
    for index in range(4):
        (root / "notes" / f"note{index}.md").write_text(
            _DOC.replace("定例", f"定例 {index}"), encoding="utf-8"
        )
    return root


class TestDurationPlan:
    def test_scene_count_follows_the_target(self) -> None:
        short = plan_duration({"min": 120, "max": 180}, available_materials=200)
        long = plan_duration({"min": 300, "max": 600}, available_materials=200)
        assert short.ok and long.ok
        assert long.scene_count > short.scene_count
        assert long.chapter_count >= short.chapter_count

    def test_scene_length_stays_within_bounds(self) -> None:
        for lo, hi in ((60, 120), (120, 180), (300, 600), (600, 900)):
            plan = plan_duration({"min": lo, "max": hi}, available_materials=500)
            assert plan.ok
            assert MIN_SCENE_SEC <= plan.scene_target_sec <= MAX_SCENE_SEC

    def test_role_counts_sum_to_scene_count(self) -> None:
        plan = plan_duration({"min": 120, "max": 180}, available_materials=200)
        assert sum(plan.scenes_by_role.values()) == plan.scene_count

    def test_card_and_body_targets_reconstruct_the_total(self) -> None:
        """カード固定尺 + 本編配分尺 の合計が目標尺に一致すること。

        ここがずれると、完成尺が目標を割る（実測 132 秒目標に対し 103 秒に
        なった不具合の再発防止）。
        """
        plan = plan_duration({"min": 120, "max": 180}, available_materials=200)
        cards = 1 + plan.chapter_count + 1  # 表紙 + 章扉 + エンドカード
        body = sum(
            plan.scenes_by_role.get(role, 0)
            for role in ("explain", "diagram", "comparison", "summary")
        )
        total = cards * plan.card_target_sec + body * plan.body_target_sec
        assert total == pytest.approx(plan.target_sec, abs=0.5)

    def test_narration_budget_is_derived_from_speech_rate(self) -> None:
        plan = plan_duration({"min": 120, "max": 180}, available_materials=200)
        assert plan.narration_chars_total == narration_chars_for(plan.target_sec)
        assert plan.narration_chars_per_body_scene == narration_chars_for(plan.body_target_sec)
        # 逆算が往復すること
        assert seconds_for_chars(plan.narration_chars_total) == pytest.approx(
            plan.target_sec, abs=0.5
        )

    def test_card_target_is_shorter_than_body(self) -> None:
        plan = plan_duration({"min": 120, "max": 180}, available_materials=200)
        assert plan.card_target_sec == CARD_TARGET_SEC
        assert plan.body_target_sec > plan.card_target_sec

    def test_insufficient_content_stops_instead_of_padding(self) -> None:
        """素材が足りないなら**水増しせずに止まる**。"""
        plan = plan_duration({"min": 600, "max": 900}, available_materials=4)
        assert not plan.ok
        assert plan.code == "INSUFFICIENT_CONTENT_FOR_DURATION"
        assert "水増し" in (plan.message or "")
        assert "到達可能" in (plan.message or "")

    def test_thin_content_warns_without_failing(self) -> None:
        plan = plan_duration({"min": 120, "max": 180}, available_materials=12)
        assert plan.ok
        assert plan.warnings

    def test_invalid_target_is_rejected(self) -> None:
        assert plan_duration({"min": 0, "max": 100}).code == "INVALID_VIDEO_SPEC"
        assert plan_duration({"min": 200, "max": 100}).code == "INVALID_VIDEO_SPEC"


class TestChapteredDraft:
    def test_materials_carry_line_numbers(self, docs: Path) -> None:
        outline = outline_document(docs, _stub_input("notes/note0.md"))
        materials = collect_materials(outline)
        assert materials
        assert all(m.line >= 1 for m in materials)
        # 見出しと箇条書きの両方が取れている
        assert any(m.is_heading for m in materials)
        assert any(not m.is_heading for m in materials)

    def test_draft_follows_the_planned_scene_count(self, docs: Path) -> None:
        outlines = [outline_document(docs, _stub_input(f"notes/note{i}.md")) for i in range(4)]
        plan = plan_duration(
            {"min": 120, "max": 180}, available_materials=count_materials(outlines)
        )
        assert plan.ok
        draft = build_chaptered_draft(outlines, plan, title="テスト動画", purpose="確認用")
        assert len(draft["scenes"]) == plan.scene_count

    def test_narration_uses_only_source_material(self, docs: Path) -> None:
        """ナレーションは素材の文からしか作らない（創作した文を混ぜない）。

        表紙・章扉・エンドカードの定型文は除き、本編の文が原文に無い、
        ということが起きていないかを見る。
        """
        outlines = [outline_document(docs, _stub_input(f"notes/note{i}.md")) for i in range(4)]
        plan = plan_duration(
            {"min": 120, "max": 180}, available_materials=count_materials(outlines)
        )
        draft = build_chaptered_draft(outlines, plan, title="テスト動画")
        corpus = "".join((docs / f"notes/note{i}.md").read_text(encoding="utf-8") for i in range(4))
        for scene in draft["scenes"]:
            if scene["role"] not in ("body", "diagram", "quote"):
                continue
            for sentence in (scene["narration"]["text"] or "").split("。"):
                if sentence:
                    assert sentence in corpus, f"原文に無い文がナレーションに入った: {sentence}"

    def test_chapter_cards_carry_index_and_total(self, docs: Path) -> None:
        outlines = [outline_document(docs, _stub_input(f"notes/note{i}.md")) for i in range(4)]
        plan = plan_duration(
            {"min": 120, "max": 180}, available_materials=count_materials(outlines)
        )
        draft = build_chaptered_draft(outlines, plan, title="テスト動画")
        chapters = [s for s in draft["scenes"] if s["role"] == "chapter"]
        assert chapters
        for index, scene in enumerate(chapters, start=1):
            assert scene["diagram"]["chapter_index"] == index
            assert scene["diagram"]["chapter_total"] == len(chapters)

    def test_draft_starts_with_a_cover_and_ends_with_an_ending_card(self, docs: Path) -> None:
        outlines = [outline_document(docs, _stub_input(f"notes/note{i}.md")) for i in range(4)]
        plan = plan_duration(
            {"min": 120, "max": 180}, available_materials=count_materials(outlines)
        )
        draft = build_chaptered_draft(outlines, plan, title="テスト動画")
        assert draft["scenes"][0]["diagram"]["kind"] == "title"
        assert draft["scenes"][-1]["diagram"]["kind"] == "ending"

    def test_deterministic(self, docs: Path) -> None:
        outlines = [outline_document(docs, _stub_input(f"notes/note{i}.md")) for i in range(4)]
        plan = plan_duration(
            {"min": 120, "max": 180}, available_materials=count_materials(outlines)
        )
        first = build_chaptered_draft(outlines, plan, title="テスト動画")
        second = build_chaptered_draft(outlines, plan, title="テスト動画")
        assert first == second


class TestPlanVideoWithDuration:
    def test_chaptered_scenes_are_validated_scene_specs(self, docs: Path) -> None:
        resolved = resolve_inputs({"kb_directories": ["notes"]}, docs_dir=docs)
        result, _draft = plan_video(
            resolved.inputs,
            docs_dir=docs,
            title="テスト動画",
            target_duration_sec={"min": 120, "max": 180},
        )
        assert result.ok, result.errors
        kinds = {s["kind"] for s in result.scenes}
        assert "title" in kinds and "chapter" in kinds and "ending" in kinds
        assert result.duration_plan is not None
        assert result.duration_plan["ok"] is True

    def test_insufficient_content_surfaces_the_code(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "tiny.md").write_text("# 見出し\n\n- 一点だけ\n", encoding="utf-8")
        resolved = resolve_inputs({"kb_paths": ["tiny.md"]}, docs_dir=docs)
        result, draft = plan_video(
            resolved.inputs,
            docs_dir=docs,
            title="長すぎる目標",
            target_duration_sec={"min": 600, "max": 900},
        )
        assert not result.ok
        assert result.errors[0]["code"] == "INSUFFICIENT_CONTENT_FOR_DURATION"
        assert draft is None, "止めると言いながら台本を作ってしまっている"

    def test_without_duration_the_legacy_path_still_works(self, docs: Path) -> None:
        """`target_duration_sec` を渡さない既存の呼び出しが壊れないこと。"""
        resolved = resolve_inputs({"kb_directories": ["notes"]}, docs_dir=docs)
        result, _draft = plan_video(resolved.inputs, docs_dir=docs, title="従来経路")
        assert result.ok
        assert result.duration_plan is None


def _stub_input(path: str):
    from abist_kb.application.video.input_resolver import ResolvedInput

    return ResolvedInput(
        path=path,
        content_hash=None,
        selection="collection_candidate",
        require_usage=False,
        origin={"type": "kb_directory", "selector": "notes"},
    )
