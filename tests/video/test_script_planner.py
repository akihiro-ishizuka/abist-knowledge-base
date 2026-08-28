"""台本の受け入れと SceneSpec 生成。

**書かれた台本をそのまま描画しない**ことと、**出典に結び付かない主張を落とす**
ことの回帰テスト。台本を機械生成する経路は持たないので、ここで検証するのは
「渡された台本をどう受け入れるか」だけ。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.application.video.script_planner import author_script, outline_document
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


class TestAuthoredScript:
    """作者（エージェント）が書いた台本の受け入れ。

    台本を機械生成する経路は無いので、ここで見るのは「渡された台本をどう
    受け入れ、何を落とすか」だけ。
    """

    def _script(self, path: str) -> dict:
        return {
            "title": "操作説明",
            "scenes": [
                {"id": "s01", "role": "intro", "title": "はじめに"},
                {
                    "id": "s02",
                    "role": "body",
                    "title": "登録の手順",
                    "narration": {"text": "登録は3手順で終わります。", "source_refs": ["s1"]},
                    "claims": [
                        {"text": "ログインする", "kind": "fact", "source_refs": ["s1"]},
                        {"text": "画面を開く", "kind": "fact", "source_refs": ["s1"]},
                    ],
                    "diagram": {
                        "kind": "explain",
                        "source": {"path": path, "start": 1, "end": 8},
                    },
                },
            ],
            "sound_events": [{"scene_id": "s02", "event": "key_point"}],
        }

    def test_scene_specs_are_validated(self, docs: Path) -> None:
        result = author_script(
            self._script("manuals/guide.md"),
            _resolved(docs, kb_paths=["manuals/guide.md"]),
            docs_dir=docs,
        )
        assert result.ok, result.errors
        for scene in result.scenes:
            assert validate_scene_spec(scene["scene_spec"]).ok, scene["id"]

    def test_sources_carry_real_content_hash(self, docs: Path) -> None:
        """出典の content_hash は**実ファイル**から計算される（作者は書かない）。"""
        result = author_script(
            self._script("manuals/guide.md"),
            _resolved(docs, kb_paths=["manuals/guide.md"]),
            docs_dir=docs,
        )
        with_sources = [s for s in result.scenes if s["scene_spec"]["sources"]]
        assert with_sources
        source = with_sources[0]["scene_spec"]["sources"][0]
        text = (docs / source["path"]).read_text(encoding="utf-8")
        expected = range_hash(text, source["start_line"], source["end_line"]).hash
        assert source["content_hash"] == expected

    def test_used_paths_are_tracked(self, docs: Path) -> None:
        result = author_script(
            self._script("manuals/guide.md"),
            _resolved(docs, kb_paths=["manuals/guide.md", "manuals/design.md"]),
            docs_dir=docs,
        )
        assert "manuals/guide.md" in result.used_paths

    def test_a_claim_without_a_source_never_reaches_the_render(self, docs: Path) -> None:
        """出典の無い主張は描画対象に残らない（作者が誰であっても同じ）。"""
        script = self._script("manuals/guide.md")
        script["scenes"][1]["claims"].append({"text": "捏造された事実", "kind": "fact"})

        result = author_script(
            script, _resolved(docs, kb_paths=["manuals/guide.md"]), docs_dir=docs
        )

        rendered = json.dumps(result.scenes, ensure_ascii=False)
        assert "捏造された事実" not in rendered, "出典の無い主張が描画対象に残っている"

    def test_a_broken_script_does_not_silently_shrink(self, docs: Path) -> None:
        """描けないシーンは黙って消えず、どのシーンかが分かる形で返る。"""
        script = self._script("manuals/guide.md")
        script["scenes"][1]["diagram"] = {
            "kind": "timeline",
            "points": [],
            "source": {"path": "manuals/guide.md", "start": 1, "end": 8},
        }

        result = author_script(
            script, _resolved(docs, kb_paths=["manuals/guide.md"]), docs_dir=docs
        )

        assert not result.ok
        assert any(e.get("sceneId") == "s02" for e in result.errors)


class TestOutline:
    def test_headings_and_bullets(self, docs: Path) -> None:
        item = _resolved(docs, kb_paths=["manuals/guide.md"])[0]
        outline = outline_document(docs, item)
        assert outline is not None
        assert outline.title == "登録手順"
        assert [t for _l, t, _n in outline.headings] == ["登録手順", "準備", "注意"]
        assert [t for t, _n in outline.bullets] == ["ログインする", "画面を開く", "登録する"]
