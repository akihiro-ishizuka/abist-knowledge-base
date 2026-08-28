"""題材の指定（4経路）を共通の `ResolvedInput` へ解決する。

契約は `design/video-pipeline.md`。

**ディレクトリ配下の全件を使用必須にしない。** `docs/` には約 85,000 件の Markdown が
あり、ディレクトリによっては数百〜数万件を含む。ディレクトリは「そこから選抜する
候補集合」として扱い、**索引で絞り込んでから本文を読む**（全件を LLM へ渡さない）。

選抜は決定的。`(score 降順, path 昇順)` で並べるので、同じ入力なら常に同じ順序・
同じ文書が選ばれる。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from abist_kb.domain.line_range import range_hash
from abist_kb.domain.video_project_spec import (
    DEFAULT_MAX_DOCS_PER_DIRECTORY,
    DEFAULT_MAX_TOTAL_CANDIDATES,
    SELECTION_COLLECTION_CANDIDATE,
    SELECTION_EXPLICIT_PRIMARY,
    SELECTION_SUPPLEMENTAL,
    is_markdown_path,
    normalize_docs_path,
)

#: `selection` の強さ。同一 path が複数経路で解決したら強い方を採る。
_SELECTION_RANK = {
    SELECTION_EXPLICIT_PRIMARY: 3,
    SELECTION_COLLECTION_CANDIDATE: 2,
    SELECTION_SUPPLEMENTAL: 1,
}

#: esa の記事 URL から post_id を取り出す。
_ESA_POST_ID_RE = re.compile(r"/posts/(\d+)")


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    """解決後の統一形（経路が違ってもここから先は同じ）。"""

    path: str
    content_hash: str | None
    selection: str
    require_usage: bool
    origin: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CollectionReport:
    """ディレクトリごとの選抜記録（なぜこの文書が入ったかを後から追うため）。"""

    selector: str
    candidate_total: int
    selected_count: int
    truncated: bool
    selected: list[dict[str, Any]] = field(default_factory=list)
    excluded_sample: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResolveResult:
    ok: bool
    inputs: list[ResolvedInput] = field(default_factory=list)
    collections: list[CollectionReport] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "inputs": [i.to_dict() for i in self.inputs],
            "collections": [c.to_dict() for c in self.collections],
            "warnings": self.warnings,
        }


def _warning(code: str, path: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def _resolve_inside_docs(docs_dir: Path, relative: str) -> Path | None:
    """`docs/` の外へ出ないことを実パスで再確認する（シンボリックリンク対策）。"""
    candidate = (docs_dir / relative).resolve()
    try:
        candidate.relative_to(docs_dir.resolve())
    except (ValueError, OSError):
        return None
    return candidate


def _read_hash(docs_dir: Path, relative: str) -> str | None:
    """文書全体の `range_hash`（purring の `content_hash` と同じ計算）。"""
    absolute = _resolve_inside_docs(docs_dir, relative)
    if absolute is None or not absolute.is_file():
        return None
    try:
        text = absolute.read_text(encoding="utf-8")
    except OSError:
        return None
    total = text.count("\n") + (0 if text.endswith("\n") else 1)
    result = range_hash(text, 1, max(total, 1))
    return result.hash if result.ok else None


def _origin(
    origin_type: str,
    *,
    selector: str | None = None,
    esa_url: str | None = None,
    esa_post_id: int | None = None,
) -> dict[str, Any]:
    return {
        "type": origin_type,
        "selector": selector,
        "esa_url": esa_url,
        "esa_post_id": esa_post_id,
    }


def _score_paths(
    paths: list[str], queries: list[str], theme: str | None
) -> dict[str, tuple[float, str]]:
    """索引を使えない場合の決定的フォールバックスコア。

    パス文字列に検索語・テーマ語が含まれる数で粗く採点する。**本文は読まない。**
    同点は呼び出し側が `path` 昇順で解くので、結果は常に決定的。
    """
    terms: list[str] = []
    for q in queries:
        terms.extend(t for t in re.split(r"\s+", q) if t)
    if theme:
        terms.extend(t for t in re.split(r"\s+", theme) if t)
    scored: dict[str, tuple[float, str]] = {}
    for path in paths:
        lowered = path.lower()
        hits = sum(1 for t in terms if t and t.lower() in lowered)
        reason = "query_match" if hits else "theme_match"
        scored[path] = (float(hits), reason)
    return scored


def _collect_markdown(docs_dir: Path, selector: str) -> list[str]:
    """`selector` 配下の Markdown を `docs/` 相対で列挙する（決定的な順序）。"""
    root = _resolve_inside_docs(docs_dir, selector)
    if root is None or not root.is_dir():
        return []
    found: list[str] = []
    base = docs_dir.resolve()
    for entry in root.rglob("*"):
        if not entry.is_file() or not is_markdown_path(entry.name):
            continue
        try:
            relative = entry.resolve().relative_to(base).as_posix()
        except (ValueError, OSError):
            # docs/ の外を指すシンボリックリンクは無視する
            continue
        found.append(relative)
    return sorted(found)


def _search_paths(
    conn: sqlite3.Connection | None, query: str, limit: int, search_fn: Any | None
) -> list[str]:
    """検索で候補パスを得る。索引が使えなければ空を返す（呼び出し側でフォールバック）。"""
    if search_fn is None:
        return []
    try:
        hits = search_fn(query=query, limit=limit)
    except Exception:  # noqa: BLE001 - 索引不備で解決全体を落とさない
        return []
    paths: list[str] = []
    for hit in hits or []:
        path = hit.get("path") if isinstance(hit, dict) else getattr(hit, "path", None)
        if isinstance(path, str):
            normalized = normalize_docs_path(path)
            if normalized and is_markdown_path(normalized):
                paths.append(normalized)
    return paths


def _esa_lookup(
    conn: sqlite3.Connection | None, url: str | None, post_id: int | None
) -> str | None:
    """esa の URL / post_id から既存 Markdown のパスを逆引きする。"""
    if conn is None:
        return None
    patterns: list[str] = []
    if url:
        patterns.append(url.rstrip("/"))
        matched = _ESA_POST_ID_RE.search(url)
        if matched:
            patterns.append(f"%/posts/{matched.group(1)}")
    if post_id is not None:
        patterns.append(f"%/posts/{post_id}")
    for pattern in patterns:
        try:
            row = conn.execute(
                "SELECT path FROM documents WHERE url = ? OR url LIKE ? LIMIT 1",
                (pattern, pattern),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is not None:
            normalized = normalize_docs_path(row["path"] if "path" in row.keys() else row[0])  # noqa: SIM118
            if normalized:
                return normalized
    return None


def resolve_inputs(
    inputs: dict[str, Any],
    *,
    docs_dir: Path,
    conn: sqlite3.Connection | None = None,
    theme: str | None = None,
    search_fn: Any | None = None,
    max_docs_per_directory: int = DEFAULT_MAX_DOCS_PER_DIRECTORY,
    max_total_candidates: int = DEFAULT_MAX_TOTAL_CANDIDATES,
) -> ResolveResult:
    """4経路を `ResolvedInput` へ解決する。

    優先順位: `kb_paths` → `kb_directories` → `esa_posts` → `kb_queries`。
    同一 `path` は1件に畳み、`selection` は強い方（explicit_primary）を残す。
    """
    resolved: dict[str, ResolvedInput] = {}
    collections: list[CollectionReport] = []
    warnings: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    def add(candidate: ResolvedInput) -> None:
        existing = resolved.get(candidate.path)
        if existing is None or (
            _SELECTION_RANK[candidate.selection] > _SELECTION_RANK[existing.selection]
        ):
            resolved[candidate.path] = candidate

    query_strings: list[str] = []
    for raw_query in inputs.get("kb_queries") or []:
        if isinstance(raw_query, str):
            query_strings.append(raw_query)
        elif isinstance(raw_query, dict) and isinstance(raw_query.get("query"), str):
            query_strings.append(raw_query["query"])

    # 1. kb_paths（明示主入力）
    for raw in inputs.get("kb_paths") or []:
        normalized = normalize_docs_path(raw)
        if normalized is None or not is_markdown_path(normalized):
            errors.append(
                _warning("INVALID_INPUT_PATH", str(raw), "docs/ 配下の Markdown ではありません")
            )
            continue
        content_hash = _read_hash(docs_dir, normalized)
        if content_hash is None:
            errors.append(_warning("INVALID_INPUT_PATH", normalized, "docs/ 配下に見つかりません"))
            continue
        add(
            ResolvedInput(
                path=normalized,
                content_hash=content_hash,
                selection=SELECTION_EXPLICIT_PRIMARY,
                require_usage=True,
                origin=_origin("kb_path"),
            )
        )

    # 2. kb_directories（候補集合。索引で絞ってから本文を読む）
    total_selected = 0
    for raw in inputs.get("kb_directories") or []:
        normalized = normalize_docs_path(raw)
        if normalized is None:
            errors.append(
                _warning("INVALID_INPUT_PATH", str(raw), "docs/ 配下の相対パスではありません")
            )
            continue
        candidates = _collect_markdown(docs_dir, normalized)
        if not candidates:
            warnings.append(_warning("EMPTY_DIRECTORY", normalized, "配下に Markdown がありません"))
            collections.append(
                CollectionReport(
                    selector=normalized, candidate_total=0, selected_count=0, truncated=False
                )
            )
            continue

        scored = _score_paths(candidates, query_strings, theme)
        # 決定的: score 降順 -> path 昇順。同点は path の辞書順で一意に決まる。
        ordered = sorted(candidates, key=lambda p: (-scored[p][0], p))

        room = max(0, max_total_candidates - total_selected)
        limit = min(max_docs_per_directory, room)
        selected = ordered[:limit]
        excluded = ordered[limit:]
        truncated = bool(excluded)

        selected_records: list[dict[str, Any]] = []
        for path in selected:
            content_hash = _read_hash(docs_dir, path)
            if content_hash is None:
                continue
            add(
                ResolvedInput(
                    path=path,
                    content_hash=content_hash,
                    selection=SELECTION_COLLECTION_CANDIDATE,
                    require_usage=False,
                    origin=_origin("kb_directory", selector=normalized),
                )
            )
            selected_records.append(
                {"path": path, "score": scored[path][0], "reason": scored[path][1]}
            )
        total_selected += len(selected_records)

        excluded_reason = (
            "over_total_limit" if room < max_docs_per_directory else "over_directory_limit"
        )
        collections.append(
            CollectionReport(
                selector=normalized,
                candidate_total=len(candidates),
                selected_count=len(selected_records),
                truncated=truncated,
                selected=selected_records,
                excluded_sample=[{"path": p, "reason": excluded_reason} for p in excluded[:10]],
            )
        )
        if truncated:
            warnings.append(
                _warning(
                    "INPUT_COLLECTION_TRUNCATED",
                    normalized,
                    f"候補 {len(candidates)} 件のうち {len(selected_records)} 件を選抜しました",
                )
            )

    # 3. esa_posts（任意の補助。既存 Markdown の逆引き）
    for raw in inputs.get("esa_posts") or []:
        if not isinstance(raw, dict):
            continue
        url = raw.get("url") if isinstance(raw.get("url"), str) else None
        post_id = raw.get("post_id") if isinstance(raw.get("post_id"), int) else None
        found = _esa_lookup(conn, url, post_id)
        label = url or f"post_id={post_id}"
        if found is None:
            warnings.append(
                _warning(
                    "ESA_POST_NOT_IN_KB",
                    str(label),
                    "KB に取り込まれていません。kb-download で先に取り込んでください",
                )
            )
            continue
        content_hash = _read_hash(docs_dir, found)
        if content_hash is None:
            warnings.append(_warning("ESA_POST_NOT_IN_KB", found, "実ファイルが見つかりません"))
            continue
        add(
            ResolvedInput(
                path=found,
                content_hash=content_hash,
                selection=SELECTION_EXPLICIT_PRIMARY,
                require_usage=True,
                origin=_origin("esa_post", esa_url=url, esa_post_id=post_id),
            )
        )

    # 4. kb_queries（補完。使用は必須ではない）
    for query in query_strings:
        for path in _search_paths(conn, query, max_docs_per_directory, search_fn):
            if path in resolved:
                continue
            content_hash = _read_hash(docs_dir, path)
            if content_hash is None:
                continue
            add(
                ResolvedInput(
                    path=path,
                    content_hash=content_hash,
                    selection=SELECTION_SUPPLEMENTAL,
                    require_usage=False,
                    origin=_origin("kb_query", selector=query),
                )
            )

    if not resolved:
        errors.append(
            _warning(
                "NO_RESOLVABLE_INPUT",
                "inputs",
                "題材を1件も解決できませんでした（パス・ディレクトリ・検索条件を確認してください）",
            )
        )
        return ResolveResult(ok=False, collections=collections, warnings=warnings, errors=errors)

    ordered_inputs = sorted(
        resolved.values(), key=lambda r: (-_SELECTION_RANK[r.selection], r.path)
    )
    return ResolveResult(
        ok=True,
        inputs=ordered_inputs,
        collections=collections,
        warnings=warnings,
        errors=errors,
    )


__all__ = ["CollectionReport", "ResolveResult", "ResolvedInput", "resolve_inputs"]
