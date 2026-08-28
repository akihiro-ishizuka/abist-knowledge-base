"""入力解決（4経路 → `ResolvedInput`）。

**最重要の回帰**: `kb_directories` は「候補集合」であって、配下の全件を
使用必須にしない。`docs/` には約 85,000 件の Markdown があり、
ディレクトリによっては数百〜数万件を含むため、全件必須は原理的に破綻する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.domain.video_project_spec import (
    SELECTION_COLLECTION_CANDIDATE,
    SELECTION_EXPLICIT_PRIMARY,
    SELECTION_SUPPLEMENTAL,
)


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "manuals").mkdir(parents=True)
    (root / "decisions").mkdir(parents=True)
    for i in range(5):
        (root / "manuals" / f"手順{i}.md").write_text(f"# 手順{i}\n本文\n", encoding="utf-8")
    (root / "decisions" / "2026-08-01.md").write_text("# 決定\n本文\n", encoding="utf-8")
    (root / "manuals" / "画像.png").write_bytes(b"notmarkdown")
    (root / "requirements.md").write_text("# 要件\n本文\n", encoding="utf-8")
    return root


class TestExplicitPaths:
    def test_kb_path_is_explicit_primary_and_requires_usage(self, docs: Path) -> None:
        result = resolve_inputs({"kb_paths": ["requirements.md"]}, docs_dir=docs)
        assert result.ok
        assert len(result.inputs) == 1
        item = result.inputs[0]
        assert item.selection == SELECTION_EXPLICIT_PRIMARY
        assert item.require_usage is True
        assert item.origin["type"] == "kb_path"
        assert item.content_hash and len(item.content_hash) == 64

    def test_docs_prefix_is_stripped(self, docs: Path) -> None:
        result = resolve_inputs({"kb_paths": ["docs/requirements.md"]}, docs_dir=docs)
        assert result.ok
        assert result.inputs[0].path == "requirements.md"

    @pytest.mark.parametrize(
        "bad", ["../secret.md", "/etc/passwd", "C:/x.md", "manuals/画像.png", "missing.md"]
    )
    def test_invalid_paths_are_rejected(self, docs: Path, bad: str) -> None:
        result = resolve_inputs({"kb_paths": [bad]}, docs_dir=docs)
        assert not result.ok
        codes = {e["code"] for e in result.errors}
        assert "INVALID_INPUT_PATH" in codes or "NO_RESOLVABLE_INPUT" in codes


class TestDirectoryCollection:
    def test_directory_documents_are_candidates_not_required(self, docs: Path) -> None:
        """⚠ 最重要: ディレクトリ由来は require_usage=False。"""
        result = resolve_inputs({"kb_directories": ["manuals"]}, docs_dir=docs)
        assert result.ok
        assert result.inputs, "ディレクトリから1件も解決していない"
        for item in result.inputs:
            assert item.selection == SELECTION_COLLECTION_CANDIDATE
            assert item.require_usage is False, "ディレクトリ配下を個別に使用必須にしてはならない"
            assert item.origin["type"] == "kb_directory"
            assert item.origin["selector"] == "manuals"

    def test_non_markdown_is_not_collected(self, docs: Path) -> None:
        result = resolve_inputs({"kb_directories": ["manuals"]}, docs_dir=docs)
        assert all(not i.path.endswith(".png") for i in result.inputs)

    def test_truncation_is_recorded_and_warned(self, docs: Path) -> None:
        result = resolve_inputs(
            {"kb_directories": ["manuals"]}, docs_dir=docs, max_docs_per_directory=2
        )
        assert result.ok
        assert len(result.inputs) == 2
        assert any(w["code"] == "INPUT_COLLECTION_TRUNCATED" for w in result.warnings)
        report = result.collections[0]
        assert report.candidate_total == 5
        assert report.selected_count == 2
        assert report.truncated is True
        assert report.selected and report.excluded_sample
        assert all("reason" in s for s in report.selected)
        assert all("reason" in e for e in report.excluded_sample)

    def test_total_limit_across_directories(self, docs: Path) -> None:
        result = resolve_inputs(
            {"kb_directories": ["manuals", "decisions"]},
            docs_dir=docs,
            max_docs_per_directory=10,
            max_total_candidates=3,
        )
        assert result.ok
        assert len(result.inputs) <= 3

    def test_empty_directory_warns(self, docs: Path) -> None:
        (docs / "empty").mkdir()
        result = resolve_inputs(
            {"kb_directories": ["empty"], "kb_paths": ["requirements.md"]}, docs_dir=docs
        )
        assert result.ok
        assert any(w["code"] == "EMPTY_DIRECTORY" for w in result.warnings)

    def test_selection_is_deterministic(self, docs: Path) -> None:
        """同じ入力なら同じ順序・同じ文書が選ばれること。"""
        kwargs = {"docs_dir": docs, "theme": "手順", "max_docs_per_directory": 3}
        first = resolve_inputs({"kb_directories": ["manuals"]}, **kwargs)
        second = resolve_inputs({"kb_directories": ["manuals"]}, **kwargs)
        assert [i.path for i in first.inputs] == [i.path for i in second.inputs]
        assert [s["path"] for s in first.collections[0].selected] == [
            s["path"] for s in second.collections[0].selected
        ]


class TestDeduplication:
    def test_explicit_wins_over_collection(self, docs: Path) -> None:
        """ディレクトリにも含まれる明示指定は explicit_primary のまま。"""
        result = resolve_inputs(
            {"kb_paths": ["manuals/手順0.md"], "kb_directories": ["manuals"]}, docs_dir=docs
        )
        assert result.ok
        matched = [i for i in result.inputs if i.path == "manuals/手順0.md"]
        assert len(matched) == 1, "重複が畳まれていない"
        assert matched[0].selection == SELECTION_EXPLICIT_PRIMARY
        assert matched[0].require_usage is True


class TestQueries:
    def test_query_results_are_supplemental(self, docs: Path) -> None:
        def fake_search(*, query: str, limit: int) -> list[dict]:
            return [{"path": "decisions/2026-08-01.md"}]

        result = resolve_inputs(
            {"kb_paths": ["requirements.md"], "kb_queries": ["決定"]},
            docs_dir=docs,
            search_fn=fake_search,
        )
        assert result.ok
        supplemental = [i for i in result.inputs if i.selection == SELECTION_SUPPLEMENTAL]
        assert len(supplemental) == 1
        assert supplemental[0].require_usage is False
        assert supplemental[0].origin["type"] == "kb_query"

    def test_search_failure_does_not_break_resolution(self, docs: Path) -> None:
        def broken_search(*, query: str, limit: int):
            raise RuntimeError("索引が壊れている")

        result = resolve_inputs(
            {"kb_paths": ["requirements.md"], "kb_queries": ["x"]},
            docs_dir=docs,
            search_fn=broken_search,
        )
        assert result.ok, "検索の失敗で解決全体を落としてはいけない"


class TestFailure:
    def test_no_resolvable_input(self, docs: Path) -> None:
        result = resolve_inputs({}, docs_dir=docs)
        assert not result.ok
        assert any(e["code"] == "NO_RESOLVABLE_INPUT" for e in result.errors)

    def test_esa_post_not_in_kb_is_a_warning_not_fatal(self, docs: Path) -> None:
        result = resolve_inputs(
            {
                "kb_paths": ["requirements.md"],
                "esa_posts": [{"url": "https://abist.esa.io/posts/99999"}],
            },
            docs_dir=docs,
        )
        assert result.ok, "esa 未取り込みで解決全体を落としてはいけない"
        assert any(w["code"] == "ESA_POST_NOT_IN_KB" for w in result.warnings)


class TestManifest:
    def test_manifest_shape(self, docs: Path) -> None:
        result = resolve_inputs(
            {"kb_paths": ["requirements.md"], "kb_directories": ["manuals"]},
            docs_dir=docs,
            max_docs_per_directory=2,
        )
        manifest = result.to_manifest()
        assert set(manifest) == {"inputs", "collections", "warnings"}
        assert manifest["inputs"][0]["selection"] == SELECTION_EXPLICIT_PRIMARY
        assert manifest["collections"][0]["selector"] == "manuals"
