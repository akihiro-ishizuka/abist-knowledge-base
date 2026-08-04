"""索引対象コーパスの選定(設計書 §9.2、`design/plans/M4-index-search.md` Task1 Step5)。

**work コーパス**は `app.sqlite` の `documents` テーブル(`source ∈
{esa, web, git, manual}` かつ `document_type != reference`)から選ぶが、
`tests/fixtures/PROVENANCE.md` §4 が記録した実測事実により、このテーブルを
「完全な台帳」と仮定してはならない: 旧 `sync-state.sqlite` は
`backfill-metadata.js` の `defaultExclude`(`knowledge/B32doc` 等を既定除外)に
よる1回限りのバックフィル・スナップショットであり、`docs/knowledge/catiadoc`
(1,313ファイル)と `docs/knowledge/generated`(4ファイル)がどちらのDBにも
登録されていないメタデータ孤児として実測されている。`select_work_targets` は
`docs/` の実走査(B32doc 配下を除く)と `documents` の突合を行い、DB に無いが
`docs/` にあるパスを `disk_only_paths` として黙って落とさずに返す。

**reference コーパス**は `docs/knowledge/B32doc` を実走査し、M2 の
`domain.b32doc_filter.decide_indexable` で索引対象を選別する。タイトルは
front matter ではなく `## Summary Keys` の `- タイトル:` 行から取る
(B32doc の front matter に title 相当のキーが無いため、`extract_summary_keys`
を使わないとフォールバックでファイル名がタイトルになり、タイトル一致ブースト
(`design/plans/M4-index-search.md` Task3)が機能しなくなる)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.domain.b32doc_filter import decide_indexable, extract_summary_keys
from abist_kb.domain.frontmatter import parse_frontmatter
from abist_kb.domain.metadata_schema import to_posix_path

#: work コーパスとして索引対象になりうる `documents.source` の値。
WORK_SOURCES: frozenset[str] = frozenset({"esa", "web", "git", "manual"})

#: reference コーパス(B32doc)が占めるディレクトリ。work 側のディスク走査から
#: 除外する(B32doc は `select_reference_targets` が別途担当するスコープであり、
#: work の突合に混ぜると 7,563 件全部が「DBに無いのに disk にはある」偽陽性の
#: discrepancy になってしまう)。
REFERENCE_SUBTREE = "knowledge/B32doc"

_MARKDOWN_SUFFIXES = frozenset({".md"})


def _walk_markdown_paths(docs_dir: Path, *, exclude_prefixes: Sequence[str]) -> set[str]:
    """`docs_dir` 配下の Markdown ファイルを走査し、posix 相対パス(先頭 `/` 無し)の集合を返す。"""
    if not docs_dir.is_dir():
        return set()
    excluded = tuple(p.rstrip("/") + "/" for p in exclude_prefixes)
    paths: set[str] = set()
    for candidate in docs_dir.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in _MARKDOWN_SUFFIXES:
            continue
        rel = candidate.relative_to(docs_dir).as_posix()
        if any(rel.startswith(prefix) for prefix in excluded):
            continue
        paths.add(rel)
    return paths


@dataclass(frozen=True, slots=True)
class WorkSelection:
    """`select_work_targets` の戻り値。"""

    targets: tuple[dict[str, Any], ...]
    #: `documents` テーブルには無いが `docs/`(B32doc除く)には実在するパス
    #: (ソート済み、全件)。PROVENANCE.md §4 が要求する「黙って落とさない」
    #: 報告そのもの — M8 の移行棚卸しがこの値に依存する。
    disk_only_paths: tuple[str, ...]
    disk_only_count: int


def select_work_targets(documents: Sequence[Mapping[str, Any]], docs_dir: Path) -> WorkSelection:
    """`documents`(app.sqlite の全行)から work コーパスの索引対象を選び、
    `docs/` との突合結果を添えて返す。

    `documents` は事前にフィルタせず**全行**渡すこと(disk 突合の母集団を
    work 対象だけに絞ると、reference/未分類の行に対応するファイルまで
    disk_only として誤検出してしまうため)。
    """
    targets: list[dict[str, Any]] = []
    db_paths: set[str] = set()

    for doc in documents:
        path = to_posix_path(doc.get("path"))
        if not path:
            continue
        db_paths.add(path)

        source = doc.get("source")
        document_type = doc.get("document_type")
        if source not in WORK_SOURCES or document_type == "reference":
            continue

        targets.append(
            {
                "path": path,
                "post_number": doc.get("post_number"),
                "title": doc.get("title"),
                "source": source,
                "document_type": document_type,
                "status": doc.get("status"),
                "url": doc.get("url"),
                "category": doc.get("category"),
            }
        )

    disk_paths = _walk_markdown_paths(docs_dir, exclude_prefixes=(REFERENCE_SUBTREE,))
    disk_only = sorted(disk_paths - db_paths)

    return WorkSelection(
        targets=tuple(targets),
        disk_only_paths=tuple(disk_only),
        disk_only_count=len(disk_only),
    )


@dataclass(frozen=True, slots=True)
class ReferenceSelection:
    """`select_reference_targets` の戻り値。"""

    targets: tuple[dict[str, Any], ...]
    #: 除外理由(`domain.b32doc_filter.decide_indexable` の `reason`)ごとの件数。
    excluded_reasons: dict[str, int] = field(default_factory=dict)
    total_scanned: int = 0


def select_reference_targets(docs_dir: Path) -> ReferenceSelection:
    """`docs/knowledge/B32doc` を走査し、B32doc フィルタで索引対象を選ぶ。"""
    root = docs_dir / "knowledge" / "B32doc"
    if not root.is_dir():
        return ReferenceSelection(targets=(), excluded_reasons={}, total_scanned=0)

    targets: list[dict[str, Any]] = []
    excluded_reasons: dict[str, int] = {}
    total = 0

    for md_path in sorted(root.rglob("*.md")):
        if not md_path.is_file():
            continue
        total += 1
        rel = md_path.relative_to(docs_dir).as_posix()
        rel_posix = "/" + rel

        try:
            raw = md_path.read_text(encoding="utf-8")
        except OSError:
            excluded_reasons["read_error"] = excluded_reasons.get("read_error", 0) + 1
            continue

        parsed = parse_frontmatter(raw)
        category = parsed.data.get("category")
        language = parsed.data.get("language")
        source_name = parsed.data.get("source_name")

        decision = decide_indexable(
            rel_posix=rel_posix,
            source_name=source_name,
            category=category,
            language=language,
            body=parsed.body,
        )
        if not decision.indexable:
            reason = decision.reason or "unknown"
            excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
            continue

        summary = extract_summary_keys(parsed.body)
        targets.append(
            {
                "path": rel,
                "post_number": None,
                "title": summary.title,
                "source": "manual",
                "document_type": "reference",
                "status": "active",
                "url": None,
                "category": category,
            }
        )

    return ReferenceSelection(
        targets=tuple(targets), excluded_reasons=excluded_reasons, total_scanned=total
    )


__all__ = [
    "REFERENCE_SUBTREE",
    "WORK_SOURCES",
    "ReferenceSelection",
    "WorkSelection",
    "select_reference_targets",
    "select_work_targets",
]
