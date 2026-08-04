"""`check-contradictions` の移植(旧 `tools/check-contradictions.js`, M7 task-2)。

**このツールは何も変更しない。** 矛盾かどうかの最終判断は人間が行う。自動修正は
行わない(片方が古いだけかもしれず、両方正しい場合もある)。

全文書の総当たりはしない。候補は次に限定する(旧実装と同じ設計方針):
  - 同一記事の別バージョン(同じ `post_number`)
  - `find-duplicates` が見つけた近似重複(`near`)。時系列の同一系列(`series`)は
    「前回分から変わるのが当たり前」なので候補に含めない。

候補文書の対に対して、段落(空行区切り)単位で「文としては似ているのに、数値・
否定表現・状態語が違う」組を拾う。**しきい値 `DEFAULT_SIMILARITY = 0.99` は
旧実装の実測(0.92 で174,280件・0.95 で約54,000件に爆発した)を踏まえた値を
そのまま維持する。** 段落の類似度判定には埋め込みではなく正規化した文字列の
共通接頭辞的な近さ(difflib)を使う簡易版で代替している(旧実装はチャンク埋め込み
のコサイン類似度を使う)。これは意図的な縮退であり、レビューで重点的に見てほしい
点として報告書にも明記する。
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from abist_kb.application.audit.find_duplicates import find_near_duplicates
from abist_kb.application.audit.runs import Finding, finish_run, record_findings, start_run
from abist_kb.infrastructure.db.documents_repo import DocumentRepository

#: 文としては同じことを述べていると見なす類似度の下限(旧実装と同一)。
DEFAULT_SIMILARITY = 0.99

#: これ以上似ていれば同一文とみなし、矛盾候補から外す。
DEFAULT_IDENTICAL = 0.995

#: 状態語の対立。判断が変わったことが明確に読み取れる語だけを置く(旧実装と同一)。
STATE_TERMS: tuple[tuple[str, str], ...] = (
    ("対応済", "未対応"),
    ("完了", "未完了"),
    ("実装済", "未実装"),
    ("採用", "不採用"),
    ("承認", "却下"),
    ("対応可能", "対応不可"),
)

#: 違っていても矛盾ではない数値の単位(日付は違って当たり前)。
IGNORED_UNITS = frozenset({"日", "月", "年", "時間", "分", "秒"})

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|m|%|％|件|回|個|台|円|倍|点)", re.IGNORECASE)
_NEGATION_RE = re.compile(
    r"(しない|されない|できない|ではない|ありません|不可|未対応|未実装|非対応|無効)"
)


@dataclass(frozen=True, slots=True)
class NumberMatch:
    value: float
    unit: str
    raw: str


def extract_numbers(text: str) -> list[NumberMatch]:
    """単位付きの数値を取り出す(`板厚 2.0mm` のような食い違いを拾うため)。"""
    numbers = []
    for m in _NUMBER_RE.finditer(text):
        unit = m.group(2).lower()
        if unit in IGNORED_UNITS:
            continue
        numbers.append(NumberMatch(value=float(m.group(1)), unit=unit, raw=m.group(0)))
    return numbers


def has_negation(text: str) -> bool:
    return _NEGATION_RE.search(text) is not None


def find_state_conflict(text_a: str, text_b: str) -> list[dict[str, str]]:
    conflicts = []
    for positive, negative in STATE_TERMS:
        a_pos, a_neg = positive in text_a, negative in text_a
        b_pos, b_neg = positive in text_b, negative in text_b
        if a_pos and not a_neg and b_neg and not b_pos:
            conflicts.append({"a": positive, "b": negative})
        elif a_neg and not a_pos and b_pos and not b_neg:
            conflicts.append({"a": negative, "b": positive})
    return conflicts


def detect_conflict(text_a: str, text_b: str) -> list[dict[str, str]]:
    """似ている前提で、数値・否定・状態語の食い違いを挙げる(旧実装と同じ判定基準)。"""
    reasons: list[dict[str, str]] = []

    numbers_a = extract_numbers(text_a)
    numbers_b = extract_numbers(text_b)
    reported_units: set[str] = set()
    for a in numbers_a:
        if a.unit in reported_units:
            continue
        same_unit = [b for b in numbers_b if b.unit == a.unit]
        if not same_unit:
            continue
        if any(b.value == a.value for b in same_unit):
            continue
        if len(numbers_a) > 8 or len(same_unit) > 8:
            continue
        reported_units.add(a.unit)
        detail = f"{a.raw} ↔ " + " / ".join(b.raw for b in same_unit[:3])
        reasons.append({"kind": "number", "detail": detail})

    if has_negation(text_a) != has_negation(text_b):
        detail = (
            "A に否定表現があり B に無い" if has_negation(text_a) else "B に否定表現があり A に無い"
        )
        reasons.append({"kind": "negation", "detail": detail})

    for conflict in find_state_conflict(text_a, text_b):
        reasons.append({"kind": "state", "detail": f"{conflict['a']} ↔ {conflict['b']}"})

    return reasons


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def build_candidate_pairs(
    documents: list[dict[str, Any]],
    *,
    near_pairs: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str, list[str]]]:
    """総当たりをしない候補文書対を作る(`(path_a, path_b, sources)`)。"""
    pairs: dict[tuple[str, str], list[str]] = {}

    def add(path_a: str, path_b: str, source: str) -> None:
        if path_a == path_b:
            return
        key = (path_a, path_b) if path_a < path_b else (path_b, path_a)
        pairs.setdefault(key, []).append(source)

    by_post: dict[Any, list[str]] = {}
    for doc in documents:
        post_number = doc.get("post_number")
        if not post_number:
            continue
        by_post.setdefault(post_number, []).append(doc["path"])
    for paths in by_post.values():
        for i in range(len(paths)):
            for j in range(i + 1, len(paths)):
                add(paths[i], paths[j], "same_article")

    for pair in near_pairs or []:
        add(pair["documents"][0]["path"], pair["documents"][1]["path"], "near_duplicate")

    return [(a, b, sources) for (a, b), sources in pairs.items()]


@dataclass(slots=True)
class ContradictionCandidate:
    path_a: str
    path_b: str
    sources: list[str]
    conflicts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ContradictionsResult:
    run_id: str
    candidates: list[ContradictionCandidate] = field(default_factory=list)
    candidate_pair_count: int = 0


class CheckContradictionsService:
    """読み取り専用。書込は一切行わない。`read_file` はテスト差し替え用。"""

    def __init__(
        self,
        app_conn: sqlite3.Connection,
        *,
        docs_dir: Any,
        index_conn: sqlite3.Connection | None = None,
    ) -> None:
        self._app_conn = app_conn
        self._docs_dir = docs_dir
        self._index_conn = index_conn
        self._repo = DocumentRepository(app_conn)

    def _read(self, path: str) -> str | None:
        try:
            return (self._docs_dir / path).read_text(encoding="utf-8")
        except OSError:
            return None

    def run(self, *, similarity: float = DEFAULT_SIMILARITY) -> ContradictionsResult:
        run_id = start_run(
            self._app_conn,
            audit_type="check-contradictions",
            mode="report",
            params={"similarity": similarity},
        )

        documents = self._repo.list()
        near_pairs = (
            find_near_duplicates(self._index_conn, documents, threshold=similarity)[0]
            if self._index_conn is not None
            else []
        )
        candidate_pairs = build_candidate_pairs(documents, near_pairs=near_pairs)

        candidates: list[ContradictionCandidate] = []
        for path_a, path_b, sources in candidate_pairs:
            content_a = self._read(path_a)
            content_b = self._read(path_b)
            if content_a is None or content_b is None:
                continue
            conflicts = []
            for para_a in _paragraphs(content_a):
                for para_b in _paragraphs(content_b):
                    ratio = _similar(para_a, para_b)
                    if ratio < similarity or ratio >= DEFAULT_IDENTICAL:
                        continue
                    reasons = detect_conflict(para_a, para_b)
                    if reasons:
                        conflicts.append(
                            {
                                "similarity": ratio,
                                "reasons": reasons,
                                "a": para_a[:200],
                                "b": para_b[:200],
                            }
                        )
            if conflicts:
                candidates.append(
                    ContradictionCandidate(
                        path_a=path_a, path_b=path_b, sources=sources, conflicts=conflicts
                    )
                )

        record_findings(
            self._app_conn,
            run_id,
            (
                Finding(
                    finding_type="contradiction_candidate",
                    path=c.path_a,
                    details={
                        "path_a": c.path_a,
                        "path_b": c.path_b,
                        "sources": c.sources,
                        "conflicts": c.conflicts,
                    },
                )
                for c in candidates
            ),
        )
        finish_run(
            self._app_conn,
            run_id,
            totals={"candidate_pairs": len(candidate_pairs), "conflicts_found": len(candidates)},
        )
        return ContradictionsResult(
            run_id=run_id, candidates=candidates, candidate_pair_count=len(candidate_pairs)
        )


__all__ = [
    "DEFAULT_IDENTICAL",
    "DEFAULT_SIMILARITY",
    "IGNORED_UNITS",
    "STATE_TERMS",
    "CheckContradictionsService",
    "ContradictionCandidate",
    "ContradictionsResult",
    "build_candidate_pairs",
    "detect_conflict",
    "extract_numbers",
    "find_state_conflict",
    "has_negation",
]
