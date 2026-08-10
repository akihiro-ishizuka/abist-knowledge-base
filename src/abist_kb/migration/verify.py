"""`migrate verify`(設計書 §11.4)。manifest と移行先を突合して検証条件を確認する。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abist_kb.application.search_service import ProviderFactory, default_provider_factory
from abist_kb.migration.manifest import Manifest

_RECALL_TOLERANCE = 0.01
_CITATION_MIN_AGREEMENT = 0.95
_DEFAULT_SEARCH_QUALITY_METHOD = "hybrid"


def _citation_key(row: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (row.get("path"), row.get("start_line"), row.get("end_line"))


def _citation_agreement(
    baseline_per_query: list[dict[str, Any]],
    new_per_query: list[dict[str, Any]],
    method: str,
) -> float:
    """baseline/newそれぞれのクエリ単位 folded top5 引用行(path+行範囲)の一致率。

    baseline の引用行集合を分母(何を再現できているべきか)とし、new 側に
    同じ引用行が含まれる割合をクエリごとに算出してマクロ平均する。
    baseline 側に引用が無いクエリ(ゼロヒット)は分母0のため比較対象から除く。
    """
    new_by_id = {q["id"]: q for q in new_per_query}
    ratios: list[float] = []
    for base_q in baseline_per_query:
        base_result = base_q.get("results", {}).get(method)
        if base_result is None:
            continue
        base_keys = {_citation_key(r) for r in base_result.get("folded", [])[:5]}
        if not base_keys:
            continue
        new_q = new_by_id.get(base_q["id"])
        new_result = (new_q or {}).get("results", {}).get(method, {})
        new_keys = {_citation_key(r) for r in new_result.get("folded", [])[:5]}
        ratios.append(len(base_keys & new_keys) / len(base_keys))
    return (sum(ratios) / len(ratios)) if ratios else 0.0


def measure_search_quality(
    conn: sqlite3.Connection,
    docs_dir: Path,
    *,
    baseline: dict[str, Any],
    queries: list[dict[str, Any]],
    method: str = _DEFAULT_SEARCH_QUALITY_METHOD,
    embedding_provider_factory: ProviderFactory = default_provider_factory,
) -> tuple[float, float, float]:
    """移行先の索引DBに対して M4 のクエリ集合を実際に実行し、baseline と比較する。

    §11.4: Recall@5 は baseline との差分が `_RECALL_TOLERANCE` 以内、出典行一致率は
    `_CITATION_MIN_AGREEMENT` 以上。ここでは実測値そのもの
    (recall_before/recall_after/citation_agreement)を返すだけで、判定は
    `_check_search_quality()` が行う。
    """
    # ローカル import: application.audit は infrastructure.ai(埋め込みモデル)へ
    # 依存し、migration パッケージの他モジュールからは重い依存を持ち込みたくないため。
    from abist_kb.application.audit.search_quality import evaluate

    report = evaluate(
        conn,
        queries,
        docs_dir=docs_dir,
        methods=(method,),
        embedding_provider_factory=embedding_provider_factory,
    )
    recall_before = float(baseline["macro"][method]["recall5"])
    recall_after = float(report["macro"][method]["recall5"])
    citation_agreement = _citation_agreement(baseline["per_query"], report["per_query"], method)
    return recall_before, recall_after, citation_agreement


@dataclass(frozen=True, slots=True)
class ConditionResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class VerifyResult:
    conditions: tuple[ConditionResult, ...]

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.conditions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "conditions": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.conditions
            ],
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_markdown_bytes(manifest: Manifest, from_root: Path, to_root: Path) -> ConditionResult:
    step = manifest.steps.get("copy_docs")
    if step is None or step.status != "completed":
        return ConditionResult(
            "markdown_bytes", False, "copy_docs 工程が manifest に記録されていません。"
        )
    mismatches: list[str] = []
    missing: list[str] = []
    for relative_path, expected_sha in step.sha256.items():
        dest = to_root / relative_path
        if not dest.exists():
            missing.append(relative_path)
            continue
        if _sha256_file(dest) != expected_sha:
            mismatches.append(relative_path)
    if missing or mismatches:
        return ConditionResult(
            "markdown_bytes",
            False,
            f"欠落 {len(missing)} 件、ハッシュ不一致 {len(mismatches)} 件"
            f"(例: {(missing + mismatches)[:5]})",
        )
    return ConditionResult(
        "markdown_bytes", True, f"{len(step.sha256)} 件のパス集合とSHA-256が一致"
    )


def _check_excluded_have_reason(manifest: Manifest) -> ConditionResult:
    unexplained: list[str] = []
    for step in manifest.steps.values():
        for item in step.excluded:
            if not item.get("reason"):
                unexplained.append(item.get("path", "?"))
    if unexplained:
        return ConditionResult("excluded_have_reason", False, f"理由の無い除外: {unexplained[:5]}")
    return ConditionResult("excluded_have_reason", True, "全ての除外に理由が付与されている")


def _check_no_unfinished_steps(manifest: Manifest) -> ConditionResult:
    gaps = manifest.unexplained_gap()
    if gaps:
        return ConditionResult("no_unfinished_steps", False, f"未完了/失敗の工程: {gaps}")
    return ConditionResult("no_unfinished_steps", True, "全工程が completed")


def _check_sync_state_counts(
    manifest: Manifest, expected_by_source: dict[str, int] | None
) -> ConditionResult:
    step = manifest.steps.get("import_sync_state")
    if step is None:
        return ConditionResult(
            "sync_state_counts", False, "import_sync_state 工程が manifest に無い"
        )
    if expected_by_source is None:
        return ConditionResult(
            "sync_state_counts",
            True,
            "比較対象の source 別件数が未指定のため import 完了のみ確認"
            f"(imported={step.counts.get('imported', 0)})",
        )
    total_expected = sum(expected_by_source.values())
    if step.counts.get("imported", 0) != total_expected:
        return ConditionResult(
            "sync_state_counts",
            False,
            f"件数不一致: imported={step.counts.get('imported', 0)} expected={total_expected}",
        )
    return ConditionResult("sync_state_counts", True, "sync-state 件数が一致")


def _check_search_quality(
    recall_before: float | None,
    recall_after: float | None,
    citation_agreement: float | None,
) -> ConditionResult:
    if recall_before is None or recall_after is None or citation_agreement is None:
        return ConditionResult(
            "search_quality",
            False,
            "Recall@5比較・出典行一致率が未計測(旧評価クエリの実行結果を渡すこと)。"
            "manifest上「未実施」として明示し、検証未完了として扱う。",
        )
    recall_drop = recall_before - recall_after
    if recall_drop > _RECALL_TOLERANCE:
        return ConditionResult(
            "search_quality", False, f"Recall@5低下 {recall_drop:.4f} > {_RECALL_TOLERANCE}"
        )
    if citation_agreement < _CITATION_MIN_AGREEMENT:
        return ConditionResult(
            "search_quality",
            False,
            f"出典行一致率 {citation_agreement:.4f} < {_CITATION_MIN_AGREEMENT}",
        )
    return ConditionResult(
        "search_quality",
        True,
        f"Recall@5低下 {recall_drop:.4f}、出典行一致率 {citation_agreement:.4f}",
    )


def verify_migration(
    manifest: Manifest,
    from_root: Path,
    to_root: Path,
    *,
    expected_sync_by_source: dict[str, int] | None = None,
    recall_before: float | None = None,
    recall_after: float | None = None,
    citation_agreement: float | None = None,
    search_quality_conn: sqlite3.Connection | None = None,
    search_quality_docs_dir: Path | None = None,
    search_quality_baseline: dict[str, Any] | None = None,
    search_quality_queries: list[dict[str, Any]] | None = None,
    search_quality_method: str = _DEFAULT_SEARCH_QUALITY_METHOD,
    embedding_provider_factory: ProviderFactory = default_provider_factory,
) -> VerifyResult:
    """§11.4 の検証条件を manifest と移行先の実ファイルに対して確認する。

    `recall_before`/`recall_after`/`citation_agreement` を直接渡せば(呼び出し側で
    既に計測済みの場合)そのまま使う。渡されず、代わりに
    `search_quality_conn`/`search_quality_baseline`/`search_quality_queries` が
    揃っている場合は `measure_search_quality()` で実際に計測する
    (`tests/fixtures/eval/baseline.json` と M4 の評価クエリ集合を接続先の
    索引DBに対して実行し、Recall@5低下・出典行一致率を測る)。
    どちらも無ければ従来どおり「未計測」として fail-closed する。
    """
    if (
        recall_before is None
        and recall_after is None
        and citation_agreement is None
        and search_quality_conn is not None
        and search_quality_baseline is not None
        and search_quality_queries is not None
    ):
        recall_before, recall_after, citation_agreement = measure_search_quality(
            search_quality_conn,
            search_quality_docs_dir or to_root / "docs",
            baseline=search_quality_baseline,
            queries=search_quality_queries,
            method=search_quality_method,
            embedding_provider_factory=embedding_provider_factory,
        )

    conditions = [
        _check_markdown_bytes(manifest, from_root, to_root),
        _check_excluded_have_reason(manifest),
        _check_no_unfinished_steps(manifest),
        _check_sync_state_counts(manifest, expected_sync_by_source),
        _check_search_quality(recall_before, recall_after, citation_agreement),
    ]
    return VerifyResult(conditions=tuple(conditions))
