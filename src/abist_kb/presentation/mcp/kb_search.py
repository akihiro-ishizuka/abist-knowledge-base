"""kb-search MCP サーバーの4ツール(M5 task-2-brief)。

`SearchService`/`IndexService`(M4)をそのまま呼び、fixture
(`tests/fixtures/mcp/kb-search/**/*.json`)と同じ形の応答を組み立てる。
work/reference の2索引DB接続は遅延オープンし、`KbSearchTools` インスタンスの
寿命の間キャッシュする(索引ファイルは読み取り専用で開くため、複数回開いても
安全だがコネクション使い回しの方が軽い)。

`get_document` のパス封じ込めは `Path.resolve()` + `relative_to()` で行う
(`docs_dir.startswith` 相当の文字列比較はしない — `docs-backup` のような
兄弟ディレクトリが `docs` の prefix 一致で素通りしてしまう旧実装のバグを
再現しない。応答の形は fixture のまま変えない)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import mcp.types as types

from abist_kb.application.index_service import IndexService
from abist_kb.application.search_service import SearchService
from abist_kb.domain.line_range import range_hash, split_doc_lines
from abist_kb.infrastructure.db.connection import connect
from abist_kb.presentation.mcp.payloads import error_result, ok_result

#: `search_kb`/`get_document`/`get_chunk` の docstring は `tools/list` fixture
#: (`tests/fixtures/mcp/tools-list.json`)から一字一句転記する(M5 は tools/list
#: のスキーマ互換もビット互換契約の対象)。
TOOL_DESCRIPTIONS: dict[str, str] = {
    "search_kb": (
        "ナレッジベースを検索し、出典(パス・行番号)付きで返す。全文検索(FTS5/BM25)"
        "を基本とし、埋め込みがある場合はベクトル検索と RRF で統合する。結果の "
        "index_stale が true の場合、索引後にファイルが変わっているため行番号を"
        "信用せず get_document で読み直すこと。同一記事が複数パスに存在する場合は"
        "1件に畳み、other_paths に残りを示す。"
    ),
    "get_document": (
        "文書の原文を取得する。検索結果の行番号が陳腐化している場合や、前後の文脈を"
        "確認したい場合に使う。行範囲を指定すると該当部分だけを行番号付きで返し、"
        "その範囲の content_hash(range_hash)も返す(可視化の SceneSpec "
        "sources[].content_hash にはこの値を使う)。"
    ),
    "get_chunk": "検索結果の chunk_id からチャンク本文とその行範囲を取得する。",
    "index_status": (
        "コーパスごとの索引の状態(文書数・チャンク数・埋め込み数・tokenizer・"
        "最終索引日時)を返す。検索結果が不自然なときや、索引が最新かを確認したい"
        "ときに使う。"
    ),
}


def list_tools() -> list[types.Tool]:
    """`tools/list` に返す4ツールのスキーマ(`tests/fixtures/mcp/tools-list.json` 準拠)。"""
    return [
        types.Tool(
            name="search_kb",
            description=TOOL_DESCRIPTIONS["search_kb"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "corpus": {
                        "description": (
                            "検索対象。work=実務資料(既定) / reference=CATIA原本(B32doc)"
                        ),
                        "enum": ["work", "reference"],
                        "type": "string",
                    },
                    "document_type": {
                        "description": "文書種類で絞る",
                        "enum": ["meeting", "specification", "knowledge", "memo", "reference"],
                        "type": "string",
                    },
                    "limit": {
                        "description": "返す件数(既定10)",
                        "maximum": 50,
                        "minimum": 1,
                        "type": "integer",
                    },
                    "path_prefix": {
                        "description": "docs/ からの相対パス接頭辞で絞る",
                        "type": "string",
                    },
                    "query": {
                        "description": "検索したい質問または識別子(日本語自然文可)",
                        "minLength": 1,
                        "type": "string",
                    },
                    "source": {
                        "description": "由来で絞る",
                        "enum": ["esa", "web", "git", "manual"],
                        "type": "string",
                    },
                    "status": {
                        "description": "業務状態で絞る",
                        "enum": ["active", "deprecated", "archived"],
                        "type": "string",
                    },
                },
                "required": ["query"],
                "type": "object",
            },
        ),
        types.Tool(
            name="get_document",
            description=TOOL_DESCRIPTIONS["get_document"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "context": {
                        "description": "前後に含める行数(既定0)",
                        "maximum": 50,
                        "minimum": 0,
                        "type": "integer",
                    },
                    "end_line": {"description": "終了行", "minimum": 1, "type": "integer"},
                    "path": {
                        "description": "docs/ からの相対パス",
                        "minLength": 1,
                        "type": "string",
                    },
                    "start_line": {
                        "description": "開始行(1始まり)",
                        "minimum": 1,
                        "type": "integer",
                    },
                },
                "required": ["path"],
                "type": "object",
            },
        ),
        types.Tool(
            name="get_chunk",
            description=TOOL_DESCRIPTIONS["get_chunk"],
            inputSchema={
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
                "properties": {
                    "chunk_id": {
                        "description": "search_kb が返した chunk_id",
                        "exclusiveMinimum": 0,
                        "type": "integer",
                    },
                    "corpus": {
                        "description": "chunk_id が属するコーパス(既定 work)",
                        "enum": ["work", "reference"],
                        "type": "string",
                    },
                },
                "required": ["chunk_id"],
                "type": "object",
            },
        ),
        types.Tool(
            name="index_status",
            description=TOOL_DESCRIPTIONS["index_status"],
            inputSchema={"properties": {}, "type": "object"},
        ),
    ]


def _camelize_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    """`search_engine.search()` の snake_case diagnostics を fixture の camelCase へ。

    `elapsedMs` は fixture 上も文字列(`"789.2"`)であり、かつ
    `nondeterministic_fields` として比較対象から除外される唯一のフィールド
    (`PROVENANCE.md` §3)。
    """
    return {
        "query": diagnostics["query"],
        "terms": diagnostics["terms"],
        "tokenizer": diagnostics["tokenizer"],
        "keywordCandidates": diagnostics["keyword_candidates"],
        "shortTermCandidates": diagnostics["short_term_candidates"],
        "vectorCandidates": diagnostics["vector_candidates"],
        "identifierOnlyQuery": diagnostics["identifier_only_query"],
        "vectorAvailable": diagnostics["vector_available"],
        "elapsedMs": f"{diagnostics['elapsed_ms']:.1f}",
    }


def _camelize_corpus_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": status["available"],
        "label": status["label"],
        "documents": status["documents"],
        "chunks": status["chunks"],
        "embeddedChunks": status["embedded_chunks"],
        "tokenizers": status["tokenizers"],
        "lastIndexedAt": status["last_indexed_at"],
        "chunkOptions": status["chunk_options"],
        "embeddingModel": status["embedding_model"],
        "indexSizeMb": status["index_size_mb"],
        "vectorSearchAvailable": status["vector_search_available"],
    }


class KbSearchTools:
    """kb-search の4ツールを実装するアダプタ。work/reference の索引接続を遅延キャッシュする。"""

    def __init__(
        self, *, docs_dir: Path, work_index_path: Path, reference_index_path: Path
    ) -> None:
        self._docs_dir = docs_dir.resolve()
        self._index_paths: dict[str, Path] = {
            "work": work_index_path,
            "reference": reference_index_path,
        }
        self._connections: dict[str, sqlite3.Connection] = {}
        self._search_service = SearchService(
            docs_dir=docs_dir,
            work_index_path=work_index_path,
            reference_index_path=reference_index_path,
        )
        self._index_service = IndexService(
            docs_dir=docs_dir,
            app_db_path=docs_dir,  # index_status は app_db に触れないため未使用ダミー
            work_index_path=work_index_path,
            reference_index_path=reference_index_path,
        )

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()

    def _conn(self, corpus: str) -> sqlite3.Connection:
        if corpus not in self._connections:
            self._connections[corpus] = connect(self._index_paths[corpus], read_only=True)
        return self._connections[corpus]

    # -- search_kb ---------------------------------------------------------

    def search_kb(self, arguments: dict[str, Any]) -> types.CallToolResult:
        query = arguments["query"]
        result = self._search_service.search(
            query,
            corpus=arguments.get("corpus", "work"),
            source=arguments.get("source"),
            document_type=arguments.get("document_type"),
            status=arguments.get("status"),
            path_prefix=arguments.get("path_prefix"),
            limit=arguments.get("limit", 10),
        )
        payload = {
            "ok": True,
            "count": result["count"],
            "results": result["results"],
            "diagnostics": _camelize_diagnostics(result["diagnostics"]),
            "note": result["note"],
        }
        return ok_result(payload)

    # -- get_document --------------------------------------------------------

    def _resolve_within_docs(self, path: str) -> Path | None:
        """`docs_dir` の外を指す場合は `None` を返す。

        prefix 文字列比較(旧実装の `startswith(DOCS_DIR)`)ではなく
        `Path.resolve()` + `relative_to()` で判定する。
        """
        candidate = (self._docs_dir / path).resolve()
        try:
            candidate.relative_to(self._docs_dir)
        except ValueError:
            return None
        return candidate

    def get_document(self, arguments: dict[str, Any]) -> types.CallToolResult:
        raw_path = arguments["path"]
        resolved = self._resolve_within_docs(raw_path)
        if resolved is None:
            return error_result("docs/ の外は参照できません")

        try:
            text = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return error_result(f"{type(exc).__name__}: {exc}")

        conn = self._conn("work")
        row = conn.execute(
            "SELECT title, url, status, source, post_number, document_hash, indexed_at "
            "FROM documents WHERE path = ?",
            (raw_path.replace("\\", "/"),),
        ).fetchone()

        lines = split_doc_lines(text)
        total_lines = len(lines)

        start_line = arguments.get("start_line")
        end_line = arguments.get("end_line")
        context = arguments.get("context", 0)

        range_payload: dict[str, int] | None = None
        range_hash_value: str | None = None
        if start_line is not None or end_line is not None:
            effective_start = start_line if start_line is not None else end_line
            effective_end = end_line if end_line is not None else start_line
            effective_start = max(1, effective_start - context)
            effective_end = min(total_lines, effective_end + context)
            hashed = range_hash(text, effective_start, effective_end)
            if not hashed.ok:
                return error_result(
                    f"行範囲が不正です(1〜{total_lines}): {effective_start}-{effective_end}"
                )
            assert hashed.hash is not None
            content = "\n".join(
                f"{i:5d} | {lines[i - 1]}" for i in range(effective_start, effective_end + 1)
            )
            range_payload = {"from": effective_start, "to": effective_end}
            range_hash_value = hashed.hash
        else:
            content = "\n".join(lines)

        index_stale = row["document_hash"] != _hash_body(text) if row is not None else True

        payload: dict[str, Any] = {
            "ok": True,
            "path": raw_path,
            "title": row["title"] if row is not None else None,
            "url": row["url"] if row is not None else None,
            "status": row["status"] if row is not None else None,
            "source": row["source"] if row is not None else None,
            "updated_at": _frontmatter_updated_at(text),
            "post_number": row["post_number"] if row is not None else None,
            "total_lines": total_lines,
            "range": range_payload,
        }
        if range_hash_value is not None:
            payload["range_hash"] = range_hash_value
        payload.update(
            {
                "index_stale": index_stale,
                "indexed_at": row["indexed_at"] if row is not None else None,
                "content": content,
            }
        )
        return ok_result(payload)

    # -- get_chunk ----------------------------------------------------------

    def get_chunk(self, arguments: dict[str, Any]) -> types.CallToolResult:
        chunk_id = arguments["chunk_id"]
        corpus = arguments.get("corpus", "work")
        conn = self._conn(corpus)
        row = conn.execute(
            "SELECT c.id AS chunk_id, c.path AS path, c.heading_path AS heading_path, "
            "c.start_line AS start_line, c.end_line AS end_line, c.text AS text, "
            "d.title AS title, d.url AS url, d.status AS status, d.source AS source, "
            "d.post_number AS post_number, d.indexed_at AS indexed_at, "
            "d.document_hash AS document_hash "
            "FROM chunks c JOIN documents d ON d.path = c.path WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return error_result(f"chunk_id {chunk_id} は存在しません")

        docs_path = self._docs_dir / row["path"]
        index_stale = True
        try:
            current_text = docs_path.read_text(encoding="utf-8")
            index_stale = _hash_body(current_text) != row["document_hash"]
        except OSError:
            index_stale = True

        payload = {
            "ok": True,
            "chunk_id": row["chunk_id"],
            "path": row["path"],
            "title": row["title"],
            "heading_path": row["heading_path"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "url": row["url"],
            "status": row["status"],
            "source": row["source"],
            "post_number": row["post_number"],
            "indexed_at": row["indexed_at"],
            "index_stale": index_stale,
            "text": row["text"],
        }
        return ok_result(payload)

    # -- index_status ---------------------------------------------------------

    def index_status(self, _arguments: dict[str, Any]) -> types.CallToolResult:
        status = self._index_service.status()
        payload = {
            "ok": True,
            "defaultCorpus": status["default_corpus"],
            "corpora": {
                corpus: _camelize_corpus_status(value)
                for corpus, value in status["corpora"].items()
            },
        }
        return ok_result(payload)


def _hash_body(text: str) -> str:
    from abist_kb.domain.frontmatter import hash_body

    return hash_body(text)


def _frontmatter_updated_at(text: str) -> str | None:
    from abist_kb.domain.frontmatter import parse_frontmatter

    parsed = parse_frontmatter(text)
    value = parsed.data.get("updated_at")
    return value if isinstance(value, str) else None


__all__ = ["KbSearchTools", "list_tools"]
