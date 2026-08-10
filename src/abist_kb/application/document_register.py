"""手置き Markdown の `documents` 台帳への登録(`abist-kb document register-disk`)。

**なぜ必要か**: `docs/` に `.md` を置いただけでは検索できるようにならない。
`index build`(work コーパス)の対象は
[`infrastructure.search.corpus.select_work_targets`] が `documents` テーブルの
行から選ぶものだけであり、テーブルに無いファイルは索引されずに
`disk_only_paths` の警告として報告されるだけで終わる。この橋渡しが無いと、
新鮮なクローンに手で置いた文書は永久に検索へ載らない。

`application.audit.backfill_metadata` とは向きが逆の操作である。あちらは
`documents` の値を frontmatter へ書き戻す(DB → ファイル)。こちらはディスク上の
ファイルを `documents` へ登録する(ファイル → DB)。**どちらもファイルの本文には
触れない**(このモジュールは `docs/` を読むだけで一切書き込まない)。

安全側の既定:

- **既定は dry-run**。`apply=True` を明示したときだけ `documents` へ書く。
- **既に `documents` にあるパスは触らない**。esa / web / git 同期が管理している
  行を、手置き扱いの分類結果で上書きしてしまわないため。
- **参照コーパス配下は対象外**(`REFERENCE_CORPUS_PREFIXES`)。B32doc は
  `select_reference_targets` がディスクを直接走査する別スコープであり、
  catiadoc / generated は `classify_document` が `document_type=reference` と
  分類する。これらを登録しても work 索引には載らない(`select_work_targets` が
  `document_type == "reference"` を除外する)一方で、`documents` に行ができた
  ことで `disk_only_paths`(`tests/fixtures/PROVENANCE.md` §4 が要求する
  「黙って落とさない」報告)からは消えてしまう。inert な行と引き換えに診断を
  失う取引になるため、登録せず報告対象のまま残す。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from abist_kb.application.document_service import DocumentService
from abist_kb.domain.frontmatter import hash_body, parse_frontmatter
from abist_kb.domain.metadata_schema import (
    REFERENCE_CORPUS_PREFIXES,
    DocumentType,
    classify_document,
)

_H1_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.MULTILINE)


def _is_excluded(relative_posix: str) -> bool:
    """参照コーパス配下か(走査そのものから外す)。"""
    return any(
        relative_posix == prefix or relative_posix.startswith(prefix + "/")
        for prefix in REFERENCE_CORPUS_PREFIXES
    )


def _iter_markdown(docs_dir: Path) -> list[tuple[str, Path]]:
    """`docs_dir` 配下の `.md` を(docs相対posixパス, 実パス)の昇順一覧で返す。"""
    if not docs_dir.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for candidate in docs_dir.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() != ".md":
            continue
        relative = candidate.relative_to(docs_dir).as_posix()
        if _is_excluded(relative):
            continue
        found.append((relative, candidate))
    return sorted(found, key=lambda item: item[0])


def _resolve_title(frontmatter: dict[str, Any], body: str, file_path: Path) -> str:
    """title は frontmatter > 本文の最初の H1 > ファイル名(拡張子なし)の順で決める。

    `documents.title` は FTS の重み付け(`index_schema` の bm25 で最も重い列)と
    検索結果の表示に使われるため、`NULL` のまま登録すると手置き文書だけが
    タイトル一致で当たらない検索結果になる。
    """
    declared = frontmatter.get("title")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    heading = _H1_RE.search(body)
    if heading:
        return heading.group(1).strip()
    return file_path.stem


def _post_number(frontmatter: dict[str, Any]) -> int | None:
    value = frontmatter.get("post_number")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def _optional_str(frontmatter: dict[str, Any], key: str) -> str | None:
    value = frontmatter.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def register_disk_documents(
    service: DocumentService, docs_dir: Path, *, apply: bool = False
) -> dict[str, Any]:
    """`docs_dir` 配下の未登録 Markdown を `documents` へ登録する。

    戻り値は `json.dumps` 可能で、リストは docs 相対パスの昇順。
    `apply=False`(既定)のときは DB を一切変更せず、登録される予定のパスだけを返す。
    """
    known_paths = {document["path"] for document in service.list()}

    registered: list[str] = []
    skipped_existing = 0
    skipped_reference: list[str] = []
    unreadable: list[str] = []
    scanned = 0

    for relative, file_path in _iter_markdown(docs_dir):
        scanned += 1
        if relative in known_paths:
            skipped_existing += 1
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable.append(relative)
            continue

        parsed = parse_frontmatter(content)
        classification = classify_document(relative_path=relative, frontmatter=parsed.data)
        if classification.document_type == DocumentType.REFERENCE.value:
            # frontmatter で reference を宣言した文書(パス由来の除外は走査時に済み)。
            skipped_reference.append(relative)
            continue

        if apply:
            service.upsert(
                {
                    "path": relative,
                    "source": classification.source,
                    "managed_by": classification.managed_by,
                    "document_type": classification.document_type,
                    "status": classification.status,
                    "title": _resolve_title(parsed.data, parsed.body, file_path),
                    "category": _optional_str(parsed.data, "category"),
                    "url": _optional_str(parsed.data, "url"),
                    "post_number": _post_number(parsed.data),
                    "local_content_hash": hash_body(content),
                }
            )
        registered.append(relative)

    return {
        "docs_dir": str(docs_dir),
        "applied": apply,
        "scanned": scanned,
        "registered": len(registered),
        "skipped_existing": skipped_existing,
        "skipped_reference": len(skipped_reference),
        "skipped_unreadable": len(unreadable),
        "paths": registered,
        "skipped_reference_paths": skipped_reference,
        "skipped_unreadable_paths": unreadable,
        "excluded_prefixes": list(REFERENCE_CORPUS_PREFIXES),
    }


__all__ = ["register_disk_documents"]
