"""Phase 3-6 通し: Markdown から視聴可能な動画一式まで。

実 Manim / ffmpeg を伴うので KB_RUN_MANIM_TESTS=1 ゲート。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.application.video.pipeline import run_pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
_run = os.environ.get("KB_RUN_MANIM_TESTS") == "1"
gate = pytest.mark.skipif(
    not _run, reason="実 Manim / ffmpeg を伴うため KB_RUN_MANIM_TESTS=1 で実行"
)


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "manuals").mkdir(parents=True)
    (root / "decisions").mkdir(parents=True)
    (root / "manuals" / "登録手順.md").write_text(
        "# 登録手順\n\n## 準備\n\n- ログインする\n- 画面を開く\n- 登録する\n\n## 注意\n\n本文\n",
        encoding="utf-8",
    )
    (root / "decisions" / "方針.md").write_text(
        "# 設計方針\n\n## 構成\n\n本文\n\n## 制約\n\n本文\n", encoding="utf-8"
    )
    return root


def _resolved(docs: Path):
    result = resolve_inputs(
        {"kb_paths": ["manuals/登録手順.md", "decisions/方針.md"]}, docs_dir=docs
    )
    assert result.ok
    return result


def _script() -> dict:
    """章立て・出典つきの台本（**作者が書いたもの**という前提の材料）。"""
    guide = "manuals/登録手順.md"
    policy = "decisions/方針.md"
    return {
        "title": "操作と設計の説明",
        "scenes": [
            {"id": "s01", "role": "intro", "title": "操作と設計の説明"},
            {"id": "s02", "role": "chapter", "title": "登録の手順", "chapter_index": 1},
            {
                "id": "s03",
                "role": "body",
                "title": "登録は3つの操作で終わります",
                "narration": {
                    "text": "登録はログイン、画面を開く、登録するの3手順です。",
                    "source_refs": ["s1"],
                },
                "claims": [
                    {"text": "ログインする", "kind": "fact", "source_refs": ["s1"]},
                    {"text": "画面を開く", "kind": "fact", "source_refs": ["s1"]},
                    {"text": "登録する", "kind": "fact", "source_refs": ["s1"]},
                ],
                "diagram": {
                    "kind": "flow",
                    "steps": ["ログインする", "画面を開く", "登録する"],
                    "source": {"path": guide, "start": 1, "end": 8},
                },
            },
            {"id": "s04", "role": "chapter", "title": "設計の方針", "chapter_index": 2},
            {
                "id": "s05",
                "role": "body",
                "title": "構成と制約を押さえます",
                "narration": {
                    "text": "設計方針では構成と制約を決めています。",
                    "source_refs": ["s1"],
                },
                "claims": [
                    {"text": "構成を決める", "kind": "fact", "source_refs": ["s1"]},
                    {"text": "制約を守る", "kind": "fact", "source_refs": ["s1"]},
                ],
                "diagram": {
                    "kind": "explain",
                    "source": {"path": policy, "start": 1, "end": 7},
                },
            },
            {
                "id": "s06",
                "role": "summary",
                "title": "まとめ",
                "narration": {
                    "text": "手順と方針を確認しました。",
                    "source_refs": ["s1"],
                },
                "claims": [{"text": "手順は3つ", "kind": "fact", "source_refs": ["s1"]}],
                "diagram": {"kind": "summary", "source": {"path": guide, "start": 1, "end": 8}},
            },
        ],
        "sound_events": [
            {"scene_id": "s01", "event": "intro", "anchor": "scene.start"},
            {"scene_id": "s02", "event": "chapter_change", "anchor": "scene.start"},
            {"scene_id": "s03", "event": "key_point", "anchor": "beat-2.reveal"},
            {"scene_id": "s06", "event": "outro", "anchor": "scene.end"},
        ],
    }


class TestPipelineGuards:
    def test_an_empty_script_fails_cleanly(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        (docs / "x").mkdir(parents=True)
        (docs / "x" / "empty.md").write_text("", encoding="utf-8")
        resolved = resolve_inputs({"kb_paths": ["x/empty.md"]}, docs_dir=docs)
        result = run_pipeline(
            resolved,
            docs_dir=docs,
            reports_dir=tmp_path / "reports",
            repo_root=REPO_ROOT,
            title="空",
            script={"title": "空", "scenes": []},
        )
        assert not result.ok
        assert result.errors


@gate
@pytest.mark.slow
class TestFullPipeline:
    def test_markdown_to_watchable_video(self, docs: Path, tmp_path: Path) -> None:
        """複数 Markdown から章立て動画・テロップ・効果音・出典が揃うこと。"""
        result = run_pipeline(
            _resolved(docs),
            docs_dir=docs,
            reports_dir=tmp_path / "reports",
            repo_root=REPO_ROOT,
            title="操作と設計の説明",
            purpose="新人向け",
            script=_script(),
        )
        assert result.ok, (result.code, result.errors)
        assert result.output is not None and result.output.is_file()
        assert result.scene_count >= 3, "章立てになっていない"

        # 字幕（サイドカーは手動アップロード用に残る）
        srt = result.subtitle_paths["srt"].read_text(encoding="utf-8")
        assert " --> " in srt
        assert result.subtitle_paths["vtt"].read_text(encoding="utf-8").startswith("WEBVTT")

        # テロップは映像へ焼き込まれている（無音で観るのでこれが本体）
        spec = json.loads((result.project_dir / "project-spec.json").read_text(encoding="utf-8"))
        assert spec["subtitles"]["burned_in"] is True, "テロップが画面に出ていない"
        assert spec["subtitles"]["cue_count"] > 0

        # 効果音（意味イベントから自動配置）
        cues = json.loads(
            (result.project_dir / "audio" / "sound-cues.json").read_text(encoding="utf-8")
        )
        assert cues["cues"], "効果音が1つも配置されていない"
        assert all("sound_id" in c and "sha256" in c for c in cues["cues"])

        # 出典と効果音の帰属
        citations = json.loads((result.project_dir / "citations.json").read_text(encoding="utf-8"))
        assert citations["sources"], "出典が残っていない"
        assert citations["sound_attributions"], "効果音の帰属が残っていない"

        # QA: 明示した主入力がすべて使われている
        assert result.qa_findings == [], result.qa_findings

    def test_video_is_understandable_with_the_sound_off(self, docs: Path, tmp_path: Path) -> None:
        """効果音まで切っても、テロップだけで内容が伝わる状態になる。"""
        result = run_pipeline(
            _resolved(docs),
            docs_dir=docs,
            reports_dir=tmp_path / "reports",
            repo_root=REPO_ROOT,
            title="無音版",
            script=_script(),
            sound_enabled=False,
        )
        assert result.ok, (result.code, result.errors)
        assert result.output is not None and result.output.is_file()
        assert result.subtitle_paths["srt"].is_file()
        spec = json.loads((result.project_dir / "project-spec.json").read_text(encoding="utf-8"))
        assert spec["subtitles"]["burned_in"] is True

    def test_sound_can_be_disabled(self, docs: Path, tmp_path: Path) -> None:
        result = run_pipeline(
            _resolved(docs),
            docs_dir=docs,
            reports_dir=tmp_path / "reports",
            repo_root=REPO_ROOT,
            title="効果音なし",
            script=_script(),
            sound_enabled=False,
        )
        assert result.ok
        assert result.sound_cue_count == 0
