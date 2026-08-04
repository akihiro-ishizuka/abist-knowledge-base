"""SceneSpec(可視化仕様)1.0 の検証。

旧実装 `tools/lib/scene-spec.js` の移植。純粋関数のみ(fs には触らない)。
判別 union + フィールド横断の出典ポリシー(metric は出典必須等)を明快に書くため
手書きバリデータを使う。全フィールドで日本語の詳細エラー({path, code, message}
の配列)を返す。

出典ポリシー(検証はここ、実ファイルとの照合は
`abist_kb.application.visualization.source_verifier`):
  - metric(数値)は source_refs 必須
  - statement / flow_step の description は source_refs 必須。
    装飾テキストのみ decorative:true で免除(事実の創作を機械判定可能にする)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

#: list_scene_kinds とバリデータの単一ソース
SCENE_KINDS: list[dict[str, Any]] = [
    {
        "kind": "explain",
        "description": "複数ステップの解説アニメーション。statement / metric を順に提示する",
        "template": "step_explanation",
        "required": [
            "schema_version",
            "scene_kind",
            "output_format",
            "template",
            "title",
            "sources",
            "beats",
        ],
        "beat_types": ["statement", "metric", "transition"],
    },
    {
        "kind": "flow",
        "description": "処理・判断・データフロー図。flow_step と transition で流れを示す",
        "template": "data_flow_v1",
        "required": [
            "schema_version",
            "scene_kind",
            "output_format",
            "template",
            "title",
            "sources",
            "beats",
        ],
        "beat_types": ["flow_step", "transition", "statement", "metric"],
    },
]

#: スキーマ予約のみ(未実装)。指定されたら専用エラーで案内する
RESERVED_KINDS: list[str] = ["timeline", "comparison", "domain"]

OUTPUT_FORMATS: list[str] = ["mp4", "png"]

MAX_BEATS = 30
_SOURCE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SceneSpecError:
    """検証エラー1件。"""

    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class SceneSpecResult:
    """`validate_scene_spec` の戻り値。"""

    ok: bool
    spec: dict[str, Any] | None = None
    errors: list[SceneSpecError] = field(default_factory=list)


def _is_plain_object(v: Any) -> bool:
    return isinstance(v, dict)


def _is_safe_relative_path(p: Any) -> bool:
    """docs/ からの POSIX 相対パスのみ許可する。"""
    if not isinstance(p, str) or len(p) == 0:
        return False
    if "\\" in p:
        return False
    if p.startswith("/"):
        return False
    if re.match(r"^[a-zA-Z]:", p):
        return False
    return ".." not in p.split("/")


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int)


def _find_kind_info(scene_kind: Any) -> dict[str, Any] | None:
    for info in SCENE_KINDS:
        if info["kind"] == scene_kind:
            return info
    return None


def _validate_sources(sources: Any, errors: list[SceneSpecError]) -> set[str]:
    if not isinstance(sources, list):
        errors.append(SceneSpecError("sources", "invalid", "sources は配列で指定してください"))
        return set()
    ids: set[str] = set()
    for i, src in enumerate(sources):
        at = f"sources[{i}]"
        if not _is_plain_object(src):
            errors.append(SceneSpecError(at, "invalid", "出典はオブジェクトで指定してください"))
            continue
        src_id = src.get("id")
        if not isinstance(src_id, str) or not _SOURCE_ID_RE.match(src_id):
            errors.append(
                SceneSpecError(
                    f"{at}.id",
                    "invalid",
                    "id は英数字・ハイフン・アンダースコア 1〜32 文字で指定してください",
                )
            )
        elif src_id in ids:
            errors.append(
                SceneSpecError(f"{at}.id", "duplicate_id", f'id "{src_id}" が重複しています')
            )
        else:
            ids.add(src_id)

        if not _is_safe_relative_path(src.get("path")):
            errors.append(
                SceneSpecError(
                    f"{at}.path",
                    "invalid",
                    "path は docs/ からの相対 POSIX パスのみ指定できます"
                    '（".."・絶対パス・バックスラッシュ不可）',
                )
            )

        start_line = src.get("start_line")
        if not _is_integer(start_line) or start_line < 1:
            errors.append(
                SceneSpecError(
                    f"{at}.start_line", "invalid", "start_line は 1 以上の整数で指定してください"
                )
            )

        end_line = src.get("end_line")
        min_end = start_line if _is_integer(start_line) else 1
        if not _is_integer(end_line) or end_line < min_end:
            errors.append(
                SceneSpecError(
                    f"{at}.end_line",
                    "invalid",
                    "end_line は start_line 以上の整数で指定してください",
                )
            )

        content_hash = src.get("content_hash")
        if not isinstance(content_hash, str) or not _HASH_RE.match(content_hash):
            errors.append(
                SceneSpecError(
                    f"{at}.content_hash",
                    "invalid",
                    "content_hash は sha256 hex(64桁)で指定してください"
                    "(kb-search get_document の range_hash を使う)",
                )
            )

        heading_path = src.get("heading_path")
        if heading_path is not None and not (
            isinstance(heading_path, list) and all(isinstance(h, str) for h in heading_path)
        ):
            errors.append(
                SceneSpecError(
                    f"{at}.heading_path", "invalid", "heading_path は文字列の配列で指定してください"
                )
            )
    return ids


def _validate_source_refs(
    refs: Any,
    at: str,
    source_ids: set[str],
    errors: list[SceneSpecError],
    *,
    required: bool,
    require_hint: str | None = None,
) -> None:
    if refs is None or (isinstance(refs, list) and len(refs) == 0):
        if required:
            errors.append(
                SceneSpecError(
                    f"{at}.source_refs",
                    "missing_source_refs",
                    require_hint or "出典(source_refs)が必要です",
                )
            )
        return
    if not isinstance(refs, list):
        errors.append(
            SceneSpecError(
                f"{at}.source_refs",
                "invalid",
                "source_refs は sources[].id の配列で指定してください",
            )
        )
        return
    for j, ref in enumerate(refs):
        if not isinstance(ref, str) or ref not in source_ids:
            errors.append(
                SceneSpecError(
                    f"{at}.source_refs[{j}]",
                    "unknown_source_ref",
                    f'source_refs "{ref}" に対応する出典が sources にありません',
                )
            )


def _validate_beats(
    spec: dict[str, Any],
    kind_info: dict[str, Any],
    source_ids: set[str],
    errors: list[SceneSpecError],
) -> None:
    beats = spec.get("beats")
    if not isinstance(beats, list) or len(beats) < 1 or len(beats) > MAX_BEATS:
        errors.append(
            SceneSpecError(
                "beats", "invalid", f"beats は 1〜{MAX_BEATS} 件の配列で指定してください"
            )
        )
        return

    flow_labels = {
        b["label"]
        for b in beats
        if _is_plain_object(b) and b.get("type") == "flow_step" and isinstance(b.get("label"), str)
    }

    for i, beat in enumerate(beats):
        at = f"beats[{i}]"
        if not _is_plain_object(beat):
            errors.append(SceneSpecError(at, "invalid", "beat はオブジェクトで指定してください"))
            continue
        beat_type = beat.get("type")
        if beat_type not in kind_info["beat_types"]:
            errors.append(
                SceneSpecError(
                    f"{at}.type",
                    "unknown_beat_type",
                    f'scene_kind "{kind_info["kind"]}" で使える beat type は '
                    f"{' / '.join(kind_info['beat_types'])} です(指定: {beat_type})",
                )
            )
            continue

        if beat_type == "statement":
            text = beat.get("text")
            if not isinstance(text, str) or not (1 <= len(text) <= 200):
                errors.append(
                    SceneSpecError(f"{at}.text", "invalid", "text は 1〜200 文字で指定してください")
                )
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=beat.get("decorative") is not True,
                require_hint=(
                    "事実を述べる statement には出典(source_refs)が必要です。"
                    "装飾テキストなら decorative:true を付けてください"
                ),
            )
        elif beat_type == "metric":
            label = beat.get("label")
            if not isinstance(label, str) or len(label) < 1:
                errors.append(SceneSpecError(f"{at}.label", "invalid", "label を指定してください"))
            value = beat.get("value")
            if not isinstance(value, str | int | float) or isinstance(value, bool):
                errors.append(
                    SceneSpecError(
                        f"{at}.value", "invalid", "value は数値または文字列で指定してください"
                    )
                )
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=True,
                require_hint=(
                    "数値(metric)には出典(source_refs)が必須です。出典のない数値は描画できません"
                ),
            )
        elif beat_type == "transition":
            for key in ("from", "to"):
                value = beat.get(key)
                if not isinstance(value, str) or len(value) < 1:
                    errors.append(
                        SceneSpecError(f"{at}.{key}", "invalid", f"{key} を指定してください")
                    )
                elif kind_info["kind"] == "flow" and value not in flow_labels:
                    errors.append(
                        SceneSpecError(
                            f"{at}.{key}",
                            "unknown_label",
                            f'transition の {key} "{value}" に一致する'
                            "flow_step の label がありません",
                        )
                    )
        elif beat_type == "flow_step":
            label = beat.get("label")
            if not isinstance(label, str) or len(label) < 1:
                errors.append(SceneSpecError(f"{at}.label", "invalid", "label を指定してください"))
            description = beat.get("description")
            if description is not None:
                if not isinstance(description, str) or len(description) > 200:
                    errors.append(
                        SceneSpecError(
                            f"{at}.description",
                            "invalid",
                            "description は 200 文字以内の文字列で指定してください",
                        )
                    )
                _validate_source_refs(
                    beat.get("source_refs"),
                    at,
                    source_ids,
                    errors,
                    required=beat.get("decorative") is not True,
                    require_hint=(
                        "description で事実を述べる flow_step には出典(source_refs)が必要です。"
                        "装飾なら decorative:true を付けてください"
                    ),
                )
            else:
                _validate_source_refs(
                    beat.get("source_refs"), at, source_ids, errors, required=False
                )

    # kind ごとの構成制約
    if kind_info["kind"] == "explain":
        substantive = [
            b for b in beats if _is_plain_object(b) and b.get("type") in ("statement", "metric")
        ]
        if len(substantive) < 1:
            errors.append(
                SceneSpecError(
                    "beats",
                    "invalid",
                    "explain には statement または metric の beat が 1 件以上必要です",
                )
            )
    if kind_info["kind"] == "flow":
        steps = [b for b in beats if _is_plain_object(b) and b.get("type") == "flow_step"]
        if len(steps) < 2:
            errors.append(
                SceneSpecError(
                    "beats", "invalid", "flow には flow_step の beat が 2 件以上必要です"
                )
            )


def validate_scene_spec(spec: Any) -> SceneSpecResult:
    """SceneSpec を検証する。"""
    if not _is_plain_object(spec):
        return SceneSpecResult(
            ok=False,
            errors=[
                SceneSpecError("", "invalid", "SceneSpec は JSON オブジェクトで指定してください")
            ],
        )
    errors: list[SceneSpecError] = []

    if spec.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            SceneSpecError(
                "schema_version",
                "invalid",
                f'schema_version は "{SCHEMA_VERSION}" を指定してください',
            )
        )

    kind_info = _find_kind_info(spec.get("scene_kind"))
    if kind_info is None:
        scene_kind = spec.get("scene_kind")
        if scene_kind in RESERVED_KINDS:
            errors.append(
                SceneSpecError(
                    "scene_kind",
                    "reserved_kind",
                    f'scene_kind "{scene_kind}" は予約済みで未実装です。現在使えるのは '
                    f"{' / '.join(k['kind'] for k in SCENE_KINDS)} です",
                )
            )
        else:
            errors.append(
                SceneSpecError(
                    "scene_kind",
                    "unknown_kind",
                    "scene_kind は "
                    f"{' / '.join(k['kind'] for k in SCENE_KINDS)} のいずれかを指定してください",
                )
            )

    if spec.get("output_format") not in OUTPUT_FORMATS:
        errors.append(
            SceneSpecError(
                "output_format",
                "invalid",
                f"output_format は {' / '.join(OUTPUT_FORMATS)} のいずれかを指定してください",
            )
        )
    title = spec.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= 80):
        errors.append(SceneSpecError("title", "invalid", "title は 1〜80 文字で指定してください"))
    query = spec.get("query")
    if query is not None and not isinstance(query, str):
        errors.append(SceneSpecError("query", "invalid", "query は文字列で指定してください"))
    font = spec.get("font")
    if font is not None and (not isinstance(font, str) or len(font) < 1):
        errors.append(
            SceneSpecError("font", "invalid", "font はフォント名の文字列で指定してください")
        )

    if kind_info is not None and spec.get("template") != kind_info["template"]:
        errors.append(
            SceneSpecError(
                "template",
                "template_mismatch",
                f'scene_kind "{kind_info["kind"]}" の template は "{kind_info["template"]}" を'
                "指定してください",
            )
        )

    source_ids = _validate_sources(spec.get("sources"), errors)
    if kind_info is not None:
        _validate_beats(spec, kind_info, source_ids, errors)

    if errors:
        return SceneSpecResult(ok=False, errors=errors)
    return SceneSpecResult(ok=True, spec=spec)


__all__ = [
    "MAX_BEATS",
    "OUTPUT_FORMATS",
    "RESERVED_KINDS",
    "SCENE_KINDS",
    "SCHEMA_VERSION",
    "SceneSpecError",
    "SceneSpecResult",
    "validate_scene_spec",
]
