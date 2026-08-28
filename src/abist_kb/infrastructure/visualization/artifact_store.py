"""可視化の出力ディレクトリ命名・封じ込め・manifest 組み立て。

旧実装 `tools/lib/visualization-store.js` の移植。出力は常に
`reports/visualizations/<id>/` 配下に閉じる。id は
`<UTC-ts>-<slug>-<4hex>` 形式(`:` を含まない。Windows で使えないため)。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def visualizations_dir(reports_dir: Path) -> Path:
    """`reports_dir` から可視化成果物ディレクトリを導出する唯一の関数。

    規約: **`reports_dir` は常にベースの `reports/`**(`Settings.reports_dir` と同義)。
    以前は呼び出し側ごとに「ベースの reports/ か、visualizations まで含んだ値か」の
    解釈が割れており、`reports/visualizations/visualizations` を作りうる状態だった。
    導出をこの関数1つに集約して語義を固定する。
    """
    return reports_dir / "visualizations"


#: Windows で予約されているベース名(大文字小文字を問わない)
_WINDOWS_RESERVED = (
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_MAX_SLUG_LENGTH = 60


class OutputPathViolation(Exception):
    """出力先がベースディレクトリの外を指している場合。"""


def normalize_slug(value: Any, fallback: str = "scene") -> str:
    """ascii 小文字・数字・ハイフンのみの slug に正規化する(日本語のみなら fallback)。"""
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower())
    slug = re.sub(r"-+", "-", slug).strip("-")[:_MAX_SLUG_LENGTH].rstrip("-")
    if not slug:
        slug = fallback
    if slug in _WINDOWS_RESERVED:
        slug = f"{slug}-x"
    return slug


def _utc_stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def make_visualization_id(slug: str, *, now: datetime | None = None, random: Any = None) -> str:
    """`<UTC-ts>-<slug>-<4hex>`。"""
    now = now or datetime.now(UTC)
    hex_part = random() if random is not None else secrets.token_hex(2)
    return f"{_utc_stamp(now)}-{slug}-{hex_part}"


@dataclass(frozen=True, slots=True)
class CreatedDir:
    visualization_id: str
    dir: Path


def create_visualization_dir(
    base_dir: Path, slug: str, *, now: datetime | None = None, random: Any = None
) -> CreatedDir:
    """出力ディレクトリを作成する。同一秒の衝突は 4hex を引き直して最大 5 回リトライ。"""
    base_dir.mkdir(parents=True, exist_ok=True)
    for _attempt in range(5):
        visualization_id = make_visualization_id(slug, now=now, random=random)
        directory = base_dir / visualization_id
        try:
            directory.mkdir(parents=False, exist_ok=False)
            return CreatedDir(visualization_id=visualization_id, dir=directory)
        except FileExistsError:
            continue
    raise RuntimeError(f"出力ディレクトリを作成できません(id 衝突が続いています): {base_dir}")


def assert_inside_path(base_dir: Path, candidate: Path) -> None:
    """candidate が base_dir 配下でなければ OutputPathViolation を投げる。"""
    base_resolved = base_dir.resolve()
    candidate_resolved = candidate.resolve()
    message = f"出力先がベースディレクトリの外を指しています: {candidate}"
    try:
        rel = candidate_resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise OutputPathViolation(message) from exc
    if str(rel) == ".":
        raise OutputPathViolation(message)


def sha256_file(path: Path) -> str:
    """ファイル内容の sha256 hex(大きい成果物向けにストリームで計算)。"""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_manifest(
    *,
    spec: dict[str, Any],
    visualization_id: str,
    created_at: datetime,
    versions: dict[str, Any],
    spec_sha256: str,
    outputs: list[dict[str, Any]],
    warnings: list[str],
    render: dict[str, Any],
) -> dict[str, Any]:
    """manifest.json の中身を組み立てる(書き込みは呼び出し側)。"""
    return {
        "schema_version": spec["schema_version"],
        "visualization_id": visualization_id,
        "query": spec.get("query"),
        "scene_kind": spec["scene_kind"],
        "template": spec["template"],
        "output_format": spec["output_format"],
        "created_at": created_at.isoformat(),
        "generator": versions,
        "input": {"scene_spec_file": "scene-spec.json", "sha256": spec_sha256},
        "outputs": outputs,
        "sources": spec["sources"],
        "warnings": warnings,
        "render": render,
    }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "CreatedDir",
    "OutputPathViolation",
    "assert_inside_path",
    "build_manifest",
    "create_visualization_dir",
    "make_visualization_id",
    "normalize_slug",
    "sha256_file",
    "sha256_text",
]
