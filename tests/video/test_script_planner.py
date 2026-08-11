"""Phase 3: 台本・絵コンテ・SceneSpec 生成。

**LLM 出力を直接レンダリングしない**ことと、**出典に結び付かない主張を落とす**
ことの回帰テスト。LLM は固定モックで検証する（外部設定はブロッカーにしない）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.application.video.script_planner import (
    outline_document,
    parse_llm_draft,
    plan_video,
)
from abist_kb.domain.line_range import range_hash
from abist_kb.domain.scene_spec import validate_scene_spec
from abist_kb.domain.script_draft import validate_script_draft


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "manuals").mkdir(parents=True)
    (root / "manuals" / "guide.md").write_text(
        "# 登録手順\n\n## 準備\n\n- ログインする\n- 画面を開く\n- 登録する\n\n## 注意\n\n本文\n",
        encoding="utf-8",
    )
    (root / "manuals" / "design.md").write_text(
        "# 設計方針\n\n## 構成\n\n本文\n\n## 制約\n\n本文\n", encoding="utf-8"
    )
    return root


def _resolved(docs: Path, **inputs):
    result = resolve_inputs(inputs, docs_dir=docs)
    assert result.ok
    return result.inputs


class TestDraftValidation:
    def test_fact_without_source_is_dropped(self) -> None:
        """出典の無い事実は落とす（創作を通さない）。"""
        draft = {
            "title": "t",
            "scenes": [
                {
                    "id": "s01",
                    "role": "body",
                    "claims": [
                        {"text": "出典あり", "kind": "fact", "source_refs": ["s1"]},
                        {"text": "出典なし", "kind": "fact"},
                        {"text": "意見", "kind": "opinion"},
                    ],
                }
            ],
        }
        result = validate_script_draft(draft, source_ids={"s1"})
        assert result.ok
        texts = [c["text"] for c in result.draft["scenes"][0]["claims"]]
        assert "出典あり" in texts
        assert "出典なし" not in texts, "出典の無い事実が残っている"
        assert "意見" in texts, "opinion は出典不要"

    def test_unknown_source_ref_is_an_error(self) -> None:
        draft = {
            "title": "t",
            "scenes": [
                {
                    "id": "s01",
                    "role": "body",
                    "claims": [{"text": "x", "kind": "fact", "source_refs": ["zzz"]}],
                }
            ],
        }
        result = validate_script_draft(draft, source_ids={"s1"})
        assert not result.ok
        assert any(e.code == "unknown_source_ref" for e in result.errors)

    @pytest.mark.parametrize("key", ["sound", "sound_id", "path", "file"])
    def test_llm_cannot_choose_sound_files(self, key: str) -> None:
        """音源名を LLM に選ばせない（Phase 6 と二重防御）。"""
        draft = {
            "title": "t",
            "scenes": [{"id": "s01", "role": "body"}],
            "sound_events": [{"scene_id": "s01", "event": "key_point", key: "accent.wav"}],
        }
        result = validate_script_draft(draft, source_ids={"s1"})
        assert not result.ok
        assert any(e.code == "INVALID_SOUND_EVENT" for e in result.errors)

    def test_unknown_event_is_rejected(self) -> None:
        draft = {
            "title": "t",
            "scenes": [{"id": "s01", "role": "body"}],
            "sound_events": [{"scene_id": "s01", "event": "explosion"}],
        }
        result = validate_script_draft(draft, source_ids=set())
        assert not result.ok
        assert any(e.code == "INVALID_SOUND_EVENT" for e in result.errors)

    def test_bad_anchor_is_rejected(self) -> None:
        draft = {
            "title": "t",
            "scenes": [{"id": "s01", "role": "body"}],
            "sound_events": [{"scene_id": "s01", "event": "key_point", "anchor": "どこか"}],
        }
        assert not validate_script_draft(draft, source_ids=set()).ok

    @pytest.mark.parametrize(
        "anchor", ["scene.start", "scene.end", "chapter.enter", "beat-3.reveal"]
    )
    def test_valid_anchors(self, anchor: str) -> None:
        draft = {
            "title": "t",
            "scenes": [{"id": "s01", "role": "body"}],
            "sound_events": [{"scene_id": "s01", "event": "key_point", "anchor": anchor}],
        }
        assert validate_script_draft(draft, source_ids=set()).ok

    def test_long_on_screen_text_is_dropped_with_warning(self) -> None:
        draft = {
            "title": "t",
            "scenes": [{"id": "s01", "role": "body", "on_screen_text": ["あ" * 100, "短い"]}],
        }
        result = validate_script_draft(draft, source_ids=set())
        assert result.ok
        assert result.draft["scenes"][0]["on_screen_text"] == ["短い"]
        assert result.warnings


class TestRuleBasedPlanning:
    def test_generates_chaptered_scenes_without_llm(self, docs: Path) -> None:
        """LLM 無しでも章立てできる（外部設定はブロッカーにしない）。"""
        plan, draft = plan_video(
            _resolved(docs, kb_paths=["manuals/guide.md"]), docs_dir=docs, title="操作説明"
        )
        assert plan.ok, plan.errors
        roles = [s["role"] for s in plan.scenes]
        assert "intro" in roles
        assert "summary" in roles
        assert any(s["kind"] == "flow" for s in plan.scenes), "手順から flow を作る"
        assert draft is not None

    def test_scene_specs_are_validated(self, docs: Path) -> None:
        plan, _ = plan_video(
            _resolved(docs, kb_paths=["manuals/guide.md"]), docs_dir=docs, title="t"
        )
        for scene in plan.scenes:
            assert validate_scene_spec(scene["scene_spec"]).ok, scene["id"]

    def test_sources_carry_real_content_hash(self, docs: Path) -> None:
        """出典の content_hash は実ファイルから計算される。"""
        plan, _ = plan_video(
            _resolved(docs, kb_paths=["manuals/guide.md"]), docs_dir=docs, title="t"
        )
        with_sources = [s for s in plan.scenes if s["scene_spec"]["sources"]]
        assert with_sources
        source = with_sources[0]["scene_spec"]["sources"][0]
        text = (docs / source["path"]).read_text(encoding="utf-8")
        expected = range_hash(text, source["start_line"], source["end_line"]).hash
        assert source["content_hash"] == expected

    def test_used_paths_are_tracked(self, docs: Path) -> None:
        plan, _ = plan_video(
            _resolved(docs, kb_paths=["manuals/guide.md", "manuals/design.md"]),
            docs_dir=docs,
            title="t",
        )
        assert plan.used_paths == {"manuals/guide.md", "manuals/design.md"}

    def test_primary_inputs_come_first(self, docs: Path) -> None:
        """明示主入力が本編の骨格になる（検索補完に埋もれない）。"""
        resolved = resolve_inputs(
            {"kb_paths": ["manuals/design.md"], "kb_directories": ["manuals"]}, docs_dir=docs
        )
        plan, _ = plan_video(resolved.inputs, docs_dir=docs, title="t")
        assert plan.ok
        assert "manuals/design.md" in plan.used_paths


class TestLlmPath:
    def test_mock_llm_draft_is_used(self, docs: Path) -> None:
        """固定モックの LLM 出力が採用されること。"""

        def fake_chat(*, outlines, title, purpose):
            return json.dumps(
                {
                    "title": "モック台本",
                    "scenes": [
                        {"id": "s01", "role": "intro", "title": "はじめに"},
                        {
                            "id": "s02",
                            "role": "body",
                            "title": "本編",
                            "claims": [
                                {"text": "登録は3手順です", "kind": "fact", "source_refs": ["s1"]}
                            ],
                            "diagram": {
                                "kind": "explain",
                                "source": {"path": outlines[0].path, "start": 1, "end": 5},
                            },
                        },
                    ],
                    "sound_events": [{"scene_id": "s02", "event": "key_point"}],
                },
                ensure_ascii=False,
            )

        plan, draft = plan_video(
            _resolved(docs, kb_paths=["manuals/guide.md"]),
            docs_dir=docs,
            title="t",
            chat_fn=fake_chat,
        )
        assert plan.ok, plan.errors
        assert draft["title"] == "モック台本"
        assert "登録は3手順です" in json.dumps(plan.scenes, ensure_ascii=False)

    def test_llm_failure_falls_back_to_rules(self, docs: Path) -> None:
        """LLM が使えなくても動画は作れる。"""

        def broken(*, outlines, title, purpose):
            raise RuntimeError("provider 未設定")

        plan, _ = plan_video(
            _resolved(docs, kb_paths=["manuals/guide.md"]),
            docs_dir=docs,
            title="t",
            chat_fn=broken,
        )
        assert plan.ok
        assert any("ルールベース" in w for w in plan.warnings)

    def test_malformed_llm_json_falls_back(self, docs: Path) -> None:
        plan, _ = plan_video(
            _resolved(docs, kb_paths=["manuals/guide.md"]),
            docs_dir=docs,
            title="t",
            chat_fn=lambda **_: "これは JSON ではない",
        )
        assert plan.ok
        assert any("ルールベース" in w for w in plan.warnings)

    def test_llm_output_is_never_rendered_directly(self, docs: Path) -> None:
        """LLM が出典の無い主張を返しても描画対象に残らない。"""

        def evil(*, outlines, title, purpose):
            return json.dumps(
                {
                    "title": "t",
                    "scenes": [
                        {
                            "id": "s01",
                            "role": "body",
                            "claims": [{"text": "捏造された事実", "kind": "fact"}],
                            "diagram": {
                                "kind": "explain",
                                "source": {"path": outlines[0].path, "start": 1, "end": 3},
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            )

        plan, _ = plan_video(
            _resolved(docs, kb_paths=["manuals/guide.md"]),
            docs_dir=docs,
            title="t",
            chat_fn=evil,
        )
        rendered = json.dumps(plan.scenes, ensure_ascii=False)
        assert "捏造された事実" not in rendered, "出典の無い主張が描画対象に残っている"

    @pytest.mark.parametrize("raw", ['{"a": 1}', '```json\n{"a": 1}\n```', '```\n{"a": 1}\n```'])
    def test_parse_fenced_json(self, raw: str) -> None:
        parsed, error = parse_llm_draft(raw)
        assert error is None
        assert parsed == {"a": 1}


class TestOutline:
    def test_headings_and_bullets(self, docs: Path) -> None:
        item = _resolved(docs, kb_paths=["manuals/guide.md"])[0]
        outline = outline_document(docs, item)
        assert outline is not None
        assert outline.title == "登録手順"
        assert [t for _l, t, _n in outline.headings] == ["登録手順", "準備", "注意"]
        assert [t for t, _n in outline.bullets] == ["ログインする", "画面を開く", "登録する"]
