"""`migrate inspect` の核: ファイルシステムを正とする棚卸し(§4 の再現テスト)。"""

from __future__ import annotations

from pathlib import Path

from abist_kb.migration.inventory import inspect_source


def test_inspect_finds_orphans_the_old_databases_miss(old_repo: Path, tmp_path: Path) -> None:
    report = inspect_source(old_repo, tmp_path / "sandbox")

    paths = {c.relative_path for c in report.markdown_candidates}
    assert "docs/knowledge/catiadoc/b.md" in paths
    assert "C#ATIA/outside.md" in paths
    assert "docs/a.md" in paths

    # sync-state.sqlite has only docs/a.md; b.md and outside.md must show up as orphans.
    assert "docs/knowledge/catiadoc/b.md" in report.orphan_paths
    assert "docs/a.md" not in report.orphan_paths


def test_inspect_flags_encoding_and_frontmatter_damage(old_repo: Path, tmp_path: Path) -> None:
    report = inspect_source(old_repo, tmp_path / "sandbox")
    by_path = {c.relative_path: c for c in report.markdown_candidates}

    assert by_path["docs/mojibake.md"].encoding_issue is True
    assert by_path["docs/broken.md"].frontmatter_broken is True
    assert by_path["docs/a.md"].encoding_issue is False
    assert by_path["docs/a.md"].frontmatter_broken is False


def test_inspect_reports_batch_outputdir_outside_docs(old_repo: Path, tmp_path: Path) -> None:
    report = inspect_source(old_repo, tmp_path / "sandbox")
    assert "C#ATIA" in report.batch_outside_docs
    assert report.batch_config_error is None


def test_inspect_reads_sync_state_counts(old_repo: Path, tmp_path: Path) -> None:
    report = inspect_source(old_repo, tmp_path / "sandbox")
    assert report.sync_state is not None
    assert report.sync_state.total_rows == 1
    assert report.sync_state.by_source == {"git": 1}
    assert report.sync_state.schema_version == 1


def test_inspect_never_modifies_source(old_repo: Path, tmp_path: Path) -> None:
    sync_db = old_repo / "data" / "sync-state.sqlite"
    before_size = sync_db.stat().st_size
    before_mtime = sync_db.stat().st_mtime_ns

    inspect_source(old_repo, tmp_path / "sandbox")

    after = sync_db.stat()
    assert after.st_size == before_size
    assert after.st_mtime_ns == before_mtime
