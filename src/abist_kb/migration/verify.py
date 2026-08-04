"""`migrate verify`(設計書 §11.4)。manifest と移行先を突合して検証条件を確認する。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abist_kb.migration.manifest import Manifest

_RECALL_TOLERANCE = 0.01
_CITATION_MIN_AGREEMENT = 0.95


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
) -> VerifyResult:
    """§11.4 の検証条件を manifest と移行先の実ファイルに対して確認する。"""
    conditions = [
        _check_markdown_bytes(manifest, from_root, to_root),
        _check_excluded_have_reason(manifest),
        _check_no_unfinished_steps(manifest),
        _check_sync_state_counts(manifest, expected_sync_by_source),
        _check_search_quality(recall_before, recall_after, citation_agreement),
    ]
    return VerifyResult(conditions=tuple(conditions))


def write_verify_result(result: VerifyResult, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
