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

#: 動画専用カードの必須フィールド（既存 kind と同じ集合。重複を避けて共有する）。
_VIDEO_CARD_REQUIRED = [
    "schema_version",
    "scene_kind",
    "output_format",
    "template",
    "title",
    "sources",
    "beats",
]

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
        "description": (
            "処理・判断・データフロー図。flow_step と transition で流れを示す。"
            "decision（ひし形の条件分岐）と transition.label（矢印ラベル）も使える"
        ),
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
        "beat_types": ["flow_step", "transition", "statement", "metric", "decision"],
    },
    {
        "kind": "timeline",
        "description": (
            "時系列の図解。決定事項や経緯を timeline_point で上から順に並べる。"
            "at は原文の表記のまま書いてよい（日付として解釈しない）"
        ),
        "template": "timeline_v1",
        "required": [
            "schema_version",
            "scene_kind",
            "output_format",
            "template",
            "title",
            "sources",
            "beats",
        ],
        "beat_types": ["timeline_point", "statement", "metric"],
    },
    {
        "kind": "comparison",
        "description": (
            "観点 x 対象のマトリクス比較。現行版/次期版、案A/案B、Before/After を"
            "comparison_item（side=列見出し・aspect=行見出し・text=セル）で表す。"
            "該当のない組み合わせは書かなくてよい（表では「—」として描かれる）"
        ),
        "template": "comparison_v1",
        "required": [
            "schema_version",
            "scene_kind",
            "output_format",
            "template",
            "title",
            "sources",
            "beats",
        ],
        "beat_types": ["comparison_item", "statement", "metric"],
    },
    {
        "kind": "domain",
        "description": (
            "システム構成・用語間の関係図。domain_entity（要素、group で括れる）と "
            "domain_relation（関係名つきの矢印）で静的な構造を示す。"
            "時間的な流れ（順序に意味があり矢印にラベルが無い）は flow を使うこと"
        ),
        "template": "domain_map_v1",
        "required": [
            "schema_version",
            "scene_kind",
            "output_format",
            "template",
            "title",
            "sources",
            "beats",
        ],
        "beat_types": ["domain_entity", "domain_relation", "statement", "metric"],
    },
    # --- 動画専用（Phase 7）。単体でも描けるが、主用途は章立て動画の構成要素 ---
    {
        "kind": "title",
        "description": "動画の表紙。大見出しと補足を出す。事実を述べない行は decorative:true",
        "template": "title_card",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["statement"],
    },
    {
        "kind": "chapter",
        "description": (
            "章扉。章タイトルとねらいを出す。chapter_index / chapter_total を"
            "付けると「第 N 章 / 全 M 章」を表示する"
        ),
        "template": "chapter_card",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["statement"],
    },
    {
        "kind": "key_points",
        "description": "同格の要点を並べる箇条書き。順を追う説明は explain を使うこと",
        "template": "key_points",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["statement", "metric"],
    },
    {
        "kind": "quote",
        "description": (
            "原文の引用。quote は言い換えでも要約でもないため出典必須で、"
            "decorative は使えない。attribution に出所の呼び名を書ける"
        ),
        "template": "quote_card",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["quote"],
    },
    {
        "kind": "summary",
        "description": "まとめ。結論をチェックマーク付きで並べる（動画の締め）",
        "template": "summary_card",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["statement", "metric"],
    },
    {
        "kind": "cta",
        "description": "次の行動の案内（社内導線）。外部公開向けの誘導は扱わない",
        "template": "cta_card",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["statement"],
    },
    {
        "kind": "ending",
        "description": "エンドカード。出典一覧と社内限定の注意書きを最後に出す",
        "template": "ending_card",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["statement"],
    },
    {
        "kind": "code",
        "description": (
            "コード片の提示。code beat に原文をそのまま入れる（language は任意）。"
            "行数・桁数の上限を超える分はテンプレート側で省略記号にする"
        ),
        "template": "code_block",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["code"],
    },
    {
        "kind": "formula",
        "description": "数式・計算式の提示。LaTeX は使わず Unicode 記号で近似表示する",
        "template": "formula_block",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["formula"],
    },
    {
        "kind": "image",
        "description": (
            "静止画（アプリ画面キャプチャなど）の提示。path は scene-spec.json と"
            "同じディレクトリからの相対パス。画像が無い場合はプレースホルダで描く"
        ),
        "template": "image_still",
        "required": _VIDEO_CARD_REQUIRED,
        "beat_types": ["image"],
    },
]

#: スキーマ予約のみ(未実装)。指定されたら専用エラーで案内する
RESERVED_KINDS: list[str] = []

OUTPUT_FORMATS: list[str] = ["mp4", "png"]

#: フレームのアスペクト比。通常(16:9)と縦型 Shorts(9:16)だけを扱う。
#: 実際の画素寸法とフレーム座標系は tools/visualize/templates/layout.py が持つ。
ASPECT_RATIOS: list[str] = ["16:9", "9:16"]

MAX_BEATS = 30
#: quote の本文上限。これを超えると 1 画面で読めない(引用は抜粋であるべき)。
MAX_QUOTE_CHARS = 240
#: code の本文上限。行数・桁数はテンプレート側でさらに切り詰める。
MAX_CODE_CHARS = 1200
#: timeline の点数上限。11件目以降は fit_to_frame の等比縮小で判読不能になるため、
#: 描いてから潰れるのではなく検証で弾いてエージェントに即フィードバックする。
MAX_TIMELINE_POINTS = 10
#: comparison の列数(distinct side)上限。16:9 に日本語で並べられる実用上の限界。
MAX_COMPARISON_SIDES = 3
#: comparison の行数(distinct aspect)上限。
MAX_COMPARISON_ASPECTS = 6
#: domain の要素数上限。これを超えると fit_to_frame の等比縮小で判読不能になる。
MAX_DOMAIN_ENTITIES = 10
#: domain の関係数上限。矢印が増えるほど交差して読めなくなる。
MAX_DOMAIN_RELATIONS = 12
#: domain のグループ数上限。
MAX_DOMAIN_GROUPS = 4
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


#: label でノードとして参照される beat type。transition の from/to はこの名前空間を
#: 引くため、種別が違っても同名は許されない。
_NODE_BEAT_TYPES = ("flow_step", "decision")


#: beat の強調表現。自由な色は受け付けず閉じた列挙にする(読めない配色を防ぎ、
#: base.py の統一配色と「固定テンプレートのみ」というセキュリティ姿勢を保つ)。
EMPHASIS_VALUES: tuple[str, ...] = ("normal", "key", "warn")

#: 出力品質。png/mp4 とも同じ 16:9 の寸法を使うので、レイアウトは変わらない。
QUALITY_VALUES: tuple[str, ...] = ("draft", "standard", "high")


def _validate_emphasis(beat: dict[str, Any], at: str, errors: list[SceneSpecError]) -> None:
    """emphasis は表現であって事実ではないので、出典要件には影響しない。"""
    emphasis = beat.get("emphasis")
    if emphasis is None:
        return
    if emphasis not in EMPHASIS_VALUES:
        errors.append(
            SceneSpecError(
                f"{at}.emphasis",
                "invalid",
                f"emphasis は {' / '.join(EMPHASIS_VALUES)} のいずれかを指定してください"
                f"(指定: {emphasis})",
            )
        )


def _validate_unique_node_label(
    label: str,
    index: int,
    first_index: dict[str, int],
    at: str,
    errors: list[SceneSpecError],
) -> None:
    """ノードの label が spec 内で一意であることを検証する。

    重複を許すと `data_flow_v1` の `boxes[label]` が後勝ちで上書きされ、前の
    ノードには矢印が繋がらない(描画は成功してしまうので気づけない)。エラー形は
    `sources[].id` の重複検査(`duplicate_id`)と対称にする。
    """
    seen_at = first_index.get(label)
    if seen_at is not None and seen_at != index:
        errors.append(
            SceneSpecError(
                f"{at}.label",
                "duplicate_label",
                f'flow_step の label "{label}" が beats[{seen_at}] と重複しています。'
                "label は transition の接続先を一意に指すため、"
                f'"{label}(1次)" のように区別してください',
            )
        )


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

    # label -> 最初に出現した beats の index。集合ではなく dict で持つのは、
    # transition の接続先解決(unknown_label)に加えて重複検出(duplicate_label)にも
    # 使うため。data_flow_v1 は `boxes[label] = box` でノードを引くので、重複を
    # 通すと後勝ちで上書きされ、前のノードには矢印が繋がらない。
    flow_label_first_index: dict[str, int] = {}
    for index, b in enumerate(beats):
        if not _is_plain_object(b) or b.get("type") not in _NODE_BEAT_TYPES:
            continue
        label = b.get("label")
        if isinstance(label, str) and label not in flow_label_first_index:
            flow_label_first_index[label] = index
    flow_labels = flow_label_first_index.keys()

    # name -> 最初に出現した index。domain_relation の接続先解決と重複検出に使う。
    entity_name_first_index: dict[str, int] = {}
    for index, b in enumerate(beats):
        if not _is_plain_object(b) or b.get("type") != "domain_entity":
            continue
        name = b.get("name")
        if isinstance(name, str) and name not in entity_name_first_index:
            entity_name_first_index[name] = index

    # (aspect, side) -> 最初に出現した index。comparison のセル重複検出に使う。
    comparison_cell_first_index: dict[tuple[str, str], int] = {}
    for index, b in enumerate(beats):
        if not _is_plain_object(b) or b.get("type") != "comparison_item":
            continue
        cell = (b.get("aspect"), b.get("side"))
        if all(isinstance(v, str) for v in cell) and cell not in comparison_cell_first_index:
            comparison_cell_first_index[cell] = index  # type: ignore[index]

    for i, beat in enumerate(beats):
        at = f"beats[{i}]"
        if not _is_plain_object(beat):
            errors.append(SceneSpecError(at, "invalid", "beat はオブジェクトで指定してください"))
            continue
        beat_type = beat.get("type")
        _validate_emphasis(beat, at, errors)
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
            # unit はテンプレート(step_explanation / data_flow_v1)が値の直後へ
            # そのまま連結する。1.0 当初から検証が無く、任意の文字列で数値の意味を
            # 書き換えられる抜け穴になっていた。
            unit = beat.get("unit")
            if unit is not None and (not isinstance(unit, str) or len(unit) > 8):
                errors.append(
                    SceneSpecError(
                        f"{at}.unit", "invalid", "unit は 8 文字以内の文字列で指定してください"
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
            # 矢印ラベル。分岐条件・データ名・トリガを示す。
            # from/to は「宣言済みノード同士の順序」でしかないので出典不要だが、
            # label は独立した事実主張(「スコア80以上」など)なので出典を要求する。
            label = beat.get("label")
            if label is not None:
                if not isinstance(label, str) or not (1 <= len(label) <= 40):
                    errors.append(
                        SceneSpecError(
                            f"{at}.label",
                            "invalid",
                            "transition の label は 1〜40 文字で指定してください",
                        )
                    )
                _validate_source_refs(
                    beat.get("source_refs"),
                    at,
                    source_ids,
                    errors,
                    required=beat.get("decorative") is not True,
                    require_hint=(
                        "label で条件や事実を述べる transition には出典(source_refs)が"
                        "必要です。装飾なら decorative:true を付けてください"
                    ),
                )
        elif beat_type == "flow_step":
            label = beat.get("label")
            if not isinstance(label, str) or len(label) < 1:
                errors.append(SceneSpecError(f"{at}.label", "invalid", "label を指定してください"))
            else:
                _validate_unique_node_label(label, i, flow_label_first_index, at, errors)
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
        elif beat_type == "domain_entity":
            name = beat.get("name")
            if not isinstance(name, str) or not (1 <= len(name) <= 24):
                errors.append(
                    SceneSpecError(f"{at}.name", "invalid", "name は 1〜24 文字で指定してください")
                )
            else:
                seen_at = entity_name_first_index.get(name)
                if seen_at is not None and seen_at != i:
                    errors.append(
                        SceneSpecError(
                            f"{at}.name",
                            "duplicate_name",
                            f'domain_entity の name "{name}" が beats[{seen_at}] と'
                            "重複しています。name は domain_relation の接続先を"
                            "一意に指すため区別してください",
                        )
                    )
            group = beat.get("group")
            if group is not None and (not isinstance(group, str) or not (1 <= len(group) <= 20)):
                errors.append(
                    SceneSpecError(
                        f"{at}.group", "invalid", "group は 1〜20 文字で指定してください"
                    )
                )
            description = beat.get("description")
            if description is not None and (
                not isinstance(description, str) or len(description) > 60
            ):
                errors.append(
                    SceneSpecError(
                        f"{at}.description",
                        "invalid",
                        "description は 60 文字以内で指定してください(箱の中に入るため)",
                    )
                )
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=beat.get("decorative") is not True,
                require_hint=(
                    "domain_entity は構成要素の存在という事実を述べるため"
                    "出典(source_refs)が必要です。装飾なら decorative:true を付けてください"
                ),
            )
        elif beat_type == "domain_relation":
            for key in ("from", "to"):
                value = beat.get(key)
                if not isinstance(value, str) or len(value) < 1:
                    errors.append(
                        SceneSpecError(f"{at}.{key}", "invalid", f"{key} を指定してください")
                    )
                elif value not in entity_name_first_index:
                    errors.append(
                        SceneSpecError(
                            f"{at}.{key}",
                            "unknown_label",
                            f'domain_relation の {key} "{value}" に一致する'
                            "domain_entity の name がありません",
                        )
                    )
            label = beat.get("label")
            if label is not None and (not isinstance(label, str) or not (1 <= len(label) <= 16)):
                errors.append(
                    SceneSpecError(
                        f"{at}.label", "invalid", "label は 1〜16 文字で指定してください"
                    )
                )
            # transition と違い、関係そのものが独立した事実主張(「A は B を呼ぶ」)。
            # ここを免除すると entity だけ出典付きで関係は全部創作、という図が通る。
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=beat.get("decorative") is not True,
                require_hint=(
                    "domain_relation は要素間の関係という事実を述べるため"
                    "出典(source_refs)が必要です。装飾なら decorative:true を付けてください"
                ),
            )
        elif beat_type == "comparison_item":
            for key, limit in (("side", 20), ("aspect", 20), ("text", 60)):
                value = beat.get(key)
                if not isinstance(value, str) or not (1 <= len(value) <= limit):
                    errors.append(
                        SceneSpecError(
                            f"{at}.{key}",
                            "invalid",
                            f"{key} は 1〜{limit} 文字で指定してください",
                        )
                    )
            # 同じ (aspect, side) が2件あるとセルが上書きされ、片方が黙って消える。
            cell = (beat.get("aspect"), beat.get("side"))
            if all(isinstance(v, str) for v in cell):
                seen_at = comparison_cell_first_index.get(cell)
                if seen_at is not None and seen_at != i:
                    errors.append(
                        SceneSpecError(
                            f"{at}.aspect",
                            "duplicate_cell",
                            f'aspect "{cell[0]}" x side "{cell[1]}" のセルが '
                            f"beats[{seen_at}] と重複しています",
                        )
                    )
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=beat.get("decorative") is not True,
                require_hint=(
                    "comparison_item は対比という事実を述べるため出典(source_refs)が"
                    "必要です。装飾なら decorative:true を付けてください"
                ),
            )
        elif beat_type == "timeline_point":
            at_value = beat.get("at")
            if not isinstance(at_value, str) or not (1 <= len(at_value) <= 24):
                errors.append(
                    SceneSpecError(
                        f"{at}.at",
                        "invalid",
                        "at は 1〜24 文字で指定してください"
                        "(日付として解釈しないので「6/11 定例」のような原文表記でよい)",
                    )
                )
            label = beat.get("label")
            if not isinstance(label, str) or not (1 <= len(label) <= 40):
                errors.append(
                    SceneSpecError(
                        f"{at}.label", "invalid", "label は 1〜40 文字で指定してください"
                    )
                )
            description = beat.get("description")
            if description is not None and (
                not isinstance(description, str) or len(description) > 200
            ):
                errors.append(
                    SceneSpecError(
                        f"{at}.description",
                        "invalid",
                        "description は 200 文字以内の文字列で指定してください",
                    )
                )
            # timeline_point は「この時点でこれが起きた/決まった」という時制付きの
            # 事実主張そのもの。flow_step の label(構造上のノード名)より強い主張なので、
            # description の有無に関わらず出典を要求する。
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=beat.get("decorative") is not True,
                require_hint=(
                    "timeline_point は時点付きの事実を述べるため出典(source_refs)が"
                    "必要です。装飾なら decorative:true を付けてください"
                ),
            )
        elif beat_type == "decision":
            # ひし形の条件分岐ノード。分岐は「decision 1個 + ラベル付き transition n本」
            # で表し、合流は複数の transition が同じ to を指すだけで表現できる。
            label = beat.get("label")
            if not isinstance(label, str) or not (1 <= len(label) <= 16):
                errors.append(
                    SceneSpecError(
                        f"{at}.label",
                        "invalid",
                        "decision の label は 1〜16 文字で指定してください"
                        "(ひし形は同じ文字数でも矩形の約1.8倍の面積を要するため)",
                    )
                )
            else:
                _validate_unique_node_label(label, i, flow_label_first_index, at, errors)
            if beat.get("description") is not None:
                errors.append(
                    SceneSpecError(
                        f"{at}.description",
                        "invalid",
                        "decision に description は指定できません"
                        "(条件はラベルに、根拠は transition.label に書いてください)",
                    )
                )
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=beat.get("decorative") is not True,
                require_hint=(
                    "条件を述べる decision には出典(source_refs)が必要です。"
                    "装飾なら decorative:true を付けてください"
                ),
            )
        elif beat_type == "quote":
            # 原文の引用。**decorative を認めない** —— 引用は「KB にこう書いてある」
            # という主張そのものなので、出典なしの引用は成立しない。
            text = beat.get("text")
            if not isinstance(text, str) or not (1 <= len(text) <= MAX_QUOTE_CHARS):
                errors.append(
                    SceneSpecError(
                        f"{at}.text",
                        "invalid",
                        f"quote の text は 1〜{MAX_QUOTE_CHARS} 文字で指定してください",
                    )
                )
            attribution = beat.get("attribution")
            if attribution is not None and not (
                isinstance(attribution, str) and 1 <= len(attribution) <= 60
            ):
                errors.append(
                    SceneSpecError(
                        f"{at}.attribution",
                        "invalid",
                        "attribution は 1〜60 文字で指定してください",
                    )
                )
            if beat.get("decorative") is True:
                errors.append(
                    SceneSpecError(
                        f"{at}.decorative",
                        "invalid",
                        "quote に decorative は指定できません"
                        "(引用は原文の主張そのものなので出典が必ず要ります)",
                    )
                )
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=True,
                require_hint="quote には出典(source_refs)が必要です",
            )
        elif beat_type == "code":
            text = beat.get("text")
            if not isinstance(text, str) or not (1 <= len(text) <= MAX_CODE_CHARS):
                errors.append(
                    SceneSpecError(
                        f"{at}.text",
                        "invalid",
                        f"code の text は 1〜{MAX_CODE_CHARS} 文字で指定してください",
                    )
                )
            language = beat.get("language")
            if language is not None and not (
                isinstance(language, str) and 1 <= len(language) <= 24
            ):
                errors.append(
                    SceneSpecError(f"{at}.language", "invalid", "language は 1〜24 文字です")
                )
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=beat.get("decorative") is not True,
                require_hint=(
                    "KB のコードを写す code には出典(source_refs)が必要です。"
                    "説明用に書き下ろしたコードなら decorative:true を付けてください"
                ),
            )
        elif beat_type == "formula":
            text = beat.get("text")
            if not isinstance(text, str) or not (1 <= len(text) <= 120):
                errors.append(
                    SceneSpecError(f"{at}.text", "invalid", "formula の text は 1〜120 文字です")
                )
            caption = beat.get("caption")
            if caption is not None and not (isinstance(caption, str) and 1 <= len(caption) <= 200):
                errors.append(
                    SceneSpecError(f"{at}.caption", "invalid", "caption は 1〜200 文字です")
                )
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=beat.get("decorative") is not True,
                require_hint=(
                    "KB に根拠のある式には出典(source_refs)が必要です。"
                    "説明用の一般式なら decorative:true を付けてください"
                ),
            )
        elif beat_type == "image":
            # path は scene-spec.json と同じディレクトリからの相対パス。
            # `..`・絶対パス・バックスラッシュを弾くことで、成果物ツリーの外を
            # 参照できないことを**検証段階で**保証する(テンプレート任せにしない)。
            path = beat.get("path")
            if not _is_safe_relative_path(path):
                errors.append(
                    SceneSpecError(
                        f"{at}.path",
                        "invalid",
                        "path は scene-spec.json からの相対 POSIX パスのみ指定できます"
                        '(".."・絶対パス・バックスラッシュ不可)',
                    )
                )
            caption = beat.get("caption")
            if caption is not None and not (isinstance(caption, str) and 1 <= len(caption) <= 200):
                errors.append(
                    SceneSpecError(f"{at}.caption", "invalid", "caption は 1〜200 文字です")
                )
            license_text = beat.get("license")
            if license_text is not None and not (
                isinstance(license_text, str) and 1 <= len(license_text) <= 120
            ):
                errors.append(
                    SceneSpecError(f"{at}.license", "invalid", "license は 1〜120 文字です")
                )
            _validate_source_refs(
                beat.get("source_refs"),
                at,
                source_ids,
                errors,
                required=beat.get("decorative") is not True,
                require_hint=(
                    "画面キャプチャが何を示すかは事実の主張なので出典(source_refs)が"
                    "必要です。装飾画像なら decorative:true を付けてください"
                ),
            )

    # kind ごとの構成制約
    reason = composition_reason(kind_info["kind"], [b for b in beats if _is_plain_object(b)])
    if reason is not None:
        errors.append(SceneSpecError("beats", "invalid", f"{kind_info['kind']} には{reason}"))


def _distinct_in_order(beats: list[dict[str, Any]], beat_type: str, key: str) -> list[str]:
    """指定 beat type の `key` の値を初出順で重複なく返す(列・行の並び順の正本)。"""
    seen: list[str] = []
    for beat in beats:
        if beat.get("type") != beat_type:
            continue
        value = beat.get(key)
        if isinstance(value, str) and value not in seen:
            seen.append(value)
    return seen


#: 動画カードの構成制約。`(必須 beat type, 最小件数)`。
#: `None` は「type を問わず合計 N 件」の意味（statement と metric の両方を許す面）。
_VIDEO_CARD_MIN_BEATS: dict[str, tuple[str | None, int]] = {
    "title": ("statement", 0),
    "chapter": ("statement", 0),
    "key_points": (None, 1),
    "quote": ("quote", 1),
    "summary": (None, 1),
    "cta": ("statement", 1),
    "ending": ("statement", 0),
    "code": ("code", 1),
    "formula": ("formula", 1),
    "image": ("image", 1),
}


def composition_reason(scene_kind: str, beats: list[dict[str, Any]]) -> str | None:
    """kind ごとの構成制約を満たさない理由を短文で返す(満たすなら None)。

    剪定前(`scene_spec._validate_beats`)と剪定後(`source_verifier.verify_sources`)の
    双方が同じ規則を検査する必要があり、以前は同じルールが別文言で二重に書かれていた。
    kind を増やすたびに二重化が広がるので、規則の定義はここ1箇所に集約する。

    呼び出し側がそれぞれの文脈を付ける:
      - scene_spec:      f"{kind} には{reason}"
      - source_verifier: f"出典検証の結果、描画可能な beat が残りません（{reason}）"
    """
    counts: dict[str, int] = {}
    for beat in beats:
        beat_type = beat.get("type")
        if isinstance(beat_type, str):
            counts[beat_type] = counts.get(beat_type, 0) + 1

    if scene_kind == "explain":
        if counts.get("statement", 0) + counts.get("metric", 0) < 1:
            return "statement または metric の beat が 1 件以上必要です"
        return None
    if scene_kind == "flow":
        if counts.get("flow_step", 0) < 2:
            return "flow_step の beat が 2 件以上必要です"
        return None
    if scene_kind == "comparison":
        if counts.get("comparison_item", 0) < 2:
            return "comparison_item の beat が 2 件以上必要です"
        sides = _distinct_in_order(beats, "comparison_item", "side")
        aspects = _distinct_in_order(beats, "comparison_item", "aspect")
        if len(sides) < 2:
            return "比較には 2 つ以上の side（列見出し）が必要です"
        if len(sides) > MAX_COMPARISON_SIDES:
            return f"side（列見出し）は {MAX_COMPARISON_SIDES} 種類以内にしてください"
        if len(aspects) > MAX_COMPARISON_ASPECTS:
            return f"aspect（行見出し）は {MAX_COMPARISON_ASPECTS} 種類以内にしてください"
        return None
    if scene_kind == "domain":
        entities = [b for b in beats if b.get("type") == "domain_entity"]
        relations = [b for b in beats if b.get("type") == "domain_relation"]
        if len(entities) < 2:
            return "domain_entity の beat が 2 件以上必要です"
        if len(entities) > MAX_DOMAIN_ENTITIES:
            return f"domain_entity は {MAX_DOMAIN_ENTITIES} 件以内にしてください"
        if len(relations) > MAX_DOMAIN_RELATIONS:
            return f"domain_relation は {MAX_DOMAIN_RELATIONS} 件以内にしてください"
        groups = {b.get("group") for b in entities if isinstance(b.get("group"), str)}
        if len(groups) > MAX_DOMAIN_GROUPS:
            return f"group は {MAX_DOMAIN_GROUPS} 種類以内にしてください"
        return None
    if scene_kind in _VIDEO_CARD_MIN_BEATS:
        beat_type, minimum = _VIDEO_CARD_MIN_BEATS[scene_kind]
        if beat_type is None:
            if sum(counts.values()) < minimum:
                return f"beat が {minimum} 件以上必要です"
        elif counts.get(beat_type, 0) < minimum:
            return f"{beat_type} の beat が {minimum} 件以上必要です"
        return None
    if scene_kind == "timeline":
        points = counts.get("timeline_point", 0)
        if points < 2:
            return "timeline_point の beat が 2 件以上必要です"
        if points > MAX_TIMELINE_POINTS:
            return f"timeline_point の beat は {MAX_TIMELINE_POINTS} 件以内にしてください"
        return None
    return None


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
    quality = spec.get("quality")
    if quality is not None and quality not in QUALITY_VALUES:
        errors.append(
            SceneSpecError(
                "quality",
                "invalid",
                f"quality は {' / '.join(QUALITY_VALUES)} のいずれかを指定してください"
                f"(指定: {quality})",
            )
        )

    frame = spec.get("frame")
    if frame is not None:
        if not _is_plain_object(frame):
            errors.append(SceneSpecError("frame", "invalid", "frame はオブジェクトで指定します"))
        elif frame.get("aspect_ratio") not in ASPECT_RATIOS:
            errors.append(
                SceneSpecError(
                    "frame.aspect_ratio",
                    "invalid",
                    f"aspect_ratio は {' / '.join(ASPECT_RATIOS)} のいずれかを指定してください",
                )
            )

    for key in ("chapter_index", "chapter_total"):
        value = spec.get(key)
        if value is not None and (not _is_integer(value) or value < 1):
            errors.append(SceneSpecError(key, "invalid", f"{key} は 1 以上の整数です"))

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
    "ASPECT_RATIOS",
    "MAX_BEATS",
    "MAX_CODE_CHARS",
    "MAX_QUOTE_CHARS",
    "OUTPUT_FORMATS",
    "RESERVED_KINDS",
    "SCENE_KINDS",
    "SCHEMA_VERSION",
    "SceneSpecError",
    "SceneSpecResult",
    "validate_scene_spec",
]
