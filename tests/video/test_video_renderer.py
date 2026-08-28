"""Phase 2: 複数 Manim シーンの結合。

実 Manim / ffmpeg を使うテストは `KB_RUN_MANIM_TESTS=1` でのみ実行する
（purring の `test_real_render.py` と同じゲート方式）。
それ以外は DI で差し替えた単体検証。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.application.video.project_store import (
    STATE_CANCELLED,
    STATE_SUCCEEDED,
    create_project,
    read_state,
    save_project,
)
from abist_kb.application.video.video_renderer import render_project
from abist_kb.domain.line_range import range_hash
from abist_kb.infrastructure.video.ffmpeg_runner import (
    ffmpeg_path,
    probe,
    write_concat_list,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_run_manim = os.environ.get("KB_RUN_MANIM_TESTS") == "1"
manim_gate = pytest.mark.skipif(
    not _run_manim,
    reason=(
        "実 Manim / ffmpeg レンダリングを伴うため既定はスキップ。"
        "KB_RUN_MANIM_TESTS=1 で実行する（scripts\\bootstrap-visualize.bat 済みなら"
        "KB_VISUALIZE_PYTHON の指定は不要）。"
    ),
)


@pytest.fixture
def project(tmp_path: Path):
    """2シーンのプロジェクトを実 Markdown から作る。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    body = "# 手順\n1. 開く\n2. 登録する\n3. 確認する\n"
    (docs / "manual.md").write_text(body, encoding="utf-8")
    content_hash = range_hash(body, 1, 4).hash

    resolved = resolve_inputs({"kb_paths": ["manual.md"]}, docs_dir=docs)
    assert resolved.ok

    def scene(scene_id: str, title: str, kind: str) -> dict:
        source = {
            "id": "s1",
            "path": "manual.md",
            "start_line": 1,
            "end_line": 4,
            "content_hash": content_hash,
        }
        if kind == "explain":
            beats = [{"type": "statement", "text": title, "source_refs": ["s1"]}]
            template = "step_explanation"
        else:
            beats = [
                {"type": "flow_step", "label": "開く", "source_refs": ["s1"]},
                {"type": "flow_step", "label": "登録", "source_refs": ["s1"]},
                {"type": "transition", "from": "開く", "to": "登録"},
            ]
            template = "data_flow_v1"
        return {
            "id": scene_id,
            "kind": kind,
            "scene_spec": {
                "schema_version": "1.0",
                "scene_kind": kind,
                "output_format": "mp4",
                "template": template,
                "title": title,
                "sources": [source],
                "beats": beats,
            },
        }

    spec = {
        "title": "操作手順の説明",
        "scenes": [scene("s01", "登録の手順", "explain"), scene("s02", "処理の流れ", "flow")],
    }
    result = create_project(spec, resolved, reports_dir=tmp_path / "reports")
    assert result.ok, result.errors
    return tmp_path, docs, result.project


class TestConcatList:
    def test_paths_are_absolute_and_quoted(self, tmp_path: Path) -> None:
        target = tmp_path / "日本語 ディレクトリ"
        target.mkdir()
        video = target / "a.mp4"
        video.write_bytes(b"x")
        listed = write_concat_list([video], tmp_path / "concat.txt")
        text = listed.read_text(encoding="utf-8")
        assert text.startswith("file '")
        assert "日本語" in text
        assert str(video.resolve()).replace("\\", "/") in text


class TestGuards:
    def test_missing_project_spec(self, tmp_path: Path) -> None:
        result = render_project(tmp_path, docs_dir=tmp_path, repo_root=REPO_ROOT)
        assert not result.ok
        assert result.code == "INVALID_VIDEO_SPEC"

    def test_no_renderable_scene(self, project) -> None:
        tmp_path, docs, created = project
        spec = json.loads((created.dir / "project-spec.json").read_text(encoding="utf-8"))
        spec["scenes"] = [{"id": "s01", "kind": "title"}]  # scene_spec 無し
        save_project(created.dir, spec)
        result = render_project(created.dir, docs_dir=docs, repo_root=REPO_ROOT)
        assert not result.ok
        assert result.code == "NO_RENDERABLE_SCENE"

    def test_cancel_before_first_scene(self, project) -> None:
        _tmp, docs, created = project
        result = render_project(
            created.dir, docs_dir=docs, repo_root=REPO_ROOT, should_cancel=lambda: True
        )
        assert not result.ok
        assert result.code == "RENDER_CANCELLED"
        assert read_state(created.dir)["state"] == STATE_CANCELLED


@manim_gate
@pytest.mark.slow
class TestRealRender:
    def test_two_scenes_become_one_video(self, project) -> None:
        """MVP コア: 複数シーンが1本の MP4 になる（無音で完走する）。"""
        _tmp, docs, created = project
        progress: list[tuple[str, int, int, str]] = []
        result = render_project(
            created.dir,
            docs_dir=docs,
            repo_root=REPO_ROOT,
            on_progress=lambda *args: progress.append(args),
        )
        assert result.ok, (result.code, result.errors)
        assert result.output is not None and result.output.is_file()
        assert len(result.scenes) == 2
        assert all(s.ok for s in result.scenes)

        info = probe(result.output)
        assert info.width == 1920
        assert info.height == 1080
        assert info.fps is not None and abs(info.fps - 30.0) < 0.5
        # 2シーン分の尺が足し合わさっている
        assert info.duration_sec is not None
        assert info.duration_sec > max(s.duration_sec or 0 for s in result.scenes)

        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["outputs"][0]["path"] == "output.mp4"
        assert len(manifest["scenes"]) == 2
        assert read_state(created.dir)["state"] == STATE_SUCCEEDED
        assert [p[0] for p in progress][-1] == "done"

    def test_manifest_sha256_matches_the_file(self, project) -> None:
        import hashlib

        _tmp, docs, created = project
        result = render_project(created.dir, docs_dir=docs, repo_root=REPO_ROOT)
        assert result.ok, result.code
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(result.output.read_bytes()).hexdigest()
        assert digest == manifest["outputs"][0]["sha256"]

    def test_bad_source_hash_fails_the_scene(self, project) -> None:
        """出典が壊れているシーンは描画されない（出典必須ポリシー）。"""
        _tmp, docs, created = project
        spec = json.loads((created.dir / "project-spec.json").read_text(encoding="utf-8"))
        spec["scenes"][0]["scene_spec"]["sources"][0]["content_hash"] = "b" * 64
        save_project(created.dir, spec)
        result = render_project(created.dir, docs_dir=docs, repo_root=REPO_ROOT)
        assert not result.ok
        assert result.code in ("SOURCE_HASH_MISMATCH", "INVALID_SCENE_SPEC")


def test_ffmpeg_is_available_for_the_gate() -> None:
    """ffmpeg が無いと Phase 2 の E2E は成立しない（環境の明示）。"""
    if _run_manim:
        assert ffmpeg_path() is not None, "KB_RUN_MANIM_TESTS=1 なら ffmpeg が必要"
