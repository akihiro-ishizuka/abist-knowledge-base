"""VideoProjectSpec 1.0 の定義・バリデータ（社内動画生成の入口）。

契約は `design/video-pipeline.md`。SceneSpec 1.0（`domain/scene_spec.py`）と同じ流儀:

- 検証は純関数。ファイルシステムにも DB にも触れない
- エラーは `{path, code, message}` の配列で返す
- 未知フィールドは拒否しない（前方互換のため。SceneSpec 1.0 と同じ判断）

**入力の中心は `docs/` 配下の Markdown。** esa 記事はその由来の一つにすぎない。
ディレクトリ指定は「そこから選抜する候補集合」であり、配下の全件を使うことは
要求しない（`docs/` には約 85,000 件の Markdown があり原理的に不可能）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

#: 入力の役割。`explicit_primary` だけが「その1件の使用」を要求される。
SELECTION_EXPLICIT_PRIMARY = "explicit_primary"
SELECTION_COLLECTION_CANDIDATE = "collection_candidate"
SELECTION_SUPPLEMENTAL = "supplemental"
SELECTIONS: tuple[str, ...] = (
    SELECTION_EXPLICIT_PRIMARY,
    SELECTION_COLLECTION_CANDIDATE,
    SELECTION_SUPPLEMENTAL,
)

#: `ResolvedInput.origin.type`。
ORIGIN_TYPES: tuple[str, ...] = ("kb_path", "kb_directory", "kb_query", "esa_post")

#: 題材として受け付ける拡張子。ディレクトリ走査でもこれ以外は収集しない。
MARKDOWN_SUFFIXES: tuple[str, ...] = (".md", ".markdown")

#: 1ディレクトリからの選抜上限。
DEFAULT_MAX_DOCS_PER_DIRECTORY = 20
#: 全ディレクトリ合計の選抜上限。
DEFAULT_MAX_TOTAL_CANDIDATES = 60

ASPECT_RATIOS: tuple[str, ...] = ("16:9", "9:16")
#: アスペクト比ごとの比率（width x height の整合検査に使う）。
ASPECT_DIMENSIONS: dict[str, tuple[int, int]] = {"16:9": (16, 9), "9:16": (9, 16)}
#: 縦型（Shorts）の尺上限。これを超える縦型動画は視聴面の前提が崩れる。
SHORTS_MAX_DURATION_SEC = 180.0

#: 品質段。**画素寸法の段だけ**を決める（アスペクト比は `format.aspect_ratio` の担当）。
VIDEO_QUALITIES: tuple[str, ...] = ("draft", "standard", "high")
DEFAULT_VIDEO_QUALITY = "standard"
#: アスペクト比 x 品質 の画素寸法。
#: `tools/visualize/templates/layout.FRAME_PIXELS` の写し（別 venv のため import
#: できない）。ずれるとレンダリング結果と契約が食い違うので整合テストで縛る。
FRAME_PIXELS: dict[str, dict[str, tuple[int, int]]] = {
    "16:9": {"draft": (1280, 720), "standard": (1920, 1080), "high": (2560, 1440)},
    "9:16": {"draft": (720, 1280), "standard": (1080, 1920), "high": (1440, 2560)},
}
#: 品質段ごとのフレームレート（`tools/visualize/render_scene.py` の写し）。
QUALITY_FRAME_RATES: dict[str, int] = {"draft": 30, "standard": 30, "high": 60}


def frame_pixels(aspect_ratio: str, quality: str) -> tuple[int, int]:
    """アスペクト比と品質から画素寸法を返す（未知の値は既定へ落とす）。"""
    table = FRAME_PIXELS.get(aspect_ratio, FRAME_PIXELS["16:9"])
    return table.get(quality, table[DEFAULT_VIDEO_QUALITY])


CLASSIFICATIONS: tuple[str, ...] = ("internal", "confidential", "public_candidate_pending")
REVIEW_STATUSES: tuple[str, ...] = (
    "not_requested",
    "submitted",
    "approved_for_manual_publish",
    "rejected",
)

_TITLE_MAX = 120
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class VideoSpecError:
    """検証エラー1件（SceneSpecError と同形）。"""

    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class VideoSpecResult:
    ok: bool
    spec: dict[str, Any] | None = None
    errors: list[VideoSpecError] = field(default_factory=list)


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def is_markdown_path(path: str) -> bool:
    return path.lower().endswith(MARKDOWN_SUFFIXES)


def normalize_docs_path(raw: Any) -> str | None:
    """入力パスを `docs/` 相対の POSIX パスへ正規化する（不正なら None）。

    利便性のため先頭の `docs/` は剥がす。`..`・絶対パス・ドライブ指定は拒否する
    （`domain/scene_spec._is_safe_relative_path` と同じ制約）。
    実ファイル・シンボリックリンクの確認は解決側（`input_resolver`）の責務。
    """
    if not isinstance(raw, str) or not raw:
        return None
    candidate = raw.replace("\\", "/").strip()
    if not candidate:
        return None
    if candidate.startswith("/"):
        return None
    if re.match(r"^[a-zA-Z]:", candidate):
        return None
    parts = [p for p in candidate.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    if parts and parts[0] == "docs":
        parts = parts[1:]
    if not parts:
        return None
    return "/".join(parts)


def default_distribution() -> dict[str, Any]:
    return {
        "classification": "internal",
        "audience": [],
        "allowed_groups": [],
        "public_candidate": False,
        "public_review_status": "not_requested",
    }


def default_format() -> dict[str, Any]:
    return {
        "aspect_ratio": "16:9",
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "target_duration_sec": {"min": 120, "max": 600},
    }


def default_inputs() -> dict[str, Any]:
    return {"kb_paths": [], "kb_directories": [], "kb_queries": [], "esa_posts": []}


def _validate_inputs(spec: dict[str, Any], errors: list[VideoSpecError]) -> None:
    inputs = spec.get("inputs")
    if not _is_obj(inputs):
        errors.append(
            VideoSpecError("inputs", "invalid", "inputs はオブジェクトで指定してください")
        )
        return

    for key in ("kb_paths", "kb_directories"):
        values = inputs.get(key) or []
        if not _is_str_list(values):
            errors.append(VideoSpecError(f"inputs.{key}", "invalid", f"{key} は文字列配列です"))
            continue
        for i, raw in enumerate(values):
            normalized = normalize_docs_path(raw)
            if normalized is None:
                errors.append(
                    VideoSpecError(
                        f"inputs.{key}[{i}]",
                        "INVALID_INPUT_PATH",
                        f'"{raw}" は docs/ 配下の相対パスではありません'
                        "(..・絶対パス・ドライブ指定は使えません)",
                    )
                )
            elif key == "kb_paths" and not is_markdown_path(normalized):
                errors.append(
                    VideoSpecError(
                        f"inputs.{key}[{i}]",
                        "INVALID_INPUT_PATH",
                        f'"{raw}" は Markdown ではありません'
                        f"(対象は {' / '.join(MARKDOWN_SUFFIXES)})",
                    )
                )

    queries = inputs.get("kb_queries") or []
    if not isinstance(queries, list):
        errors.append(VideoSpecError("inputs.kb_queries", "invalid", "kb_queries は配列です"))
    else:
        for i, q in enumerate(queries):
            if isinstance(q, str):
                if not q.strip():
                    errors.append(
                        VideoSpecError(f"inputs.kb_queries[{i}]", "invalid", "空の検索条件です")
                    )
            elif _is_obj(q):
                if not isinstance(q.get("query"), str) or not q["query"].strip():
                    errors.append(
                        VideoSpecError(
                            f"inputs.kb_queries[{i}].query", "invalid", "query を指定してください"
                        )
                    )
            else:
                errors.append(
                    VideoSpecError(
                        f"inputs.kb_queries[{i}]",
                        "invalid",
                        "kb_queries の要素は文字列またはオブジェクトです",
                    )
                )

    posts = inputs.get("esa_posts") or []
    if not isinstance(posts, list):
        errors.append(VideoSpecError("inputs.esa_posts", "invalid", "esa_posts は配列です"))
    else:
        for i, post in enumerate(posts):
            if not _is_obj(post):
                errors.append(
                    VideoSpecError(f"inputs.esa_posts[{i}]", "invalid", "オブジェクトで指定します")
                )
                continue
            has_url = isinstance(post.get("url"), str) and post["url"].strip()
            post_id = post.get("post_id")
            has_id = isinstance(post_id, int) and not isinstance(post_id, bool)
            if not has_url and not has_id:
                errors.append(
                    VideoSpecError(
                        f"inputs.esa_posts[{i}]",
                        "invalid",
                        "url または post_id のどちらかを指定してください",
                    )
                )

    # 空の inputs は「題材が無い」ので解決前に弾く。
    if not any(inputs.get(k) for k in ("kb_paths", "kb_directories", "kb_queries", "esa_posts")):
        errors.append(
            VideoSpecError(
                "inputs",
                "NO_RESOLVABLE_INPUT",
                "題材が指定されていません。kb_paths / kb_directories / kb_queries / "
                "esa_posts のいずれかを指定してください",
            )
        )


def _validate_format(spec: dict[str, Any], errors: list[VideoSpecError]) -> None:
    fmt = spec.get("format")
    if not _is_obj(fmt):
        errors.append(VideoSpecError("format", "invalid", "format はオブジェクトです"))
        return
    if fmt.get("aspect_ratio") not in ASPECT_RATIOS:
        errors.append(
            VideoSpecError(
                "format.aspect_ratio",
                "invalid",
                f"aspect_ratio は {' / '.join(ASPECT_RATIOS)} のいずれかです",
            )
        )
    for key in ("width", "height", "fps"):
        value = fmt.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(VideoSpecError(f"format.{key}", "invalid", f"{key} は正の整数です"))
    aspect = fmt.get("aspect_ratio")
    width, height = fmt.get("width"), fmt.get("height")
    expected = ASPECT_DIMENSIONS.get(aspect)
    if expected is not None and isinstance(width, int) and isinstance(height, int):
        want_w, want_h = expected
        if abs(width / max(1, height) - want_w / want_h) > 0.01:
            errors.append(
                VideoSpecError(
                    "format",
                    "invalid",
                    f"width x height が aspect_ratio {aspect} と一致しません"
                    f"（{aspect} は {want_w}:{want_h} の比率です）",
                )
            )

    target = fmt.get("target_duration_sec")
    if _is_obj(target):
        lo, hi = target.get("min"), target.get("max")
        numeric = (int, float)
        if (
            not isinstance(lo, numeric)
            or not isinstance(hi, numeric)
            or bool in (type(lo), type(hi))
        ):
            errors.append(
                VideoSpecError("format.target_duration_sec", "invalid", "min / max は数値です")
            )
        elif lo > hi:
            errors.append(
                VideoSpecError("format.target_duration_sec", "invalid", "min は max 以下です")
            )
        elif lo <= 0:
            errors.append(
                VideoSpecError("format.target_duration_sec", "invalid", "min は 0 より大きい値です")
            )
        elif aspect == "9:16" and hi > SHORTS_MAX_DURATION_SEC:
            # 縦型は Shorts 想定。長尺を縦で作ると視聴面の前提が崩れるので、
            # 描いてから気付くのではなく検証で弾く。
            errors.append(
                VideoSpecError(
                    "format.target_duration_sec",
                    "INVALID_VIDEO_SPEC",
                    f"9:16（縦型）の尺は {SHORTS_MAX_DURATION_SEC} 秒以内にしてください"
                    f"（指定: {hi} 秒）",
                )
            )


def _validate_distribution(spec: dict[str, Any], errors: list[VideoSpecError]) -> None:
    dist = spec.get("distribution")
    if not _is_obj(dist):
        errors.append(VideoSpecError("distribution", "invalid", "distribution はオブジェクトです"))
        return
    if dist.get("classification") not in CLASSIFICATIONS:
        errors.append(
            VideoSpecError(
                "distribution.classification",
                "invalid",
                f"classification は {' / '.join(CLASSIFICATIONS)} のいずれかです",
            )
        )
    if dist.get("public_review_status") not in REVIEW_STATUSES:
        errors.append(
            VideoSpecError(
                "distribution.public_review_status",
                "invalid",
                f"public_review_status は {' / '.join(REVIEW_STATUSES)} のいずれかです",
            )
        )
    for key in ("audience", "allowed_groups"):
        if not _is_str_list(dist.get(key) or []):
            errors.append(VideoSpecError(f"distribution.{key}", "invalid", "文字列配列です"))
    if not isinstance(dist.get("public_candidate"), bool):
        errors.append(
            VideoSpecError("distribution.public_candidate", "invalid", "真偽値で指定してください")
        )


def _validate_story_requirements(spec: dict[str, Any], errors: list[VideoSpecError]) -> None:
    """作者が自己申告する構成要件を検証する。

    かつては動画タイトルの部分一致で必須テーマ・シーン数・出典期間を暗黙に強制して
    いた。同種のタイトルを持つ別動画が誤った要件を継承してしまうため、要件は spec 側の
    宣言に移した（`content_quality.validate_story_content` が執行する）。
    全フィールド任意。宣言しなければ汎用チェックだけが走る。
    """
    requirements = spec.get("story_requirements")
    if requirements is None:
        return
    if not _is_obj(requirements):
        errors.append(
            VideoSpecError("story_requirements", "invalid", "story_requirements はオブジェクトです")
        )
        return

    for key in ("required_topics", "required_scene_kinds"):
        values = requirements.get(key)
        if values is not None and not _is_str_list(values):
            errors.append(
                VideoSpecError(f"story_requirements.{key}", "invalid", f"{key} は文字列配列です")
            )

    for key in ("scene_count", "sound_events"):
        span = requirements.get(key)
        if span is None:
            continue
        at = f"story_requirements.{key}"
        if not _is_obj(span):
            errors.append(VideoSpecError(at, "invalid", f"{key} は min / max を持つ範囲です"))
            continue
        low, high = span.get("min"), span.get("max")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (low, high)):
            errors.append(VideoSpecError(at, "invalid", "min / max は 0 以上の整数です"))
        elif low > high:
            errors.append(VideoSpecError(at, "invalid", "min は max 以下です"))

    pattern = requirements.get("source_path_pattern")
    if pattern is not None:
        at = "story_requirements.source_path_pattern"
        if not isinstance(pattern, str) or not pattern:
            errors.append(VideoSpecError(at, "invalid", "source_path_pattern は文字列です"))
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(VideoSpecError(at, "invalid", f"正規表現として解釈できません: {exc}"))


def _validate_sources(spec: dict[str, Any], errors: list[VideoSpecError]) -> None:
    """`sources` は解決後にのみ入る。空でも valid（Phase 1 では未解決の spec を許す）。"""
    sources = spec.get("sources")
    if sources is None:
        return
    if not isinstance(sources, list):
        errors.append(VideoSpecError("sources", "invalid", "sources は配列です"))
        return
    seen: set[str] = set()
    for i, src in enumerate(sources):
        at = f"sources[{i}]"
        if not _is_obj(src):
            errors.append(VideoSpecError(at, "invalid", "オブジェクトで指定します"))
            continue
        path = src.get("path")
        if normalize_docs_path(path) is None:
            errors.append(
                VideoSpecError(f"{at}.path", "INVALID_INPUT_PATH", "docs/ 配下の相対パスです")
            )
        elif path in seen:
            errors.append(
                VideoSpecError(f"{at}.path", "duplicate_path", f'"{path}" が重複しています')
            )
        else:
            seen.add(str(path))
        if src.get("selection") not in SELECTIONS:
            errors.append(
                VideoSpecError(
                    f"{at}.selection",
                    "invalid",
                    f"selection は {' / '.join(SELECTIONS)} のいずれかです",
                )
            )
        if not isinstance(src.get("require_usage"), bool):
            errors.append(VideoSpecError(f"{at}.require_usage", "invalid", "真偽値です"))
        content_hash = src.get("content_hash")
        if content_hash is not None and not (
            isinstance(content_hash, str) and _HASH_RE.match(content_hash)
        ):
            errors.append(
                VideoSpecError(f"{at}.content_hash", "invalid", "sha256 の 64 桁 hex です")
            )
        origin = src.get("origin")
        if not _is_obj(origin) or origin.get("type") not in ORIGIN_TYPES:
            errors.append(
                VideoSpecError(
                    f"{at}.origin.type",
                    "invalid",
                    f"origin.type は {' / '.join(ORIGIN_TYPES)} のいずれかです",
                )
            )
        elif origin["type"] == "kb_path" and src.get("selection") != SELECTION_EXPLICIT_PRIMARY:
            errors.append(
                VideoSpecError(
                    f"{at}.selection",
                    "invalid",
                    "kb_path 由来は explicit_primary でなければなりません",
                )
            )
        elif (
            origin["type"] == "kb_directory"
            and src.get("selection") != SELECTION_COLLECTION_CANDIDATE
        ):
            errors.append(
                VideoSpecError(
                    f"{at}.selection",
                    "invalid",
                    "kb_directory 由来は collection_candidate でなければなりません",
                )
            )
        # require_usage は selection から一意に決まる（食い違いを弾く）
        expected_require = src.get("selection") == SELECTION_EXPLICIT_PRIMARY
        if isinstance(src.get("require_usage"), bool) and src["require_usage"] != expected_require:
            errors.append(
                VideoSpecError(
                    f"{at}.require_usage",
                    "invalid",
                    "require_usage は explicit_primary のときだけ true です",
                )
            )


def fill_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """省略可能なフィールドへ既定値を埋めた**コピー**を返す。"""
    filled = dict(spec)
    filled.setdefault("schema_version", SCHEMA_VERSION)
    filled.setdefault("language", "ja")
    filled.setdefault("video_id", None)
    filled.setdefault("chapters", [])
    filled.setdefault("scenes", [])
    filled.setdefault("sound_events", [])
    filled.setdefault("sources", [])
    filled.setdefault("story_requirements", None)

    inputs = {**default_inputs(), **(filled.get("inputs") or {})}
    filled["inputs"] = inputs

    fmt = {**default_format(), **(filled.get("format") or {})}
    filled["format"] = fmt

    dist = {**default_distribution(), **(filled.get("distribution") or {})}
    filled["distribution"] = dist
    return filled


def validate_video_project_spec(spec: Any) -> VideoSpecResult:
    """VideoProjectSpec を検証する（既定値を埋めた spec を返す）。"""
    if not _is_obj(spec):
        return VideoSpecResult(
            ok=False,
            errors=[VideoSpecError("", "invalid", "VideoProjectSpec はオブジェクトです")],
        )

    errors: list[VideoSpecError] = []
    if spec.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        errors.append(
            VideoSpecError(
                "schema_version",
                "invalid",
                f'schema_version は "{SCHEMA_VERSION}" を指定してください',
            )
        )

    title = spec.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= _TITLE_MAX):
        errors.append(VideoSpecError("title", "invalid", f"title は 1〜{_TITLE_MAX} 文字です"))

    filled = fill_defaults(spec)
    _validate_inputs(filled, errors)
    _validate_format(filled, errors)
    _validate_distribution(filled, errors)
    _validate_sources(filled, errors)
    _validate_story_requirements(filled, errors)

    # 機密ソースがあれば public_candidate を強制的に下げる（審査提出の誤爆防止）
    dist = filled.get("distribution") or {}
    if dist.get("public_candidate") is True:
        sensitive = [
            s
            for s in (filled.get("sources") or [])
            if _is_obj(s) and s.get("sensitivity") in ("confidential", "pii", "internal_restricted")
        ]
        if sensitive or dist.get("classification") == "confidential":
            dist["public_candidate"] = False

    if errors:
        return VideoSpecResult(ok=False, errors=errors)
    return VideoSpecResult(ok=True, spec=filled)


__all__ = [
    "ASPECT_DIMENSIONS",
    "ASPECT_RATIOS",
    "DEFAULT_VIDEO_QUALITY",
    "FRAME_PIXELS",
    "QUALITY_FRAME_RATES",
    "VIDEO_QUALITIES",
    "frame_pixels",
    "DEFAULT_MAX_DOCS_PER_DIRECTORY",
    "DEFAULT_MAX_TOTAL_CANDIDATES",
    "MARKDOWN_SUFFIXES",
    "ORIGIN_TYPES",
    "SCHEMA_VERSION",
    "SELECTIONS",
    "SELECTION_COLLECTION_CANDIDATE",
    "SELECTION_EXPLICIT_PRIMARY",
    "SELECTION_SUPPLEMENTAL",
    "SHORTS_MAX_DURATION_SEC",
    "VideoSpecError",
    "VideoSpecResult",
    "default_distribution",
    "default_format",
    "default_inputs",
    "fill_defaults",
    "is_markdown_path",
    "normalize_docs_path",
    "validate_video_project_spec",
]
