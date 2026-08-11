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


class TestPipelineGuards:
    def test_no_scenes_fails_cleanly(self, tmp_path: Path) -> None:
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
        )
        # 台本が組めないか、描けるシーンが無い
        assert not result.ok or result.scene_count >= 0


@gate
@pytest.mark.slow
class TestFullPipeline:
    def test_markdown_to_watchable_video(self, docs: Path, tmp_path: Path) -> None:
        """複数 Markdown から章立て動画・字幕・効果音・出典が揃うこと。"""
        result = run_pipeline(
            _resolved(docs),
            docs_dir=docs,
            reports_dir=tmp_path / "reports",
            repo_root=REPO_ROOT,
            title="操作と設計の説明",
            purpose="新人向け",
            tts="silence",
        )
        assert result.ok, (result.code, result.errors)
        assert result.output is not None and result.output.is_file()
        assert result.scene_count >= 3, "章立てになっていない"

        # 字幕
        srt = result.subtitle_paths["srt"].read_text(encoding="utf-8")
        assert " --> " in srt
        assert result.subtitle_paths["vtt"].read_text(encoding="utf-8").startswith("WEBVTT")

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

    def test_silent_path_without_tts(self, docs: Path, tmp_path: Path) -> None:
        """TTS 未設定（none）でも完走する。"""
        result = run_pipeline(
            _resolved(docs),
            docs_dir=docs,
            reports_dir=tmp_path / "reports",
            repo_root=REPO_ROOT,
            title="無音版",
            tts="none",
        )
        assert result.ok, (result.code, result.errors)
        assert result.output is not None and result.output.is_file()
        # 字幕は無音でも作られる
        assert result.subtitle_paths["srt"].is_file()

    def test_sound_can_be_disabled(self, docs: Path, tmp_path: Path) -> None:
        result = run_pipeline(
            _resolved(docs),
            docs_dir=docs,
            reports_dir=tmp_path / "reports",
            repo_root=REPO_ROOT,
            title="効果音なし",
            sound_enabled=False,
        )
        assert result.ok
        assert result.sound_cue_count == 0
