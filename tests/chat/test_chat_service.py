"""`ChatService`: 引用検証(`range_hash`)・履歴永続化(M7 task-1)。

モデルプロバイダは常にモックする(実APIへは絶対に到達しない)。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from abist_kb.application.chat_service import ChatService
from abist_kb.domain.errors import AppError
from abist_kb.infrastructure.ai.chat_provider import ChatChunk, RawCitation
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.db.schema import ensure_app_schema


class FakeSearchService:
    """`ChatService._build_context` が呼ぶ形だけ満たすスタブ。"""

    def __init__(self, results: list[dict]) -> None:
        self._results = results

    def search(self, query: str, *, corpus: str = "work", limit: int = 10) -> dict:
        return {
            "ok": True,
            "count": len(self._results),
            "results": self._results,
            "diagnostics": {},
            "note": "",
        }


class ScriptedProvider:
    """あらかじめ決めたチャンク列をそのまま返すモックプロバイダ。"""

    def __init__(self, chunks: list[ChatChunk]) -> None:
        self._chunks = chunks
        self.calls: list[dict] = []

    def stream(
        self, *, system: str, messages: Sequence[dict[str, str]], context: str
    ) -> Iterator[ChatChunk]:
        self.calls.append({"system": system, "messages": list(messages), "context": context})
        yield from self._chunks


@pytest.fixture
def conn(tmp_root: Path):
    connection = connect(tmp_root / "app.sqlite")
    ensure_app_schema(connection)
    return connection


@pytest.fixture
def docs_dir(tmp_root: Path) -> Path:
    d = tmp_root / "docs"
    d.mkdir(parents=True)
    (d / "a.md").write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    return d


def _service(conn, docs_dir: Path, provider) -> ChatService:
    return ChatService(
        conn,
        search_service=FakeSearchService(
            [{"path": "a.md", "start_line": 1, "end_line": 2, "snippet": "line1\nline2"}]
        ),
        provider=provider,
        docs_dir=docs_dir,
    )


def test_ask_persists_conversation_and_message(conn, docs_dir: Path) -> None:
    provider = ScriptedProvider(
        [
            ChatChunk(delta="こんにちは、"),
            ChatChunk(delta="回答です。"),
            ChatChunk(done=True, citations=(RawCitation(path="a.md", start_line=1, end_line=2),)),
        ]
    )
    service = _service(conn, docs_dir, provider)
    conversation_id = service.start_conversation(title="質問")

    answer = service.ask(conversation_id, "テストの質問です")

    assert answer.text == "こんにちは、回答です。"
    history = service.history(conversation_id)
    assert [row["role"] for row in history] == ["user", "assistant"]
    assert history[0]["content"] == "テストの質問です"
    assert history[1]["content"] == answer.text


def test_valid_citation_is_persisted_with_range_hash(conn, docs_dir: Path) -> None:
    provider = ScriptedProvider(
        [
            ChatChunk(delta="回答"),
            ChatChunk(done=True, citations=(RawCitation(path="a.md", start_line=1, end_line=2),)),
        ]
    )
    service = _service(conn, docs_dir, provider)
    conversation_id = service.start_conversation()

    answer = service.ask(conversation_id, "質問")

    assert len(answer.citations) == 1
    citation = answer.citations[0]
    assert citation.valid is True
    assert citation.range_hash is not None
    assert answer.citation_warnings == ()

    row = conn.execute(
        "SELECT * FROM citations WHERE message_id = ?", (answer.message_id,)
    ).fetchone()
    assert row["valid"] == 1
    assert row["range_hash"] == citation.range_hash


def test_citation_out_of_bounds_is_marked_invalid_and_surfaced(conn, docs_dir: Path) -> None:
    """モデルが実在しない行範囲を捏造した場合、黙って落とさず invalid として提示する。"""
    provider = ScriptedProvider(
        [
            ChatChunk(delta="回答"),
            # a.md は4行しかないのに 10-20 行目を主張する捏造citation
            ChatChunk(done=True, citations=(RawCitation(path="a.md", start_line=10, end_line=20),)),
        ]
    )
    service = _service(conn, docs_dir, provider)
    conversation_id = service.start_conversation()

    answer = service.ask(conversation_id, "質問")

    assert len(answer.citations) == 1
    citation = answer.citations[0]
    assert citation.valid is False
    assert citation.range_hash is None
    assert citation.reason is not None
    assert len(answer.citation_warnings) == 1
    assert "a.md:10-20" in answer.citation_warnings[0]

    row = conn.execute(
        "SELECT * FROM citations WHERE message_id = ?", (answer.message_id,)
    ).fetchone()
    assert row["valid"] == 0
    assert row["range_hash"] is None


def test_citation_pointing_outside_docs_dir_is_rejected(conn, docs_dir: Path) -> None:
    provider = ScriptedProvider(
        [
            ChatChunk(delta="回答"),
            ChatChunk(
                done=True, citations=(RawCitation(path="../secret.md", start_line=1, end_line=1),)
            ),
        ]
    )
    service = _service(conn, docs_dir, provider)
    conversation_id = service.start_conversation()

    answer = service.ask(conversation_id, "質問")

    assert answer.citations[0].valid is False
    assert "docs/" in answer.citations[0].reason


def test_citation_for_missing_document_is_rejected(conn, docs_dir: Path) -> None:
    provider = ScriptedProvider(
        [
            ChatChunk(delta="回答"),
            ChatChunk(
                done=True, citations=(RawCitation(path="missing.md", start_line=1, end_line=1),)
            ),
        ]
    )
    service = _service(conn, docs_dir, provider)
    conversation_id = service.start_conversation()

    answer = service.ask(conversation_id, "質問")

    assert answer.citations[0].valid is False


def test_citations_block_is_stripped_from_display_text(conn, docs_dir: Path) -> None:
    provider = ScriptedProvider(
        [
            ChatChunk(delta="表示テキスト。"),
            ChatChunk(
                delta='```citations\n[{"path": "a.md", "start_line": 1, "end_line": 2}]\n```'
            ),
            ChatChunk(done=True, citations=(RawCitation(path="a.md", start_line=1, end_line=2),)),
        ]
    )
    service = _service(conn, docs_dir, provider)
    conversation_id = service.start_conversation()

    answer = service.ask(conversation_id, "質問")
    assert "```citations" not in answer.text
    assert answer.text == "表示テキスト。"


def test_ask_on_unknown_conversation_raises(conn, docs_dir: Path) -> None:
    provider = ScriptedProvider([ChatChunk(done=True)])
    service = _service(conn, docs_dir, provider)
    with pytest.raises(AppError):
        service.ask("does-not-exist", "質問")


def test_second_question_includes_prior_history_in_provider_call(conn, docs_dir: Path) -> None:
    provider = ScriptedProvider([ChatChunk(delta="A", done=True)])
    service = _service(conn, docs_dir, provider)
    conversation_id = service.start_conversation()
    service.ask(conversation_id, "最初の質問")

    provider2 = ScriptedProvider([ChatChunk(delta="B", done=True)])
    service2 = _service(conn, docs_dir, provider2)
    service2.ask(conversation_id, "次の質問")

    sent_messages = provider2.calls[0]["messages"]
    assert sent_messages[0]["content"] == "最初の質問"
    assert sent_messages[-1]["content"] == "次の質問"
