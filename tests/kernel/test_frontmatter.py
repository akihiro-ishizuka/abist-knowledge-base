"""frontmatter kernel fixture (`tests/fixtures/kernel/frontmatter.json`) の全36ケース検証。

fixture は旧 Node 実装 `tools/lib/frontmatter.js` を実行して得たゴールデン値であり、
本テストが不合格になった場合は Python 実装側の不具合を疑う(fixture を直さない)。

fixture のキーは Node 由来の camelCase(`hasFrontmatter` 等)、Python 側 API は
snake_case のため、ここで明示的にマッピングする。
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from abist_kb.domain.frontmatter import (
    body_of,
    hash_body,
    parse_frontmatter,
    serialize_scalar,
    set_frontmatter_values,
)
from conftest import b64d, load_kernel_fixture

_FIXTURE = load_kernel_fixture("frontmatter")
_CASES: list[dict[str, Any]] = _FIXTURE["cases"]
assert len(_CASES) == 36, f"想定36ケースに対し {len(_CASES)} 件しか読み込めていない"

_PARSE_CASES = [c for c in _CASES if c["id"].startswith("parse_")]
_SETVALUES_CASES = [c for c in _CASES if c["id"].startswith("setvalues_")]
_HASHBODY_CASES = [c for c in _CASES if c["id"].startswith("hashbody_")]
_SERIALIZE_CASES = [c for c in _CASES if c["id"].startswith("serialize_scalar_")]

assert len(_PARSE_CASES) + len(_SETVALUES_CASES) + len(_HASHBODY_CASES) + len(
    _SERIALIZE_CASES
) == len(_CASES), "全ケースがどれかのカテゴリに分類されていない(id の接頭辞を確認)"


@pytest.mark.parametrize("case", _PARSE_CASES, ids=[c["id"] for c in _PARSE_CASES])
def test_parse_frontmatter_matches_fixture(case: dict[str, Any]) -> None:
    text = b64d(case["input_b64"])
    expected = case["expected"]

    result = parse_frontmatter(text)

    assert result.has_frontmatter == expected["hasFrontmatter"]
    assert (result.bom != "") == expected["bomPresent"]
    assert result.bom == b64d(expected["bom_b64"])
    assert result.eol == expected["eol"]
    assert result.data == expected["data"]
    assert list(result.keys) == expected["keys"]
    # `sorted()` を使う理由: 採取スクリプト(capture-kernel.mjs)自体が
    # `[...fm.blockKeys].sort()` で書き出しており、fixture の `blockKeys` は
    # そもそも出現順を保持していない(決定的な diff のためにソート済み)。
    # そのため fixture との比較ではソート済み同士を比べるしかない。
    # Python 側の出現順保持そのものは fixture に依存しない
    # `test_block_keys_preserve_first_occurrence_order`(下記)で別途検証する。
    assert sorted(result.block_keys) == expected["blockKeys"]
    assert result.body == b64d(expected["body_b64"])
    assert result.raw == b64d(expected["raw_b64"])
    assert hash_body(text) == expected["hashBody"]


# tests/fixtures/capture/_shared.mjs の sortedReplacer が JSON 書き出し時に全オブジェクトの
# キーをアルファベット順に並べ替えるため、`input_updates`(JSON object)のキー順は
# 採取時にコード上で実際に呼び出した順(`source, managed_by, document_type, status`)を
# 保持していない(配列である `added`/`updated` はソートされないため順序を保持している)。
# setvalues_backfill_* ケースはこの4キー更新の「追加される順」を検証するため、
# 採取スクリプト(capture-kernel.mjs:172-177)のオブジェクトリテラル順を明示的に復元する。
_BACKFILL_KEY_ORDER = ("source", "managed_by", "document_type", "status")


def _ordered_updates(case: dict[str, Any]) -> dict[str, Any]:
    raw_updates: dict[str, Any] = case["input_updates"]
    if case["id"].startswith("setvalues_backfill_"):
        assert set(raw_updates) == set(_BACKFILL_KEY_ORDER)
        return {key: raw_updates[key] for key in _BACKFILL_KEY_ORDER}
    return raw_updates


@pytest.mark.parametrize("case", _SETVALUES_CASES, ids=[c["id"] for c in _SETVALUES_CASES])
def test_set_frontmatter_values_matches_fixture(case: dict[str, Any]) -> None:
    text = b64d(case["input_b64"])
    updates = _ordered_updates(case)
    expected = case["expected"]

    result = set_frontmatter_values(text, updates)

    if "changed" in expected:
        assert result.changed == expected["changed"]
    if "created" in expected:
        assert result.created == expected["created"]
    if "added" in expected:
        assert list(result.added) == expected["added"]
    if "updated" in expected:
        assert list(result.updated) == expected["updated"]
    if "skipped" in expected:
        assert [{"key": s.key, "reason": s.reason} for s in result.skipped] == expected["skipped"]
    if "text_b64" in expected:
        assert result.text == b64d(expected["text_b64"])
    if "parsedBodyAfter_b64" in expected:
        assert parse_frontmatter(result.text).body == b64d(expected["parsedBodyAfter_b64"])
    if "bodyAfter_b64" in expected:
        assert parse_frontmatter(result.text).body == b64d(expected["bodyAfter_b64"])
    if "startsWithBomDelimiter" in expected:
        assert result.text.startswith("﻿---\n") == expected["startsWithBomDelimiter"]
    if "hashBodyMatchesOriginal" in expected:
        assert (hash_body(result.text) == hash_body(text)) == expected["hashBodyMatchesOriginal"]
    if "unchanged" in expected:
        assert (result.text == text) == expected["unchanged"]


@pytest.mark.parametrize("case", _HASHBODY_CASES, ids=[c["id"] for c in _HASHBODY_CASES])
def test_hash_body_matches_fixture(case: dict[str, Any]) -> None:
    text = b64d(case["input_b64"])
    expected = case["expected"]

    if case["id"] == "hashbody_unaffected_by_frontmatter_change":
        with_meta = set_frontmatter_values(text, {"status": "active"}).text
        assert hash_body(with_meta) == expected["hashWithMeta"]
        assert hash_body(text) == expected["hashOriginal"]
        assert (hash_body(with_meta) == hash_body(text)) == expected["equal"]
    elif case["id"] == "hashbody_detects_body_change":
        appended = text + "追記\r\n"
        assert hash_body(text) == expected["hashOriginal"]
        assert hash_body(appended) == expected["hashAppended"]
        assert (hash_body(text) == hash_body(appended)) == expected["equal"]
    elif case["id"] == "hashbody_detects_eol_change":
        crlf_doc = "---\ntitle: a\n---\r\n本文\r\n"
        assert hash_body(text) == expected["hashLf"]
        assert hash_body(crlf_doc) == expected["hashCrlf"]
        assert (hash_body(text) == hash_body(crlf_doc)) == expected["equal"]
    else:
        pytest.fail(f"未対応の hashbody ケース: {case['id']}")


@pytest.mark.parametrize("case", _SERIALIZE_CASES, ids=[c["id"] for c in _SERIALIZE_CASES])
def test_serialize_scalar_matches_fixture(case: dict[str, Any]) -> None:
    expected = case["expected"]
    assert serialize_scalar(case["input"]) == expected["value"]


# ---------------------------------------------------------------------------
# Hypothesis 性質テスト(brief Step 6): 任意の(BOM有無 × LF/CRLF混在 × 日本語 ×
# front matter有無)テキストに対して成り立つべき3つの不変条件。
# ---------------------------------------------------------------------------

# レビュー指摘(Minor): 以前は本文アルファベットから `\r`/`\n` を除外していたため、
# 本文が常に単一行になり、モジュール docstring が明記する esa の実態
# (front matter は LF・本文は CRLF の混在)を Hypothesis が一度も生成できな
# かった。`\r`/`\n` を許可することで、本文自体が複数行・CRLF/LF混在になる
# ケースも生成対象に含める(BOM と `-` 開始は依然として除外——BOMは `bom`
# パラメータで別途制御し、`-` 開始は偶発的な `---` 区切り誤認を避けるため)。
_body_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="﻿-"),
    max_size=30,
)


def _make_doc(bom: bool, front_matter: bool, crlf: bool, body: str) -> str:
    eol = "\r\n" if crlf else "\n"
    prefix = "﻿" if bom else ""
    if front_matter:
        return f'{prefix}---{eol}title: "見出し"{eol}---{eol}{body}'
    return f"{prefix}{body}"


_doc_strategy = st.builds(
    _make_doc,
    bom=st.booleans(),
    front_matter=st.booleans(),
    crlf=st.booleans(),
    body=_body_text,
)


@given(text=_doc_strategy)
def test_property_raw_roundtrip_is_byte_identical(text: str) -> None:
    assert parse_frontmatter(text).raw == text


_extra_value_text = st.text(
    min_size=1,
    max_size=10,
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters='\r\n﻿"'),
)


@given(text=_doc_strategy, extra=_extra_value_text)
def test_property_editing_frontmatter_leaves_hash_body_unchanged(text: str, extra: str) -> None:
    before = hash_body(text)
    updated_text = set_frontmatter_values(text, {"injected_prop_key": extra}).text
    assert hash_body(updated_text) == before


# ---------------------------------------------------------------------------
# レビュー指摘の回帰テスト(fixture では拾えない・手書きケース)。
# ---------------------------------------------------------------------------


def test_block_keys_preserve_first_occurrence_order() -> None:
    """`block_keys` は JS の `Set`(挿入順を保持し、初出位置を維持する)と同じ順序で
    出現する。fixture の `blockKeys` はソート済みで記録されているため
    (`capture-kernel.mjs` が `.sort()` して書き出す)、この順序保持そのものは
    fixture では検証できない。ここで合成ケースで直接固定する。
    """
    result = parse_frontmatter("---\nz: |\n  block\na: |\n  block\nm: |\n  block\n---\n本文\n")
    assert list(result.block_keys) == ["z", "a", "m"]


def test_i2_fullwidth_digits_do_not_parse_as_numbers() -> None:
    """I2: Python の `\\d` は全角数字にもマッチするが、JS の `\\d` は ASCII のみ。

    全角数字が数値に変換されてしまうと `classify_document` の
    `source`/`managed_by` 判定が JS 側と食い違いうる。
    """
    result = parse_frontmatter("---\npost_number: ２０２４\n---\n")
    assert result.data["post_number"] == "２０２４"
    assert not isinstance(result.data["post_number"], int)


def test_i3_serialize_scalar_quotes_trailing_newline_value() -> None:
    """I3: `$` は Python では末尾改行の直前にもマッチするが JS では末尾のみ。

    `serialize_scalar('abc\\n')` がクォート無しで返ると、
    `set_frontmatter_values` 経由で front matter 行に生の改行が混入し、
    ブロックの構造が壊れる。
    """
    assert serialize_scalar("abc\n") == '"abc\n"'


def test_dup_key_after_block_value_dedup_timing() -> None:
    """レビュー指摘: ブロック値として宣言した直後に同じキーを単一行スカラーで
    再宣言すると、JS はループ完了後にまとめてブロックキーを `data` から除くため
    最終的に `{}` になるが、ループ中に都度 pop する実装だと再宣言が生き残って
    `{'a': 5}` になってしまう。
    """
    result = parse_frontmatter("---\na: |\na: 5\n---\n本文\n")
    assert result.data == {}
    assert list(result.block_keys) == ["a"]


def test_set_frontmatter_values_filters_none_like_js_undefined() -> None:
    """レビュー指摘: `None` を含む更新はそのキーに触れない(JS の
    `filter(([, v]) => v !== undefined)` に相当)。フィルタしないと
    `a: null` という行を書き込んでしまう。
    """
    text = "---\ntitle: a\n---\n本文\n"
    result = set_frontmatter_values(text, {"title": "a", "extra": None})
    assert result.text == text
    assert result.changed is False
    assert "extra" not in result.added
    assert "null" not in result.text


@given(body=_body_text)
def test_property_changing_body_eol_changes_hash_body(body: str) -> None:
    lf_doc = f"---\ntitle: a\n---\n{body}\n"
    crlf_doc = f"---\ntitle: a\n---\r\n{body}\r\n"
    # body アルファベットから \r\n を除外しているため body_of は常に "<body>\n" vs
    # "<body>\r\n" になり、両者が一致することはない(不変条件そのものの健全性チェック)。
    assert body_of(lf_doc) != body_of(crlf_doc)
    assert hash_body(lf_doc) != hash_body(crlf_doc)
