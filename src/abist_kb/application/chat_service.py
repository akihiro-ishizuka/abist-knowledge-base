"""`ChatService`(設計書 §7.1, M7 task-1)。

`SearchService` で根拠(evidence)を取得 → コンテキストを組み立てて
`ChatProvider`(境界、`infrastructure.ai.chat_provider`)へストリーミング応答を
依頼 → **引用検証** → 会話履歴を DB(`conversations`/`messages`/`citations`)へ
永続化する。

**引用検証が本サービスの核心。** 旧システムの `chat-server.js` は出典を提示する
だけで実在を確認しなかった。モデルがそれらしい行範囲を捏造しても、検証なしでは
「出典付きに見える」だけの回答になる。ここではモデルが返した各引用
(`path`/`start_line`/`end_line`)について、`domain.line_range.range_hash`
(`get_document`・可視化の source-verifier と同じ実装)で実ファイルへ引き直し、
範囲が実在するか(`RANGE_OUT_OF_BOUNDS` にならないか)を確認する。**検証に
失敗した引用は黙って捨てない**: `citations` テーブルには `valid=0` と理由を
付けて必ず記録し、`ChatAnswer.citation_warnings` として呼び出し側にも提示する
(表示からこっそり消すと「検証した上で正しい」と誤読されるため)。

会話履歴はサーバー側 DB に持つ(旧版はクライアント側保持で、リロードや別
サーフェス間の共有ができなかった。この設計判断を変える)。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from abist_kb.application.search_service import SearchService
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.domain.line_range import range_hash
from abist_kb.infrastructure.ai.chat_provider import (
    ChatProvider,
    RawCitation,
    split_citations_block,
)
from abist_kb.infrastructure.db.connection import transaction

SYSTEM_PROMPT = (
    "あなたは社内ナレッジベースの質問応答アシスタントです。"
    "与えられた参考文献の範囲内で回答し、根拠のない推測は避けてください。"
)


@dataclass(frozen=True, slots=True)
class ValidatedCitation:
    """検証済みの引用。`valid=False` でも破棄せず保持する。"""

    path: str
    start_line: int
    end_line: int
    valid: bool
    range_hash: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ChatAnswer:
    conversation_id: str
    message_id: str
    text: str
    citations: tuple[ValidatedCitation, ...] = field(default_factory=tuple)

    @property
    def citation_warnings(self) -> tuple[str, ...]:
        """検証に失敗した引用についての警告文(呼び出し側が黙って落とさず提示するため)。"""
        return tuple(
            f"引用 {c.path}:{c.start_line}-{c.end_line} を検証できませんでした({c.reason})"
            for c in self.citations
            if not c.valid
        )


class ChatService:
    """検索・引用検証・履歴永続化をまとめるアプリケーションサービス。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        search_service: SearchService,
        provider: ChatProvider,
        docs_dir: Path,
        corpus: str = "work",
        evidence_limit: int = 5,
    ) -> None:
        self._conn = conn
        self._search = search_service
        self._provider = provider
        self._docs_dir = docs_dir
        self._corpus = corpus
        self._evidence_limit = evidence_limit

    # -- 会話 -------------------------------------------------------------

    def start_conversation(self, *, title: str | None = None) -> str:
        conversation_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, title, now, now),
            )
        return conversation_id

    def history(self, conversation_id: str) -> list[dict[str, Any]]:
        self._require_conversation(conversation_id)
        rows = self._conn.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _require_conversation(self, conversation_id: str) -> None:
        row = self._conn.execute(
            "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise AppError(
                code=ErrorCode.NOT_FOUND, message=f"会話が見つかりません: {conversation_id}"
            )

    # -- 根拠取得・コンテキスト構築 -------------------------------------------

    def _build_context(self, question: str) -> tuple[str, list[dict[str, Any]]]:
        result = self._search.search(question, corpus=self._corpus, limit=self._evidence_limit)
        evidence = result["results"]
        blocks = []
        for item in evidence:
            blocks.append(
                f"[{item['path']}:{item.get('start_line')}-{item.get('end_line')}]\n"
                f"{item.get('snippet', '')}"
            )
        return "\n\n".join(blocks), evidence

    # -- 引用検証 -----------------------------------------------------------

    def _validate_citation(self, citation: RawCitation) -> ValidatedCitation:
        absolute = (self._docs_dir / citation.path).resolve()
        try:
            absolute.relative_to(self._docs_dir.resolve())
        except ValueError:
            return ValidatedCitation(
                path=citation.path,
                start_line=citation.start_line,
                end_line=citation.end_line,
                valid=False,
                range_hash=None,
                reason="docs/ の外を指しています",
            )
        try:
            text = absolute.read_text(encoding="utf-8")
        except OSError:
            return ValidatedCitation(
                path=citation.path,
                start_line=citation.start_line,
                end_line=citation.end_line,
                valid=False,
                range_hash=None,
                reason="文書が見つかりません",
            )

        hashed = range_hash(text, citation.start_line, citation.end_line)
        if not hashed.ok:
            return ValidatedCitation(
                path=citation.path,
                start_line=citation.start_line,
                end_line=citation.end_line,
                valid=False,
                range_hash=None,
                reason=f"行範囲が実在しません(全{hashed.total_lines}行): {hashed.reason}",
            )
        return ValidatedCitation(
            path=citation.path,
            start_line=citation.start_line,
            end_line=citation.end_line,
            valid=True,
            range_hash=hashed.hash,
            reason=None,
        )

    # -- 質問応答 -----------------------------------------------------------

    def ask(self, conversation_id: str, question: str) -> ChatAnswer:
        self._require_conversation(conversation_id)
        context, _evidence = self._build_context(question)

        history_rows = self.history(conversation_id)
        messages = [{"role": row["role"], "content": row["content"]} for row in history_rows]
        messages.append({"role": "user", "content": question})

        raw_text = ""
        raw_citations: tuple[RawCitation, ...] = ()
        for chunk in self._provider.stream(
            system=SYSTEM_PROMPT, messages=messages, context=context
        ):
            raw_text += chunk.delta
            if chunk.done:
                raw_citations = chunk.citations

        display_text, _ = split_citations_block(raw_text)
        validated = tuple(self._validate_citation(c) for c in raw_citations)

        now = datetime.now(UTC).isoformat()
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (?, ?, 'user', ?, ?)",
                (str(uuid4()), conversation_id, question, now),
            )
            answer_id = str(uuid4())
            self._conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (?, ?, 'assistant', ?, ?)",
                (answer_id, conversation_id, display_text, now),
            )
            for citation in validated:
                self._conn.execute(
                    "INSERT INTO citations "
                    "(id, message_id, path, start_line, end_line, "
                    "range_hash, valid, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid4()),
                        answer_id,
                        citation.path,
                        citation.start_line,
                        citation.end_line,
                        citation.range_hash,
                        1 if citation.valid else 0,
                        citation.reason,
                        now,
                    ),
                )
            self._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )

        return ChatAnswer(
            conversation_id=conversation_id,
            message_id=answer_id,
            text=display_text,
            citations=validated,
        )


__all__ = ["ChatAnswer", "ChatService", "SYSTEM_PROMPT", "ValidatedCitation"]
