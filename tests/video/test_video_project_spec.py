"""VideoProjectSpec 1.0 の検証。"""

from __future__ import annotations

import pytest

from abist_kb.domain.video_project_spec import (
    SELECTION_COLLECTION_CANDIDATE,
    SELECTION_EXPLICIT_PRIMARY,
    normalize_docs_path,
    validate_video_project_spec,
)


def _spec(**overrides) -> dict:
    base = {"title": "テスト動画", "inputs": {"kb_paths": ["knowledge/a.md"]}}
    base.update(overrides)
    return base


class TestNormalizeDocsPath:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("knowledge/a.md", "knowledge/a.md"),
            ("docs/knowledge/a.md", "knowledge/a.md"),
            ("./knowledge/a.md", "knowledge/a.md"),
            ("knowledge//a.md", "knowledge/a.md"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert normalize_docs_path(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["../x.md", "a/../../x.md", "/etc/passwd", "C:/x.md", "docs", "", None]
    )
    def test_rejects_escapes(self, raw) -> None:
        assert normalize_docs_path(raw) is None


class TestValidate:
    def test_minimal_valid(self) -> None:
        result = validate_video_project_spec(_spec())
        assert result.ok, [e.to_dict() for e in result.errors]
        assert result.spec["distribution"]["classification"] == "internal"
        assert result.spec["distribution"]["public_candidate"] is False
        assert result.spec["format"]["aspect_ratio"] == "16:9"

    def test_empty_inputs_is_no_resolvable_input(self) -> None:
        result = validate_video_project_spec(_spec(inputs={}))
        assert not result.ok
        assert any(e.code == "NO_RESOLVABLE_INPUT" for e in result.errors)

    def test_path_escape_is_rejected(self) -> None:
        result = validate_video_project_spec(_spec(inputs={"kb_paths": ["../secret.md"]}))
        assert not result.ok
        assert any(e.code == "INVALID_INPUT_PATH" for e in result.errors)

    def test_non_markdown_kb_path_is_rejected(self) -> None:
        result = validate_video_project_spec(_spec(inputs={"kb_paths": ["a/b.txt"]}))
        assert not result.ok
        assert any(e.code == "INVALID_INPUT_PATH" for e in result.errors)

    def test_directory_need_not_be_markdown(self) -> None:
        assert validate_video_project_spec(_spec(inputs={"kb_directories": ["knowledge"]})).ok

    def test_query_accepts_string_and_object(self) -> None:
        spec = _spec(inputs={"kb_queries": ["検索語", {"query": "別の語", "top_k": 5}]})
        assert validate_video_project_spec(spec).ok

    def test_esa_post_requires_url_or_id(self) -> None:
        result = validate_video_project_spec(_spec(inputs={"esa_posts": [{}]}))
        assert not result.ok

    def test_title_is_required(self) -> None:
        result = validate_video_project_spec({"inputs": {"kb_paths": ["a.md"]}})
        assert not result.ok
        assert any(e.path == "title" for e in result.errors)

    def test_bad_aspect_ratio(self) -> None:
        result = validate_video_project_spec(_spec(format={"aspect_ratio": "4:3"}))
        assert not result.ok

    def test_confidential_source_forces_public_candidate_false(self) -> None:
        spec = _spec(
            distribution={"classification": "internal", "public_candidate": True},
            sources=[
                {
                    "path": "a.md",
                    "selection": SELECTION_EXPLICIT_PRIMARY,
                    "require_usage": True,
                    "sensitivity": "confidential",
                    "origin": {"type": "kb_path"},
                }
            ],
        )
        result = validate_video_project_spec(spec)
        assert result.ok, [e.to_dict() for e in result.errors]
        assert result.spec["distribution"]["public_candidate"] is False


class TestSourcesConsistency:
    def _source(self, **over) -> dict:
        base = {
            "path": "a.md",
            "selection": SELECTION_EXPLICIT_PRIMARY,
            "require_usage": True,
            "origin": {"type": "kb_path"},
        }
        base.update(over)
        return base

    def test_require_usage_must_match_selection(self) -> None:
        """require_usage は selection から一意に決まる（食い違いを弾く）。"""
        bad = self._source(selection=SELECTION_COLLECTION_CANDIDATE, require_usage=True)
        bad["origin"] = {"type": "kb_directory"}
        result = validate_video_project_spec(_spec(sources=[bad]))
        assert not result.ok
        assert any(e.path.endswith("require_usage") for e in result.errors)

    def test_directory_origin_must_be_collection_candidate(self) -> None:
        bad = self._source(origin={"type": "kb_directory"})
        result = validate_video_project_spec(_spec(sources=[bad]))
        assert not result.ok

    def test_duplicate_source_path_is_rejected(self) -> None:
        result = validate_video_project_spec(_spec(sources=[self._source(), self._source()]))
        assert not result.ok
        assert any(e.code == "duplicate_path" for e in result.errors)

    def test_collection_candidate_source_is_valid(self) -> None:
        src = self._source(
            selection=SELECTION_COLLECTION_CANDIDATE,
            require_usage=False,
            origin={"type": "kb_directory", "selector": "knowledge"},
        )
        assert validate_video_project_spec(_spec(sources=[src])).ok
