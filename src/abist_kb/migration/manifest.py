"""`migration-manifest.json`(設計書 §11.1/§11.3)の読み書きと再開判定。

各工程は `record_step` で件数・SHA-256・警告を記録する。**何も黙って
落とさない**: `plan` が除外と判定したファイルは必ず理由付きで
`excluded` として manifest に載る。`run` は同じ `input_hash` を持つ
完了済み工程をスキップして再開する。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StepRecord:
    """1工程ぶんの記録。"""

    name: str
    status: str  # "completed" | "failed"
    input_hash: str
    counts: dict[str, int] = field(default_factory=dict)
    sha256: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    """除外されたアイテム。各要素は最低限 `{"path": ..., "reason": ...}`。"""
    started_at: str = ""
    finished_at: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    """任意の工程固有データ(例: `.env` キー名一覧、埋め込み工程の進捗ログパス)。
    値そのもの(秘密情報等)を入れてはならない。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "input_hash": self.input_hash,
            "counts": self.counts,
            "sha256": self.sha256,
            "warnings": self.warnings,
            "excluded": self.excluded,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "details": self.details,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> StepRecord:
        return StepRecord(
            name=data["name"],
            status=data["status"],
            input_hash=data["input_hash"],
            counts=dict(data.get("counts", {})),
            sha256=dict(data.get("sha256", {})),
            warnings=list(data.get("warnings", [])),
            excluded=list(data.get("excluded", [])),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            details=dict(data.get("details", {})),
        )


@dataclass(slots=True)
class Manifest:
    """移行1回ぶんの manifest。工程名をキーに `StepRecord` を持つ。"""

    from_root: str
    to_root: str
    steps: dict[str, StepRecord] = field(default_factory=dict)
    swapped_in: bool = False

    def record_step(self, step: StepRecord) -> None:
        self.steps[step.name] = step

    def is_step_current(self, name: str, input_hash: str) -> bool:
        """同名工程が同じ `input_hash` で `completed` 済みなら再実行不要。"""
        existing = self.steps.get(name)
        return (
            existing is not None
            and existing.status == "completed"
            and existing.input_hash == input_hash
        )

    def unexplained_gap(self) -> list[str]:
        """failed のまま残っている工程名(=未説明の欠落の候補)。"""
        return [name for name, step in self.steps.items() if step.status != "completed"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_root": self.from_root,
            "to_root": self.to_root,
            "swapped_in": self.swapped_in,
            "steps": {name: step.to_dict() for name, step in self.steps.items()},
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Manifest:
        return Manifest(
            from_root=data["from_root"],
            to_root=data["to_root"],
            steps={
                name: StepRecord.from_dict(step) for name, step in data.get("steps", {}).items()
            },
            swapped_in=bool(data.get("swapped_in", False)),
        )


def load_manifest(path: Path) -> Manifest | None:
    if not path.exists():
        return None
    return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_manifest(manifest: Manifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def hash_inputs(*parts: str) -> str:
    """工程の再開判定に使う安定した入力ハッシュ。"""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
