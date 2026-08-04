"""`domain.scene_spec` の検証(M7 task-3): 旧実装 `tools/lib/scene-spec.js` の移植先。"""

from __future__ import annotations

from abist_kb.domain.scene_spec import (
    MAX_BEATS,
    SCENE_KINDS,
    validate_scene_spec,
)

_HASH = "a" * 64


def _base_source(**overrides):
    src = {
        "id": "s1",
        "path": "foo/bar.md",
        "start_line": 1,
        "end_line": 3,
        "content_hash": _HASH,
    }
    src.update(overrides)
    return src


def _explain_spec(**overrides):
    spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "mp4",
        "template": "step_explanation",
        "title": "テストタイトル",
        "sources": [_base_source()],
        "beats": [
            {"type": "statement", "text": "テスト文", "source_refs": ["s1"]},
        ],
    }
    spec.update(overrides)
    return spec


def test_valid_explain_spec_is_ok():
    result = validate_scene_spec(_explain_spec())
    assert result.ok is True
    assert result.spec is not None


def test_valid_flow_spec_is_ok():
    spec = {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "mp4",
        "template": "data_flow_v1",
        "title": "フロー",
        "sources": [_base_source()],
        "beats": [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "transition", "from": "A", "to": "B"},
        ],
    }
    result = validate_scene_spec(spec)
    assert result.ok is True


def test_empty_object_reports_multiple_errors():
    result = validate_scene_spec({"foo": "bar"})
    assert result.ok is False
    codes = {(e.path, e.code) for e in result.errors}
    assert ("schema_version", "invalid") in codes
    assert ("scene_kind", "unknown_kind") in codes
    assert ("output_format", "invalid") in codes
    assert ("title", "invalid") in codes
    assert ("sources", "invalid") in codes


def test_reserved_kind_reports_reserved_kind_error():
    result = validate_scene_spec(_explain_spec(scene_kind="timeline", template="timeline"))
    assert result.ok is False
    assert any(e.code == "reserved_kind" for e in result.errors)


def test_non_object_is_invalid():
    result = validate_scene_spec("not-an-object")
    assert result.ok is False
    assert result.errors[0].path == ""


def test_too_many_beats_is_invalid():
    spec = _explain_spec(
        beats=[
            {"type": "statement", "text": f"文{i}", "source_refs": ["s1"]}
            for i in range(MAX_BEATS + 1)
        ]
    )
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.path == "beats" for e in result.errors)


def test_metric_without_source_refs_is_missing_source_refs():
    spec = _explain_spec(beats=[{"type": "metric", "label": "ラベル", "value": 1}])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "missing_source_refs" for e in result.errors)


def test_statement_decorative_bypasses_source_refs_requirement():
    spec = _explain_spec(beats=[{"type": "statement", "text": "装飾", "decorative": True}])
    result = validate_scene_spec(spec)
    assert result.ok is True


def test_flow_step_description_requires_source_refs_unless_decorative():
    spec = {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "mp4",
        "template": "data_flow_v1",
        "title": "フロー",
        "sources": [_base_source()],
        "beats": [
            {"type": "flow_step", "label": "A", "description": "説明"},
            {"type": "flow_step", "label": "B"},
        ],
    }
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "missing_source_refs" for e in result.errors)


def test_transition_referencing_unknown_flow_label_is_invalid():
    spec = {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "mp4",
        "template": "data_flow_v1",
        "title": "フロー",
        "sources": [_base_source()],
        "beats": [
            {"type": "flow_step", "label": "A"},
            {"type": "flow_step", "label": "B"},
            {"type": "transition", "from": "A", "to": "C"},
        ],
    }
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "unknown_label" for e in result.errors)


def test_explain_requires_at_least_one_substantive_beat():
    # schema_version 段階の検証(source_verifier.py の剪定後チェックとは別物)では
    # decorative でも type が statement/metric なら「実質 beat」として数える
    # (旧実装 `scene-spec.js` の substantive フィルタも decorative を見ない)。
    # ここでは transition のみで statement/metric が0件のケースを使う。
    spec = {
        "schema_version": "1.0",
        "scene_kind": "explain",
        "output_format": "mp4",
        "template": "step_explanation",
        "title": "テスト",
        "sources": [_base_source()],
        "beats": [{"type": "transition", "from": "A", "to": "B"}],
    }
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.path == "beats" and e.code == "invalid" for e in result.errors)


def test_flow_requires_at_least_two_flow_steps():
    spec = {
        "schema_version": "1.0",
        "scene_kind": "flow",
        "output_format": "mp4",
        "template": "data_flow_v1",
        "title": "フロー",
        "sources": [_base_source()],
        "beats": [{"type": "flow_step", "label": "A"}],
    }
    result = validate_scene_spec(spec)
    assert result.ok is False


def test_duplicate_source_id_is_invalid():
    spec = _explain_spec(sources=[_base_source(id="s1"), _base_source(id="s1")])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "duplicate_id" for e in result.errors)


def test_source_path_traversal_is_invalid():
    spec = _explain_spec(sources=[_base_source(path="../secret.md")])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.path == "sources[0].path" for e in result.errors)


def test_source_content_hash_wrong_length_is_invalid():
    spec = _explain_spec(sources=[_base_source(content_hash="deadbeef")])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.path == "sources[0].content_hash" for e in result.errors)


def test_unknown_source_ref_is_invalid():
    spec = _explain_spec(beats=[{"type": "statement", "text": "文", "source_refs": ["missing"]}])
    result = validate_scene_spec(spec)
    assert result.ok is False
    assert any(e.code == "unknown_source_ref" for e in result.errors)


def test_scene_kinds_have_two_entries_matching_list_scene_kinds_contract():
    assert [k["kind"] for k in SCENE_KINDS] == ["explain", "flow"]
    for info in SCENE_KINDS:
        assert set(info.keys()) == {"kind", "description", "template", "required", "beat_types"}
