"""実 Manim レンダリングの統合検証(M7 task-3 ゲート): `KB_RUN_MANIM_TESTS=1` で有効化。

Manim/ffmpeg のインストールを要するため既定はスキップする(`test_embedding.py`
の `KB_RUN_MODEL_TESTS` と同じゲート方式)。

`scripts\\bootstrap-visualize.bat` でリポジトリ直下に `.venv-visualize` を作れば
**`KB_VISUALIZE_PYTHON` の指定は不要**。このテストは `repo_root` だけを渡し、
`renderer.render_scene` -> `manim_runner.resolve_python(root=repo_root)` が
`<repo>/.venv-visualize/Scripts/python.exe` を自力で見つける。
`KB_VISUALIZE_PYTHON` は「別の場所の venv を使いたいとき」だけの上書き手段。

`design/plans/M6-M10-remaining.md` M7 のゲート「実 Manim レンダリング(明示フラグ時)
で manifest sha256 検証成功」を満たすことを実測する。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from abist_kb.application.visualization.renderer import render_scene
from abist_kb.domain.line_range import range_hash

REPO_ROOT = Path(__file__).resolve().parents[2]

_run_manim_tests = os.environ.get("KB_RUN_MANIM_TESTS") == "1"

pytestmark_manim = pytest.mark.skipif(
    not _run_manim_tests,
    reason=(
        "実 Manim レンダリング(子プロセス起動・数秒〜数十秒)を伴うため既定はスキップ。"
        "KB_RUN_MANIM_TESTS=1 で実行する(scripts\\bootstrap-visualize.bat 済みなら"
        "KB_VISUALIZE_PYTHON の指定は不要)。"
    ),
)


@pytestmark_manim
@pytest.mark.slow
def test_real_manim_render_produces_manifest_with_verified_sha256(tmp_path: Path) -> None:
    doc_text = "行1\n行2\n行3\n"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "doc.md").write_text(doc_text, encoding="utf-8")
    reports_dir = tmp_path / "reports" / "visualizations"

    hashed = range_hash(doc_text, 1, 2)
    assert hashed.ok and hashed.hash is not None

    spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "mp4",
        "template": "step_explanation",
        "title": "実レンダリング検証",
        "sources": [
            {
                "id": "s1",
                "path": "doc.md",
                "start_line": 1,
                "end_line": 2,
                "content_hash": hashed.hash,
            }
        ],
        "beats": [
            {
                "type": "statement",
                "text": "これは実 Manim レンダリングの検証です",
                "source_refs": ["s1"],
            },
        ],
    }

    outcome = render_scene(
        spec,
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        repo_root=REPO_ROOT,
        timeout_seconds=180,
    )

    assert outcome.ok is True, (
        outcome.code,
        outcome.errors,
        outcome.stdout_tail,
        outcome.stderr_tail,
    )
    assert outcome.manifest_path is not None
    assert outcome.output_dir is not None

    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))
    assert manifest["outputs"], "manifest に outputs が記録されていません"
    output_entry = manifest["outputs"][0]
    output_file = outcome.output_dir / output_entry["path"]
    assert output_file.exists()

    actual_sha256 = hashlib.sha256(output_file.read_bytes()).hexdigest()
    assert actual_sha256 == output_entry["sha256"], "manifest の sha256 が実出力と一致しません"
    assert output_entry["size_bytes"] == output_file.stat().st_size


__all__ = []


@pytestmark_manim
@pytest.mark.slow
def test_timeline_renders_to_png_with_verified_sha256(tmp_path: Path) -> None:
    """timeline の実レンダリング smoke(png は save_last_frame で mp4 より桁違いに速い)。"""
    doc_text = "行1\n行2\n行3\n"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "t.md").write_text(doc_text, encoding="utf-8")
    spec = {
        "schema_version": "1.0",
        "scene_kind": "timeline",
        "output_format": "png",
        "template": "timeline_v1",
        "title": "経緯の実レンダリング",
        "sources": [
            {
                "id": "s1",
                "path": "t.md",
                "start_line": 1,
                "end_line": 2,
                "content_hash": range_hash(doc_text, 1, 2).hash,
            }
        ],
        "beats": [
            {
                "type": "timeline_point",
                "at": "2026-05-14",
                "label": "統合方針を決定",
                "description": "抽出→提案→編集→出力を単一アプリへ",
                "source_refs": ["s1"],
            },
            {
                "type": "timeline_point",
                "at": "6/11 定例",
                "label": "VDI 要件を追加",
                "source_refs": ["s1"],
            },
        ],
    }
    outcome = render_scene(
        spec, docs_dir=docs_dir, reports_dir=tmp_path / "out", repo_root=REPO_ROOT
    )
    assert outcome.ok, (outcome.code, outcome.errors, outcome.stderr_tail)
    manifest = json.loads(Path(outcome.manifest_path).read_text(encoding="utf-8"))
    for entry in manifest["outputs"]:
        produced = Path(outcome.output_dir) / entry["path"]
        digest = hashlib.sha256(produced.read_bytes()).hexdigest()
        assert digest == entry["sha256"]
