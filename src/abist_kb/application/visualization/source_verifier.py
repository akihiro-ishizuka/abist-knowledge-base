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
from abist_kb.domain.scene_spec import composition_reason

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


#: 不良出典で剪定しうる beat type(事実を述べうるもの)。
#: 新しく出典を要求するフィールドを追加したら、ここへの登録も必ず見直すこと。
#: `tests/visualize/test_source_verifier.py` の網羅テストが漏れを検出する。
_PRUNABLE_BEAT_TYPES = (
    "statement",
    "flow_step",
    "transition",
    "decision",
    "timeline_point",
    "comparison_item",
    "domain_entity",
    "domain_relation",
    # --- 動画専用カード（Phase 7）。事実を載せる beat は必ずここへ足す ---
    "quote",
    "code",
    "formula",
    "image",
    # グラフの数値。出典が食い違ったまま棒を描くと、根拠のない図が
    # 「出典つき」の顔をして出てしまう。
    "chart_series",
)

#: 「ノードを名乗る beat type」-> 「それを参照するエッジの beat type」。
#: ノードが出典不良で除外されたら、それを指すエッジも道連れにしないと
#: 接続先を失った矢印が黙って消え、警告なしに図が崩れる。
_NODE_TO_EDGE_TYPE: dict[str, str] = {
    "flow_step": "transition",
    "decision": "transition",
    "domain_entity": "domain_relation",
}

#: エッジ beat type -> 接続先を指すフィールド名。
_EDGE_ENDPOINT_KEYS: tuple[str, str] = ("from", "to")


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
    if beat["type"] == "decision":
        return f"decision「{beat.get('label')}」"
    if beat["type"] == "timeline_point":
        return f"timeline_point「{beat.get('at')} {beat.get('label')}」"
    if beat["type"] == "comparison_item":
        return f"comparison_item「{beat.get('aspect')} x {beat.get('side')}」"
    if beat["type"] == "domain_entity":
        return f"domain_entity「{beat.get('name')}」"
    if beat["type"] == "domain_relation":
        return f"domain_relation「{beat.get('from')} → {beat.get('to')}」"
    if beat["type"] == "transition":
        return f"transition「{beat.get('from')} → {beat.get('to')}」のラベル"
    if beat["type"] == "quote":
        return f"quote「{beat.get('text')}」"
    if beat["type"] == "code":
        return f"code「{str(beat.get('text', ''))[:40]}」"
    if beat["type"] == "formula":
        return f"formula「{beat.get('text')}」"
    if beat["type"] == "image":
        return f"image「{beat.get('path')}」"
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
        return VerifyResult(
            ok=False,
            code=code,
            errors=errors,
            warnings=warnings,
            # 失敗時も出典の判定結果を返す。カタログが「どの出典が壊れていたか」を
            # 記録できるようにするため(以前は成功時しか埋まらず捨てられていた)。
            source_status=source_status,
        )

    # 事実を述べる beat の不良出典は警告して除外する。
    #
    # ここが実ファイル照合の唯一の関門であり、`scene_spec` の構文検証(source_refs が
    # sources[].id に存在するか)では「出典ファイルが消えた・content_hash が変わった」
    # を捕まえられない。**出典を要求するフィールドを追加したら必ずここにも足すこと。**
    # 足し忘れると出典必須ポリシーがレンダリング時に迂回される。
    # エッジ beat type -> 除外されたノード名の集合。
    dropped_nodes: dict[str, set[str]] = {}
    beats: list[dict[str, Any]] = []
    for beat in spec["beats"]:
        if beat.get("decorative") is True:
            beats.append(beat)
            continue
        if beat["type"] not in _PRUNABLE_BEAT_TYPES:
            beats.append(beat)
            continue
        # transition は label を持つときだけ出典を要する(from/to は宣言済みノード
        # 同士の順序でしかない)。
        if beat["type"] == "transition" and beat.get("label") is None:
            beats.append(beat)
            continue
        bad = bad_refs_of(beat)
        if not bad:
            beats.append(beat)
            continue
        src = source_by_id[bad[0]]
        status = source_status[bad[0]]
        if beat["type"] == "transition":
            # 矢印そのものは出典と無関係なので残し、出典の無い主張(label)だけを外す。
            # 矢印ごと消すとグラフ構造が黙って壊れ、剪定後の構成制約まで割れうる。
            stripped = {k: v for k, v in beat.items() if k not in ("label", "source_refs")}
            beats.append(stripped)
            warnings.append(
                f"{_beat_label(beat)}は{_status_message(src, status)}。"
                "ラベルのみ描画から除外しました（矢印は残ります）"
            )
            continue
        warnings.append(
            f"{_beat_label(beat)}は{_status_message(src, status)}。描画から除外しました"
        )
        if beat["type"] in _NODE_TO_EDGE_TYPE:
            # 除外したノードを指すエッジは接続先を失うので連鎖除外する。
            # flow_step / decision は label、domain_entity は name で名乗る。
            node_name = beat.get("label") if "label" in beat else beat.get("name")
            if isinstance(node_name, str):
                dropped_nodes.setdefault(_NODE_TO_EDGE_TYPE[beat["type"]], set()).add(node_name)

    # 除外したノードを参照するエッジも連鎖除外する。
    if dropped_nodes:
        kept: list[dict[str, Any]] = []
        for beat in beats:
            names = dropped_nodes.get(beat["type"])
            if names is None:
                kept.append(beat)
                continue
            endpoints = [beat.get(key) for key in _EDGE_ENDPOINT_KEYS]
            if any(endpoint in names for endpoint in endpoints):
                warnings.append(
                    f"{beat['type']}「{endpoints[0]} → {endpoints[1]}」は除外済みの"
                    "ノードを参照するため描画から除外しました"
                )
                continue
            kept.append(beat)
        beats = kept

    # 剪定後の構成制約。規則の定義は domain.scene_spec.composition_reason に一本化
    # してあるので、kind を増やしてもここは変更不要。
    reason = composition_reason(spec["scene_kind"], beats)
    if reason is not None:
        return VerifyResult(
            ok=False,
            code="INVALID_SCENE_SPEC",
            errors=[
                VerifyError(
                    "beats",
                    "invalid",
                    f"出典検証の結果、描画可能な beat が残りません（{reason}）",
                )
            ],
            warnings=warnings,
            source_status=source_status,
        )

    new_spec = {**spec, "beats": beats}
    return VerifyResult(ok=True, spec=new_spec, warnings=warnings, source_status=source_status)


__all__ = ["SourceStatus", "VerifyError", "VerifyResult", "verify_sources"]
