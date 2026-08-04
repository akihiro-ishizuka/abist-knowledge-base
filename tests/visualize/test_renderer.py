"""`application.visualization.renderer.render_scene` のオーケストレーション(M7 task-3)。

実 Manim/Python は呼ばず、`run_process`/`resolve_python_fn`/`ffmpeg_version_fn` を
DI で差し替えて検証する。実レンダリング(Manim 実行)の検証は
`tests/visualize/test_real_render.py`(`KB_RUN_MANIM_TESTS=1` で明示有効化)で行う。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abist_kb.application.visualization.renderer import render_scene
from abist_kb.domain.line_range import range_hash
from abist_kb.infrastructure.visualization.manim_runner import ProcessResult

_DOC_TEXT = "行1\n行2\n行3\n"


@pytest.fixture
def docs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    (d / "doc.md").write_text(_DOC_TEXT, encoding="utf-8")
    return d


@pytest.fixture
def reports_dir(tmp_path: Path) -> Path:
    return tmp_path / "reports" / "visualizations"


def _hash(start: int, end: int) -> str:
    result = range_hash(_DOC_TEXT, start, end)
    assert result.hash is not None
    return result.hash


def _spec(**overrides):
    spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "mp4",
        "template": "step_explanation",
        "title": "テスト",
        "sources": [
            {
                "id": "s1",
                "path": "doc.md",
                "start_line": 1,
                "end_line": 2,
                "content_hash": _hash(1, 2),
            }
        ],
        "beats": [{"type": "statement", "text": "文", "source_refs": ["s1"]}],
    }
    spec.update(overrides)
    return spec


def test_invalid_scene_spec_fails_before_touching_filesystem(docs_dir, reports_dir):
    outcome = render_scene(
        {"foo": "bar"}, docs_dir=docs_dir, reports_dir=reports_dir, repo_root=Path.cwd()
    )
    assert outcome.ok is False
    assert outcome.code == "INVALID_SCENE_SPEC"
    assert not reports_dir.exists()


def test_source_hash_mismatch_fails_before_python_resolution(docs_dir, reports_dir):
    spec = _spec(
        beats=[{"type": "metric", "label": "L", "value": 1, "source_refs": ["s1"]}],
        sources=[
            {
                "id": "s1",
                "path": "doc.md",
                "start_line": 1,
                "end_line": 2,
                "content_hash": "f" * 64,
            }
        ],
    )

    def _boom(**_kwargs):
        raise AssertionError("resolve_python should not be called")

    outcome = render_scene(
        spec,
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        repo_root=Path.cwd(),
        resolve_python_fn=_boom,
    )
    assert outcome.ok is False
    assert outcome.code == "SOURCE_HASH_MISMATCH"
    assert not reports_dir.exists()


def test_python_not_found_fails_after_source_verification(docs_dir, reports_dir):
    outcome = render_scene(
        _spec(),
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        repo_root=Path.cwd(),
        python_path=None,
    )
    assert outcome.ok is False
    assert outcome.code == "PYTHON_NOT_FOUND"


def test_successful_render_writes_manifest_with_sha256(docs_dir, reports_dir):
    def fake_run_process(*, python_path, args, timeout_seconds, cwd):
        outdir = Path(args[args.index("--outdir") + 1])
        output_file = outdir / "output.mp4"
        output_file.write_bytes(b"fake video bytes")
        payload = json.dumps(
            {"ok": True, "output": "output.mp4", "python": "3.11.9", "manim": "0.19.0"}
        )
        return ProcessResult(exit_code=0, stdout=payload, stderr="", timed_out=False)

    outcome = render_scene(
        _spec(),
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        repo_root=Path.cwd(),
        python_path="fake-python",
        run_process=fake_run_process,
        ffmpeg_version_fn=lambda: "ffmpeg version 8.0.1",
    )
    assert outcome.ok is True
    assert outcome.manifest_path is not None
    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))
    assert manifest["outputs"][0]["path"] == "output.mp4"
    assert len(manifest["outputs"][0]["sha256"]) == 64
    assert manifest["generator"]["manim"] == "0.19.0"


def test_render_timeout_writes_manifest_and_fails(docs_dir, reports_dir):
    def fake_run_process(*, python_path, args, timeout_seconds, cwd):
        return ProcessResult(exit_code=None, stdout="", stderr="", timed_out=True)

    outcome = render_scene(
        _spec(),
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        repo_root=Path.cwd(),
        python_path="fake-python",
        run_process=fake_run_process,
    )
    assert outcome.ok is False
    assert outcome.code == "RENDER_TIMEOUT"
    assert outcome.manifest_path is not None
    assert outcome.manifest_path.exists()


def test_manim_not_found_propagates_code_from_payload(docs_dir, reports_dir):
    def fake_run_process(*, python_path, args, timeout_seconds, cwd):
        payload = json.dumps(
            {"ok": False, "code": "MANIM_NOT_FOUND", "error": "no module named manim"}
        )
        return ProcessResult(exit_code=3, stdout=payload, stderr="", timed_out=False)

    outcome = render_scene(
        _spec(),
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        repo_root=Path.cwd(),
        python_path="fake-python",
        run_process=fake_run_process,
    )
    assert outcome.ok is False
    assert outcome.code == "MANIM_NOT_FOUND"


def test_output_not_found_when_process_reports_success_but_file_missing(docs_dir, reports_dir):
    def fake_run_process(*, python_path, args, timeout_seconds, cwd):
        payload = json.dumps({"ok": True, "output": "output.mp4"})
        return ProcessResult(exit_code=0, stdout=payload, stderr="", timed_out=False)

    outcome = render_scene(
        _spec(),
        docs_dir=docs_dir,
        reports_dir=reports_dir,
        repo_root=Path.cwd(),
        python_path="fake-python",
        run_process=fake_run_process,
    )
    assert outcome.ok is False
    assert outcome.code == "OUTPUT_NOT_FOUND"
