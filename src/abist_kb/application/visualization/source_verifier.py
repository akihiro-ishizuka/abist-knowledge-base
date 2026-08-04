"""描画開始時の出典検証とビート剪定。

旧実装 `tools/lib/source-verifier.js` の移植。SceneSpec の `sources[]` を
docs/ の現在の内容と照合し、出典ポリシーを適用する:

  - metric(数値)が不良出典を参照 → 即中断(SOURCE_NOT_FOUND / SOURCE_HASH_MISMATCH)。
    根拠のない数値は決して描画しない
  - statement / flow_step の description が不良出典 → 警告を積んで beat を除外。
    flow_step を除外したら、その label を参照する transition も連鎖除外
  - decorative:true の beat は検証対象外
  - 剪定後に kind の構成制約(explain の実質 beat / flow のステップ数)を
    満たさなくなったら INVALID_SCENE_SPEC

content_hash の照合は `domain.line_range.range_hash`(= kb-search get_document の
range_hash と同一の正規化)で行う。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from abist_kb.domain.line_range import range_hash

SourceStatus = Literal["ok", "not_found", "hash_mismatch"]


@dataclass(frozen=True, slots=True)
class VerifyError:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    spec: dict[str, Any] | None = None
    code: str | None = None
    errors: list[VerifyError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_status: dict[str, SourceStatus] = field(default_factory=dict)


def _resolve_inside_docs(docs_dir: Path, relative_path: str) -> Path | None:
    """docs 配下に安全解決できなければ None(逸脱は not_found 扱いにする多層防御)。"""
    docs_dir = docs_dir.resolve()
    absolute = (docs_dir / relative_path).resolve()
    try:
        absolute.relative_to(docs_dir)
    except ValueError:
        return None
    return absolute


def _check_source(docs_dir: Path, src: dict[str, Any]) -> SourceStatus:
    absolute = _resolve_inside_docs(docs_dir, src["path"])
    if absolute is None:
        return "not_found"
    try:
        content = absolute.read_text(encoding="utf-8")
    except OSError:
        return "not_found"
    result = range_hash(content, src["start_line"], src["end_line"])
    if not result.ok or result.hash != src["content_hash"]:
        return "hash_mismatch"
    return "ok"


def _status_message(src: dict[str, Any], status: SourceStatus) -> str:
    if status == "not_found":
        return f'出典 "{src["id"]}"（{src["path"]}）が見つかりません'
    return (
        f'出典 "{src["id"]}"（{src["path"]}:{src["start_line"]}-{src["end_line"]}）の内容が'
        "変わっています（content_hash 不一致）。kb-search で再取得してください"
    )


def _beat_label(beat: dict[str, Any]) -> str:
    if beat["type"] == "statement":
        return f"statement「{beat.get('text')}」"
    if beat["type"] == "flow_step":
        return f"flow_step「{beat.get('label')}」"
    return str(beat["type"])


def verify_sources(spec: dict[str, Any], docs_dir: Path) -> VerifyResult:
    """出典を検証し、必要ならビートを剪定する。"""
    source_status: dict[str, SourceStatus] = {
        src["id"]: _check_source(docs_dir, src) for src in spec["sources"]
    }
    source_by_id = {s["id"]: s for s in spec["sources"]}

    def bad_refs_of(beat: dict[str, Any]) -> list[str]:
        return [rid for rid in beat.get("source_refs") or [] if source_status.get(rid) != "ok"]

    warnings: list[str] = []
    errors: list[VerifyError] = []

    # metric の不良出典は即中断(not_found を優先して報告する)
    for i, beat in enumerate(spec["beats"]):
        if beat.get("type") != "metric" or beat.get("decorative") is True:
            continue
        for rid in bad_refs_of(beat):
            src = source_by_id[rid]
            status = source_status[rid]
            errors.append(
                VerifyError(
                    f"beats[{i}].source_refs",
                    status,
                    f"metric「{beat.get('label')}」の{_status_message(src, status)}",
                )
            )
    if errors:
        code = (
            "SOURCE_NOT_FOUND"
            if any(e.code == "not_found" for e in errors)
            else "SOURCE_HASH_MISMATCH"
        )
        return VerifyResult(ok=False, code=code, errors=errors, warnings=warnings)

    # statement / flow_step description の不良出典は警告して除外
    dropped_flow_labels: set[str] = set()
    beats: list[dict[str, Any]] = []
    for beat in spec["beats"]:
        if beat.get("decorative") is True:
            beats.append(beat)
            continue
        if beat["type"] not in ("statement", "flow_step"):
            beats.append(beat)
            continue
        bad = bad_refs_of(beat)
        if not bad:
            beats.append(beat)
            continue
        src = source_by_id[bad[0]]
        status = source_status[bad[0]]
        warnings.append(
            f"{_beat_label(beat)}は{_status_message(src, status)}。描画から除外しました"
        )
        if beat["type"] == "flow_step":
            dropped_flow_labels.add(beat["label"])

    # 除外した flow_step を参照する transition も連鎖除外
    if dropped_flow_labels:
        kept: list[dict[str, Any]] = []
        for beat in beats:
            if beat["type"] != "transition":
                kept.append(beat)
                continue
            if beat.get("from") in dropped_flow_labels or beat.get("to") in dropped_flow_labels:
                warnings.append(
                    f"transition「{beat.get('from')} → {beat.get('to')}」は除外済みの flow_step を"
                    "参照するため描画から除外しました"
                )
                continue
            kept.append(beat)
        beats = kept

    # 剪定後の構成制約
    if spec["scene_kind"] == "explain":
        substantive = [b for b in beats if b["type"] in ("statement", "metric")]
        if len(substantive) < 1:
            return VerifyResult(
                ok=False,
                code="INVALID_SCENE_SPEC",
                errors=[
                    VerifyError(
                        "beats",
                        "invalid",
                        "出典検証の結果、描画可能な beat が残りません"
                        "（statement / metric が 0 件）",
                    )
                ],
                warnings=warnings,
            )
    if spec["scene_kind"] == "flow":
        steps = [b for b in beats if b["type"] == "flow_step"]
        if len(steps) < 2:
            return VerifyResult(
                ok=False,
                code="INVALID_SCENE_SPEC",
                errors=[
                    VerifyError(
                        "beats",
                        "invalid",
                        "出典検証の結果、描画可能な flow_step が 2 件未満になり残りません",
                    )
                ],
                warnings=warnings,
            )

    new_spec = {**spec, "beats": beats}
    return VerifyResult(ok=True, spec=new_spec, warnings=warnings, source_status=source_status)


__all__ = ["SourceStatus", "VerifyError", "VerifyResult", "verify_sources"]
