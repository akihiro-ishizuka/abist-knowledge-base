"""`migrate plan`(設計書 §11.3)。ファイル単位で copy/convert/regenerate/exclude を確定する。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.migration.inventory import (
    InspectReport,
    MarkdownCandidate,
    resolve_batch_config_path,
)

PlanAction = Literal["copy", "convert", "regenerate", "exclude"]


@dataclass(frozen=True, slots=True)
class PlanItem:
    relative_path: str
    action: PlanAction
    reason: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    from_root: str
    to_root: str
    items: tuple[PlanItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_root": self.from_root,
            "to_root": self.to_root,
            "items": [
                {"path": item.relative_path, "action": item.action, "reason": item.reason}
                for item in self.items
            ],
            "summary": self.summary(),
        }

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.action] = counts.get(item.action, 0) + 1
        return counts

    def excluded(self) -> tuple[PlanItem, ...]:
        return tuple(item for item in self.items if item.action == "exclude")


def assert_safe_roots(from_root: Path, to_root: Path) -> None:
    """移行元と移行先が同一・親子関係なら拒否する(§11.3)。"""
    from_resolved = from_root.resolve()
    to_resolved = to_root.resolve()
    if from_resolved == to_resolved:
        raise AppError(
            ErrorCode.MIGRATION_FAILED,
            "--from と --to が同一のディレクトリです。",
            hint="移行先には空の別ディレクトリを指定してください。",
        )
    is_parent_child = from_resolved in to_resolved.parents or to_resolved in from_resolved.parents
    if is_parent_child:
        raise AppError(
            ErrorCode.MIGRATION_FAILED,
            "--from と --to が親子関係にあります。",
            hint="移行元・移行先は独立したディレクトリにしてください。",
        )


def _plan_for_candidate(candidate: MarkdownCandidate) -> PlanItem:
    if candidate.encoding_issue:
        return PlanItem(
            candidate.relative_path,
            "exclude",
            "UTF-8として読めない、または文字化け(U+FFFD)を検出したため除外",
        )
    if candidate.frontmatter_broken:
        return PlanItem(
            candidate.relative_path,
            "exclude",
            "front matter の開始区切り '---' はあるが閉じ区切りが見つからないため除外",
        )
    return PlanItem(candidate.relative_path, "copy", "バイト保持コピー")


def build_plan(report: InspectReport, from_root: Path, to_root: Path) -> MigrationPlan:
    """`InspectReport` からファイル単位の移行計画を確定する。

    棚卸しはファイルシステムを正としているため(§4)、`docs/` 内外を問わず
    走査で見つかった Markdown 全件が計画対象になる。DB 未登録の孤児
    (`report.orphan_paths`)も自動的に `copy` として拾われる ——
    除外されるのは文字化け・front matter 破損など個別の理由がある場合のみ。
    """
    assert_safe_roots(from_root, to_root)
    items = [_plan_for_candidate(c) for c in report.markdown_candidates]
    batch_config_relative = resolve_batch_config_path(from_root).relative_to(from_root).as_posix()
    if report.batch_config_error:
        items.append(
            PlanItem(
                batch_config_relative,
                "exclude",
                f"UNSUPPORTED_BATCH_CONFIG: {report.batch_config_error}",
            )
        )
    else:
        items.append(PlanItem(batch_config_relative, "convert", "M2 リテラルパーサで import"))
    if report.sync_state is not None:
        items.append(
            PlanItem("data/sync-state.sqlite", "convert", "列単位で documents テーブルへ import")
        )
    if report.reference_index is not None:
        items.append(
            PlanItem(
                "data/reference-index.sqlite",
                "regenerate",
                "索引は Markdown から再構築するため参照専用(§11.2)",
            )
        )
    items.append(
        PlanItem(
            "embeddings",
            "regenerate",
            "§11.2 互換ゲート不合格(0.99253<0.999)のため全件再生成",
        )
    )
    return MigrationPlan(from_root=str(from_root), to_root=str(to_root), items=tuple(items))


def write_plan(plan: MigrationPlan, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_plan(path: Path) -> MigrationPlan:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = tuple(PlanItem(item["path"], item["action"], item["reason"]) for item in data["items"])
    return MigrationPlan(from_root=data["from_root"], to_root=data["to_root"], items=items)
