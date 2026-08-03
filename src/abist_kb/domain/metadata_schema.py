"""文書メタデータのスキーマ定義と由来推定、および各所に重複していたサニタイズ処理の集約。

旧実装 `tools/lib/metadata-schema.js` の移植。設計原則(`design/system-design.md`)の
「frontmatter には人間が読む意味のある属性のみ」「業務状態と同期状態を分離する」に従う。

**`safe_batch_name` と `sanitize_file_name` は別物である。** 旧実装では同種のロジックが
`download-batch.js`(`generateFolderName` 内の `safeBatchName`)と `download-article.js`
(`sanitizeFileName`/`sanitizeCategoryPath`)の3箇所に重複していた。ここへ集約するが、
**振る舞いの違いは維持する**:

  - `safe_batch_name` は4つの文字置換のみ(禁止文字→`-`、空白→`-`、連続`-`圧縮、前後`-`除去)。
    batch-config.js のキー名から出力先ディレクトリ名を作るためだけに使う。
  - `sanitize_file_name` は上記4置換に加えて、Windows 予約名(CON/PRN/AUX/NUL/COM1-9/LPT1-9)
    への `file-` 接頭辞付与と、255 **UTF-8バイト**での切詰(マルチバイト文字の途中では切らない、
    切り詰めたら `...` を付与)を行う。ダウンロードしたファイル・カテゴリパスの実ファイル名に使う。

  この2つを1関数へ統合すると、統合先に寄せた方の関数を呼んでいた全既存バッチ名(または
  全ファイル名)の挙動が変わってしまう。`tests/fixtures/kernel/metadata-schema.json` の
  `sanitize_filename_*` 系と `resolve_batch_output_dirs`(内部で `safe_batch_name` を
  間接的に検証する)が両方の入出力をゴールデンとして固定しているので、実装を変える前に
  必ずその両方が緑のままであることを確認すること。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from abist_kb.domain.frontmatter import JS_WHITESPACE_CLASS

# --- 列挙値 ------------------------------------------------------------------


class Source(StrEnum):
    """文書の由来。"""

    ESA = "esa"
    WEB = "web"
    GIT = "git"
    MANUAL = "manual"


class ManagedBy(StrEnum):
    """誰がこのファイルを管理するか(human 以外は自動同期に上書きされうる)。"""

    ESA_SYNC = "esa-sync"
    WEB_SYNC = "web-sync"
    GIT_SYNC = "git-sync"
    HUMAN = "human"


class DocumentType(StrEnum):
    """文書の種類。"""

    MEETING = "meeting"
    SPECIFICATION = "specification"
    KNOWLEDGE = "knowledge"
    MEMO = "memo"
    REFERENCE = "reference"


class Status(StrEnum):
    """業務状態(人間が決める。同期処理は変更しない)。"""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


#: source -> managed_by の正しい対応
MANAGED_BY_FOR_SOURCE: dict[Source, ManagedBy] = {
    Source.ESA: ManagedBy.ESA_SYNC,
    Source.WEB: ManagedBy.WEB_SYNC,
    Source.GIT: ManagedBy.GIT_SYNC,
    Source.MANUAL: ManagedBy.HUMAN,
}

#: frontmatter のキー順。既存の download-article.js / download-web.js が出す順を前段に置き、
#: Stage 1 で追加する4キーを末尾に固定する(既存ファイルの差分を末尾追記だけに留めるため)。
FRONTMATTER_KEY_ORDER: tuple[str, ...] = (
    "title",
    "date",
    "updated_at",
    "author",
    "updated_by",
    "category",
    "tags",
    "post_number",
    "url",
    "source",
    "managed_by",
    "document_type",
    "status",
)

#: backfill がファイルへ書き込むキー(これ以外は触らない)。
BACKFILL_KEYS: tuple[str, ...] = ("source", "managed_by", "document_type", "status")

#: 参照コーパス(実務資料と分離する大規模コーパス)のパス接頭辞。
REFERENCE_CORPUS_PREFIXES: tuple[str, ...] = (
    "knowledge/B32doc",
    "knowledge/catiadoc",
    "knowledge/generated",
)


def to_posix_path(path: Any) -> str:
    """バックスラッシュをスラッシュへ変換する(Windows パス対策)。"""
    return str(path if path is not None else "").replace("\\", "/")


def is_reference_corpus(relative_path: Any) -> bool:
    """docs/ からの相対パスが参照コーパスかどうか。"""
    p = to_posix_path(relative_path)
    return any(p == prefix or p.startswith(prefix + "/") for prefix in REFERENCE_CORPUS_PREFIXES)


# --- サニタイズ ---------------------------------------------------------------

_SPECIAL_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
# JS の `\s`(RegExp)は `.trim()` と同じ WhiteSpace/LineTerminator 集合に
# マッチする(U+FEFF を含む等、Python の既定 `\s` とは集合が異なる)。
# `frontmatter.py` の `JS_WHITESPACE_CLASS` を再利用して揃える。
_WHITESPACE_RE = re.compile(f"[{JS_WHITESPACE_CLASS}]+")
_DASH_RUN_RE = re.compile(r"-+")
# `frontmatter.py` の `_PLAIN_SAFE_RE` と同じ理由(JS の `$` は Python と違い
# 末尾の改行の直前にはマッチしない)で `\Z` を使う。
_EDGE_DASH_RE = re.compile(r"^-|-\Z")

_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
)

_MAX_FILENAME_BYTES = 255
_TRUNCATION_SUFFIX = "..."


def _replace_unsafe_chars(name: str) -> str:
    """4つの置換だけを行う共通ステップ(`safe_batch_name` と `sanitize_file_name` の前段)。"""
    s = _SPECIAL_CHARS_RE.sub("-", name)
    s = _WHITESPACE_RE.sub("-", s)
    s = _DASH_RUN_RE.sub("-", s)
    return _EDGE_DASH_RE.sub("", s)


def safe_batch_name(name: Any) -> str:
    """`download-batch.js` の `generateFolderName` と同一ロジック(4置換のみ)。

    `sanitize_file_name` とは別物。予約名プレフィックスや255バイト切詰は行わない。
    """
    return _replace_unsafe_chars(str(name))


def sanitize_file_name(name: Any) -> str:
    """ダウンロードしたファイルの実ファイル名を安全にする。

    `safe_batch_name` の4置換に加え、Windows予約名への `file-` 接頭辞付与と
    255 UTF-8バイトでの切詰(マルチバイト文字の途中では切らない)を行う。
    """
    if not name:
        return "untitled"

    sanitized = _replace_unsafe_chars(str(name))
    if not sanitized:
        return "untitled"

    upper = sanitized.upper()
    is_reserved = any(
        upper == reserved or upper.startswith(reserved + ".")
        for reserved in _WINDOWS_RESERVED_NAMES
    )
    if is_reserved:
        sanitized = "file-" + sanitized

    encoded = sanitized.encode("utf-8")
    if len(encoded) > _MAX_FILENAME_BYTES:
        suffix_bytes = len(_TRUNCATION_SUFFIX.encode("utf-8"))
        max_content_bytes = _MAX_FILENAME_BYTES - suffix_bytes
        truncated = ""
        truncated_bytes = 0
        for ch in sanitized:
            ch_bytes = len(ch.encode("utf-8"))
            if truncated_bytes + ch_bytes > max_content_bytes:
                break
            truncated += ch
            truncated_bytes += ch_bytes
        sanitized = truncated + _TRUNCATION_SUFFIX

    return sanitized


def sanitize_category_path(category_path: Any) -> str:
    """カテゴリパスを安全なディレクトリ名に変換する(セグメント単位で `sanitize_file_name`)。"""
    if not category_path:
        return ""
    parts = [part for part in str(category_path).split("/") if part.strip() != ""]
    return "/".join(sanitize_file_name(part) for part in parts)


def extract_repo_name(repository: Any) -> str:
    """git リポジトリURLからリポジトリ名を取り出す(SSH形式の user:repo も処理)。"""
    cleaned = to_posix_path(repository)
    # `\Z` を使う理由は `_PLAIN_SAFE_RE`(frontmatter.py)と同じ:
    # JS の `$` は文字列の絶対末尾にしかマッチしないが、Python の既定 `$` は
    # 末尾の改行の直前にもマッチしてしまうため、末尾に改行を含む repository
    # 値で JS と異なる結果(`.git` サフィックスが剥がれない)になりうる。
    cleaned = re.sub(r"\.git\Z", "", cleaned)
    cleaned = re.sub(r"/\Z", "", cleaned)
    parts = [p for p in cleaned.split("/") if p]
    last = parts[-1] if parts else "repository"
    if ":" in last:
        return last.split(":")[-1]
    return last


def _with_docs_prefix(directory: Any) -> str:
    normalized = to_posix_path(directory)
    normalized = re.sub(r"/\Z", "", normalized)
    if normalized == "docs" or normalized.startswith("docs/"):
        return normalized
    return f"docs/{normalized}"


@dataclass(frozen=True, slots=True)
class BatchOutputDir:
    """`resolve_batch_output_dirs` の1要素。"""

    name: str
    type: str
    dir: str


def resolve_batch_output_dirs(batch_configs: dict[str, Any] | None = None) -> list[BatchOutputDir]:
    """batch-config.js の各バッチが書き込む出力先ディレクトリ("docs/xxx")を解決する。

    `download-batch.js` / `download-git.js` の出力先解決と同じ規則(esa=配列、
    web/git=type付きオブジェクト)。type が esa/web/git 以外(または不明)のバッチは
    結果に含めない。
    """
    results: list[BatchOutputDir] = []
    for name, cfg in (batch_configs or {}).items():
        if isinstance(cfg, list):
            esa_dir = f"docs/{safe_batch_name(name)}"
            results.append(BatchOutputDir(name=name, type="esa", dir=esa_dir))
        elif isinstance(cfg, dict) and cfg.get("type") == "web":
            output_dir = cfg.get("outputDir") or safe_batch_name(name)
            results.append(BatchOutputDir(name=name, type="web", dir=_with_docs_prefix(output_dir)))
        elif isinstance(cfg, dict) and cfg.get("type") == "git":
            if cfg.get("outputDir"):
                directory = _with_docs_prefix(cfg["outputDir"])
            else:
                directory = f"docs/{extract_repo_name(cfg.get('repository') or '')}"
            results.append(BatchOutputDir(name=name, type="git", dir=directory))
    return results


def resolve_git_output_dirs(batch_configs: dict[str, Any] | None = None) -> list[str]:
    """git バッチの出力先ディレクトリ一覧("docs/xxx")。重複は除去、初出順を維持する。"""
    seen: dict[str, None] = {}
    for entry in resolve_batch_output_dirs(batch_configs):
        if entry.type == "git":
            seen.setdefault(entry.dir, None)
    return list(seen)


# --- 由来推定 ------------------------------------------------------------------

_POST_NUMBER_STR_RE = re.compile(r"^[0-9]+$")
_SPEC_PATH_RE = re.compile(r"(^|/)(要件|仕様|仕様書|requirements?|spec)(/|$)", re.IGNORECASE)
_MEETING_PATH_RE = re.compile(r"(議事録|定例|meeting|ミーティング|打合せ|打ち合わせ|キックオフ)")
_MEMO_PATH_ANCHORED_RE = re.compile(r"(^|/)(メモ|memo|notes?)(/|$)", re.IGNORECASE)
_MEMO_ANYWHERE_RE = re.compile(r"メモ")
_MEETING_CATEGORY_RE = re.compile(r"(議事録|定例)")
_ARCHIVED_PATH_RE = re.compile(r"(^|/)Archived(/|$)", re.IGNORECASE)


def _has_post_number(frontmatter: dict[str, Any]) -> bool:
    value = frontmatter.get("post_number")
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        stripped = value.strip()
        return bool(_POST_NUMBER_STR_RE.fullmatch(stripped)) and int(stripped) > 0
    return False


@dataclass(frozen=True, slots=True)
class _InferredType:
    value: str
    reason: str


def _infer_document_type(posix_path: str, frontmatter: dict[str, Any]) -> _InferredType:
    if is_reference_corpus(posix_path):
        return _InferredType("reference", "参照コーパス配下")

    if _SPEC_PATH_RE.search(posix_path):
        return _InferredType("specification", "パスに要件/仕様を含む")
    if _MEETING_PATH_RE.search(posix_path):
        return _InferredType("meeting", "パスに議事録/定例を含む")
    if _MEMO_PATH_ANCHORED_RE.search(posix_path):
        return _InferredType("memo", "パスにメモを含む")
    if _MEMO_ANYWHERE_RE.search(posix_path):
        return _InferredType("memo", "パスにメモを含む")

    category = frontmatter.get("category")
    if category and _MEETING_CATEGORY_RE.search(str(category)):
        return _InferredType("meeting", "category に議事録/定例を含む")

    return _InferredType("knowledge", "既定値")


@dataclass(frozen=True, slots=True)
class DocumentClassification:
    """`classify_document` の戻り値。"""

    source: str
    managed_by: str
    document_type: str
    status: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    conflict: bool = False


def classify_document(
    *,
    relative_path: Any,
    frontmatter: dict[str, Any] | None = None,
    git_output_dirs: list[str] | None = None,
) -> DocumentClassification:
    """文書の由来・種類・業務状態を推定する。

    既に frontmatter に値がある場合は常にそれを優先する(人間が書いた値を機械が
    上書きしない)。推定の根拠は reasons に必ず残し、dry-run で検証できるようにする。
    """
    fm = frontmatter or {}
    posix_path = to_posix_path(relative_path)
    reasons: list[str] = []
    conflict = False

    declared_source = fm.get("source")
    declared_source = declared_source.strip().lower() if isinstance(declared_source, str) else ""

    git_prefixes = [
        p
        for d in (git_output_dirs or [])
        if (p := re.sub(r"/\Z", "", re.sub(r"^docs/", "", to_posix_path(d))))
    ]
    in_git_dir = any(
        posix_path == prefix or posix_path.startswith(prefix + "/") for prefix in git_prefixes
    )

    if _has_post_number(fm):
        inferred_source = Source.ESA.value
        reasons.append(f"post_number={fm.get('post_number')} → esa")
    elif declared_source == Source.WEB.value:
        inferred_source = Source.WEB.value
        reasons.append("frontmatter の source: web → web")
    elif in_git_dir:
        inferred_source = Source.GIT.value
        reasons.append("git バッチ出力先配下 → git")
    else:
        inferred_source = Source.MANUAL.value
        reasons.append("esa/web/git のいずれの条件にも該当せず → manual")

    source = inferred_source
    valid_sources = {s.value for s in Source}
    if declared_source and declared_source in valid_sources and declared_source != inferred_source:
        source = declared_source
        conflict = True
        reasons.append(f"既存の source: {declared_source} を優先（推定は {inferred_source}）")

    declared_managed_by = fm.get("managed_by")
    declared_managed_by = (
        declared_managed_by.strip() if isinstance(declared_managed_by, str) else ""
    )
    managed_by = MANAGED_BY_FOR_SOURCE[Source(source)].value
    valid_managed_by = {m.value for m in ManagedBy}
    if declared_managed_by and declared_managed_by in valid_managed_by:
        if declared_managed_by != managed_by:
            conflict = True
            reasons.append(
                f"既存の managed_by: {declared_managed_by} を優先（推定は {managed_by}）"
            )
        managed_by = declared_managed_by

    declared_type = fm.get("document_type")
    declared_type = declared_type.strip() if isinstance(declared_type, str) else ""
    valid_types = {t.value for t in DocumentType}
    if declared_type and declared_type in valid_types:
        document_type = declared_type
        reasons.append(f"既存の document_type: {declared_type} を維持")
    else:
        inferred = _infer_document_type(posix_path, fm)
        document_type = inferred.value
        reasons.append(f"document_type={inferred.value}（{inferred.reason}）")

    declared_status = fm.get("status")
    declared_status = declared_status.strip() if isinstance(declared_status, str) else ""
    valid_statuses = {s.value for s in Status}
    if declared_status and declared_status in valid_statuses:
        status = declared_status
        reasons.append(f"既存の status: {declared_status} を維持")
    elif _ARCHIVED_PATH_RE.search(posix_path):
        status = Status.ARCHIVED.value
        reasons.append("パスに Archived を含む → archived")
    else:
        status = Status.ACTIVE.value
        reasons.append("status=active（既定値）")

    return DocumentClassification(
        source=source,
        managed_by=managed_by,
        document_type=document_type,
        status=status,
        reasons=tuple(reasons),
        conflict=conflict,
    )


@dataclass(frozen=True, slots=True)
class MetadataValidation:
    """`validate_metadata` の戻り値。"""

    ok: bool
    errors: tuple[str, ...]


def validate_metadata(meta: dict[str, Any] | None = None) -> MetadataValidation:
    """メタデータの列挙値と整合性を検証する。"""
    m = meta or {}
    errors: list[str] = []

    def check(key: str, allowed: type[StrEnum]) -> None:
        value = m.get(key)
        if value is None or value == "":
            return
        allowed_values = [a.value for a in allowed]
        if value not in allowed_values:
            errors.append(f'{key}: "{value}" は不正（許可値: {" | ".join(allowed_values)}）')

    check("source", Source)
    check("managed_by", ManagedBy)
    check("document_type", DocumentType)
    check("status", Status)

    from abist_kb.domain.sync_policy import SyncStatus

    check("sync_status", SyncStatus)

    source = m.get("source")
    managed_by = m.get("managed_by")
    if (
        source
        and managed_by
        and source in {s.value for s in Source}
        and managed_by in {mb.value for mb in ManagedBy}
        and MANAGED_BY_FOR_SOURCE[Source(source)].value != managed_by
    ):
        expected = MANAGED_BY_FOR_SOURCE[Source(source)].value
        errors.append(f"managed_by: source={source} には {expected} が対応（指定は {managed_by}）")

    return MetadataValidation(ok=len(errors) == 0, errors=tuple(errors))


__all__ = [
    "BACKFILL_KEYS",
    "FRONTMATTER_KEY_ORDER",
    "MANAGED_BY_FOR_SOURCE",
    "REFERENCE_CORPUS_PREFIXES",
    "BatchOutputDir",
    "DocumentClassification",
    "DocumentType",
    "ManagedBy",
    "MetadataValidation",
    "Source",
    "Status",
    "classify_document",
    "extract_repo_name",
    "is_reference_corpus",
    "resolve_batch_output_dirs",
    "resolve_git_output_dirs",
    "safe_batch_name",
    "sanitize_category_path",
    "sanitize_file_name",
    "to_posix_path",
    "validate_metadata",
]
