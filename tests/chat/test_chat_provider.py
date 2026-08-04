"""`infrastructure.ai.chat_provider`: 引用フェンスの分離(ネットワーク非依存の純粋関数)。"""

from __future__ import annotations

from abist_kb.infrastructure.ai.chat_provider import RawCitation, split_citations_block


def test_split_citations_block_extracts_and_strips() -> None:
    text = '本文です。\n```citations\n[{"path": "a.md", "start_line": 1, "end_line": 2}]\n```'
    display, citations = split_citations_block(text)
    assert display == "本文です。"
    assert citations == [RawCitation(path="a.md", start_line=1, end_line=2)]


def test_split_citations_block_no_block_returns_text_unchanged() -> None:
    display, citations = split_citations_block("本文だけです。")
    assert display == "本文だけです。"
    assert citations == []


def test_split_citations_block_ignores_malformed_json() -> None:
    text = "本文です。\n```citations\nnot json\n```"
    display, citations = split_citations_block(text)
    assert citations == []


def test_split_citations_block_ignores_items_with_wrong_types() -> None:
    text = '本文です。\n```citations\n[{"path": 1, "start_line": "x", "end_line": 2}]\n```'
    _display, citations = split_citations_block(text)
    assert citations == []
