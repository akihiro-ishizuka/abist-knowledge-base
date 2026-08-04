"""`application.visualization.source_verifier` の検証(M7 task-3):

metric の不良出典は即中断、statement/flow_step は警告して除外・カスケードする
非対称ポリシーを確認する。旧実装 `tools/lib/source-verifier.js` の移植先。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.visualization.source_verifier import verify_sources
from abist_kb.domain.line_range import range_hash

_DOC_TEXT = "行1\n行2\n行3\n行4\n行5\n"


@pytest.fixture
def docs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    (d / "doc.md").write_text(_DOC_TEXT, encoding="utf-8")
    return d


def _valid_hash(start: int, end: int) -> str:
    result = range_hash(_DOC_TEXT, start, end)
    assert result.ok
    assert result.hash is not None
    return result.hash


def _source(**overrides):
    src = {
        "id": "s1",
        "path": "doc.md",
        "start_line": 1,
        "end_line": 2,
        "content_hash": _valid_hash(1, 2),
    }
    src.update(overrides)
    return src


def test_all_sources_ok_passes_through_unchanged(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source()],
        "beats": [{"type": "statement", "text": "文", "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is True
    assert result.spec["beats"] == spec["beats"]
    assert result.warnings == []


def test_metric_with_missing_source_aborts_with_source_not_found(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", path="missing.md")],
        "beats": [{"type": "metric", "label": "L", "value": 1, "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "SOURCE_NOT_FOUND"


def test_metric_with_hash_mismatch_aborts_with_source_hash_mismatch(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", content_hash="f" * 64)],
        "beats": [{"type": "metric", "label": "L", "value": 1, "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "SOURCE_HASH_MISMATCH"


def test_statement_with_bad_source_is_dropped_with_warning(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", content_hash="f" * 64), _source(id="s2")],
        "beats": [
            {"type": "statement", "text": "壊れた文", "source_refs": ["s1"]},
            {"type": "statement", "text": "生きてる文", "source_refs": ["s2"]},
        ],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is True
    assert len(result.spec["beats"]) == 1
    assert result.spec["beats"][0]["text"] == "生きてる文"
    assert len(result.warnings) == 1


def test_dropped_flow_step_cascades_to_referencing_transition(docs_dir: Path):
    spec = {
        "scene_kind": "flow",
        "sources": [_source(id="s1", content_hash="f" * 64), _source(id="s2")],
        "beats": [
            {"type": "flow_step", "label": "A", "description": "壊れた", "source_refs": ["s1"]},
            {"type": "flow_step", "label": "B", "description": "生きてる", "source_refs": ["s2"]},
            {"type": "flow_step", "label": "C", "description": "生きてる2", "source_refs": ["s2"]},
            {"type": "transition", "from": "A", "to": "B"},
            {"type": "transition", "from": "B", "to": "C"},
        ],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is True
    kept_types_labels = [
        (b["type"], b.get("label"), b.get("from"), b.get("to")) for b in result.spec["beats"]
    ]
    assert ("flow_step", "A", None, None) not in kept_types_labels
    assert ("transition", None, "A", "B") not in kept_types_labels
    assert ("transition", None, "B", "C") in kept_types_labels
    assert len(result.warnings) == 2  # flow_step除外 + transition連鎖除外


def test_pruning_below_explain_minimum_is_invalid_scene_spec(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", content_hash="f" * 64)],
        "beats": [{"type": "statement", "text": "唯一の文", "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "INVALID_SCENE_SPEC"


def test_pruning_below_flow_minimum_is_invalid_scene_spec(docs_dir: Path):
    spec = {
        "scene_kind": "flow",
        "sources": [_source(id="s1", content_hash="f" * 64), _source(id="s2")],
        "beats": [
            {"type": "flow_step", "label": "A", "description": "壊れた", "source_refs": ["s1"]},
            {"type": "flow_step", "label": "B", "description": "生きてる", "source_refs": ["s2"]},
        ],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "INVALID_SCENE_SPEC"


def test_decorative_beat_bypasses_verification_entirely(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", path="missing.md")],
        "beats": [
            {"type": "statement", "text": "装飾", "decorative": True, "source_refs": ["s1"]},
            {"type": "statement", "text": "本文", "source_refs": []},
        ],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is True
    assert len(result.spec["beats"]) == 2


def test_path_traversal_is_treated_as_not_found(docs_dir: Path):
    spec = {
        "scene_kind": "explain",
        "sources": [_source(id="s1", path="../outside.md")],
        "beats": [{"type": "metric", "label": "L", "value": 1, "source_refs": ["s1"]}],
    }
    result = verify_sources(spec, docs_dir)
    assert result.ok is False
    assert result.code == "SOURCE_NOT_FOUND"
