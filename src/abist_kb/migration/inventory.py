"""`migrate inspect`(設計書 §11.3)の中核: 移行元の read-only 棚卸し。

**ファイルシステムを正とする。** `tests/fixtures/PROVENANCE.md` §4 の実測により、
`sync-state.sqlite` は `backfill-metadata.js` の1回限りのスナップショットで
あり、既定で参照コーパス配下(`knowledge/B32doc`・`knowledge/catiadoc`・
`knowledge/generated` など)を除外する。DB を正としてしまうと、DB に
未登録の実ファイル(実測で約1,300件、`docs/knowledge/catiadoc` 配下)を
移行から静かに落とす。そのため `inspect` はまずリポジトリ全体を
ファイルシステムから走査し、DB はあくまで突合・注記のための補助情報として
サンドボックスコピー経由で読む。

同じ理由(§4)で、収集済みコンテンツは `docs/` の外にも存在しうる
(旧 web アダプタが `outputDir` に `docs/` を強制前置しないバグの実例が
`C#ATIA` バッチ)。そのため走査対象は `docs/` 配下に限定せず、
`batch-config.js` に記録された `outputDir` も候補に加える。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.domain.frontmatter import parse_frontmatter
from abist_kb.migration.batch_config_parser import parse_batch_config
from abist_kb.migration.sandbox import copy_sqlite_to_sandbox

_IGNORED_DIR_NAMES = frozenset({".git", "node_modules", "__pycache__", ".pytest_cache"})

_IGNORED_DIR_PREFIXES = (".venv", "venv", "site-packages")
"""実データ検証(scratch-real-inspect)で判明: 旧リポジトリ直下に
`.venv-visualize`(Manim 用の別仮想環境)が置かれており、これを除外しない
と `.venv-visualize/Lib/site-packages/**/*.md`(ライブラリ同梱の LICENSE.md
等、数万件)が Markdown 候補に混入し、棚卸し件数・孤児件数を大きく歪める。
ベンダー由来のディレクトリはナレッジベースの実データではないため除外する。
"""


def _is_ignored_dir(name: str) -> bool:
    if name in _IGNORED_DIR_NAMES:
        return True
    lowered = name.lower()
    return lowered.startswith(_IGNORED_DIR_PREFIXES) or lowered.endswith(".dist-info")


_TEXT_LIKE_SUFFIXES = frozenset({".md", ".markdown"})


@dataclass(frozen=True, slots=True)
class MarkdownCandidate:
    """走査で見つかった Markdown ファイル1件。"""

    relative_path: str
    """`from_root` からの相対パス(POSIX 区切り)。"""
    size_bytes: int
    sha256: str
    encoding_issue: bool
    """UTF-8 として読めない、または置換文字(U+FFFD)を含む場合に True。"""
    frontmatter_broken: bool
    """`---` で始まるのに front matter として閉じられていない場合に True。"""
    inside_docs: bool
    """`docs/` 配下かどうか(False は §4 の docs 外孤児候補)。"""


@dataclass(frozen=True, slots=True)
class DbCounts:
    """旧 DB 1つぶんの件数集計(サンドボックスコピー経由で読み取り)。"""

    schema_version: int | None
    total_rows: int
    by_source: dict[str, int]
    by_sync_status: dict[str, int]
    tracked_paths: frozenset[str]


@dataclass(frozen=True, slots=True)
class InspectReport:
    """`migrate inspect` の出力全体。"""

    from_root: str
    markdown_candidates: tuple[MarkdownCandidate, ...]
    sync_state: DbCounts | None
    reference_index: DbCounts | None
    orphan_paths: tuple[str, ...]
    """`sync-state.sqlite` にも `reference-index.sqlite` にも登録が無い Markdown。"""
    batch_config_error: str | None
    batch_outside_docs: tuple[str, ...]
    """batch-config.js の `outputDir` のうち `docs/` 配下にないもの。"""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_root": self.from_root,
            "markdown_count": len(self.markdown_candidates),
            "markdown_total_bytes": sum(c.size_bytes for c in self.markdown_candidates),
            "encoding_issue_count": sum(1 for c in self.markdown_candidates if c.encoding_issue),
            "frontmatter_broken_count": sum(
                1 for c in self.markdown_candidates if c.frontmatter_broken
            ),
            "outside_docs_count": sum(1 for c in self.markdown_candidates if not c.inside_docs),
            "sync_state": _db_counts_to_dict(self.sync_state),
            "reference_index": _db_counts_to_dict(self.reference_index),
            "orphan_count": len(self.orphan_paths),
            "orphan_paths_sample": list(self.orphan_paths[:20]),
            "batch_config_error": self.batch_config_error,
            "batch_outside_docs": list(self.batch_outside_docs),
            "warnings": list(self.warnings),
        }


def _db_counts_to_dict(counts: DbCounts | None) -> dict[str, Any] | None:
    if counts is None:
        return None
    return {
        "schema_version": counts.schema_version,
        "total_rows": counts.total_rows,
        "by_source": counts.by_source,
        "by_sync_status": counts.by_sync_status,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_broken_frontmatter(raw: str, result_has_frontmatter: bool) -> bool:
    stripped = raw.lstrip("﻿")
    if result_has_frontmatter:
        return False
    # '---' で始まるのに parse_frontmatter が front matter なしと判定した場合、
    # 閉じ区切りが無い(=壊れている)可能性が高い。
    first_line = stripped.split("\n", 1)[0].strip("\r")
    return first_line == "---"


def scan_markdown(from_root: Path) -> list[MarkdownCandidate]:
    """`from_root` 全体(`.git`/`node_modules` 等を除く)を走査して Markdown 候補を集める。

    `docs/` の外も対象にする(§4: 旧 web アダプタの `docs/` 未強制付与)。
    """
    candidates: list[MarkdownCandidate] = []
    docs_root = from_root / "docs"
    matches: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(from_root):
        # プルーニング: ベンダーディレクトリ配下は再帰しない(実データ検証で
        # `.venv-visualize/Lib/site-packages` が数万件の無関係な *.md を含む
        # ことが判明した。os.walk の in-place dirnames 変更で早期に切り捨てる)。
        dirnames[:] = [d for d in dirnames if not _is_ignored_dir(d)]
        for filename in filenames:
            if Path(filename).suffix.lower() in _TEXT_LIKE_SUFFIXES:
                matches.append(Path(dirpath) / filename)
    for path in sorted(matches):
        relative = path.relative_to(from_root).as_posix()
        raw_bytes = path.read_bytes()
        encoding_issue = False
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            encoding_issue = True
            text = raw_bytes.decode("utf-8", errors="replace")
        if "�" in text:
            encoding_issue = True
        result = parse_frontmatter(text)
        frontmatter_broken = _looks_broken_frontmatter(text, result.has_frontmatter)
        try:
            inside_docs = path.relative_to(docs_root) is not None
        except ValueError:
            inside_docs = False
        candidates.append(
            MarkdownCandidate(
                relative_path=relative,
                size_bytes=len(raw_bytes),
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
                encoding_issue=encoding_issue,
                frontmatter_broken=frontmatter_broken,
                inside_docs=inside_docs,
            )
        )
    return candidates


def _read_db_counts(db_path: Path, sandbox_dir: Path, path_column: str = "path") -> DbCounts:
    sandboxed = copy_sqlite_to_sandbox(db_path, sandbox_dir)
    conn = sqlite3.connect(f"file:{sandboxed.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        schema_version: int | None = None
        try:
            row = conn.execute("SELECT schema_version FROM meta LIMIT 1").fetchone()
            if row is not None:
                schema_version = int(row["schema_version"])
        except sqlite3.OperationalError:
            schema_version = None

        by_source: dict[str, int] = {}
        by_sync_status: dict[str, int] = {}
        tracked_paths: set[str] = set()
        try:
            cursor = conn.execute("SELECT * FROM documents")  # noqa: S608
        except sqlite3.OperationalError:
            return DbCounts(
                schema_version=schema_version,
                total_rows=0,
                by_source={},
                by_sync_status={},
                tracked_paths=frozenset(),
            )
        columns = [d[0] for d in cursor.description]
        total_rows = 0
        for row in cursor:
            total_rows += 1
            record = dict(zip(columns, row, strict=True))
            source = str(record.get("source") or "(none)")
            by_source[source] = by_source.get(source, 0) + 1
            if "sync_status" in record:
                status = str(record.get("sync_status") or "(none)")
                by_sync_status[status] = by_sync_status.get(status, 0) + 1
            if path_column in record and record[path_column]:
                tracked_paths.add(str(record[path_column]))
        return DbCounts(
            schema_version=schema_version,
            total_rows=total_rows,
            by_source=by_source,
            by_sync_status=by_sync_status,
            tracked_paths=frozenset(tracked_paths),
        )
    finally:
        conn.close()


def _batch_outside_docs(from_root: Path) -> tuple[tuple[str, ...], str | None]:
    batch_config_path = from_root / "data" / "batch-config.js"
    if not batch_config_path.exists():
        batch_config_path = from_root / "batch-config.js"
    if not batch_config_path.exists():
        return (), None
    text = batch_config_path.read_text(encoding="utf-8")
    try:
        config = parse_batch_config(text)
    except Exception as exc:  # noqa: BLE001 - inspect は診断のみ、失敗を握りつぶさず記録
        return (), str(exc)
    outside: list[str] = []
    for _name, entry in config.items():
        if not isinstance(entry, dict):
            continue
        output_dir = entry.get("outputDir")
        if not isinstance(output_dir, str):
            continue
        normalized = output_dir.replace("\\", "/")
        if normalized != "docs" and not normalized.startswith("docs/"):
            outside.append(output_dir)
    return tuple(outside), None


def inspect_source(from_root: Path, sandbox_dir: Path) -> InspectReport:
    """移行元を read-only で棚卸しする。移行元へは一切書き込まない。"""
    warnings: list[str] = []
    markdown_candidates = scan_markdown(from_root)

    sync_state: DbCounts | None = None
    sync_state_path = from_root / "data" / "sync-state.sqlite"
    if sync_state_path.exists():
        sync_state = _read_db_counts(sync_state_path, sandbox_dir / "sync-state")
    else:
        warnings.append("data/sync-state.sqlite が見つかりません。")

    reference_index: DbCounts | None = None
    reference_index_path = from_root / "data" / "reference-index.sqlite"
    if reference_index_path.exists():
        reference_index = _read_db_counts(reference_index_path, sandbox_dir / "reference-index")
    else:
        warnings.append("data/reference-index.sqlite が見つかりません。")

    tracked: set[str] = set()
    if sync_state is not None:
        tracked |= sync_state.tracked_paths
    if reference_index is not None:
        tracked |= reference_index.tracked_paths

    def _normalize(path: str) -> str:
        return path.replace("\\", "/").removeprefix("docs/")

    normalized_tracked = {_normalize(p) for p in tracked}
    orphan_paths = tuple(
        sorted(
            c.relative_path
            for c in markdown_candidates
            if c.inside_docs
            and _normalize(c.relative_path.removeprefix("docs/")) not in normalized_tracked
        )
    )

    batch_outside_docs, batch_config_error = _batch_outside_docs(from_root)
    if batch_config_error:
        warnings.append(
            f"batch-config.js の解析に失敗しました(UNSUPPORTED_BATCH_CONFIG): {batch_config_error}"
        )

    return InspectReport(
        from_root=str(from_root),
        markdown_candidates=tuple(markdown_candidates),
        sync_state=sync_state,
        reference_index=reference_index,
        orphan_paths=orphan_paths,
        batch_config_error=batch_config_error,
        batch_outside_docs=batch_outside_docs,
        warnings=tuple(warnings),
    )


def write_inspect_report(report: InspectReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
