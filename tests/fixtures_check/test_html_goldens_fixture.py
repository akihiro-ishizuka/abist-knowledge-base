"""M1 Task 5 (Part 2) で採取した HTML→Markdown ゴールデン
(`tests/fixtures/html/turndown-goldens.json`) の健全性検証。

このフィクスチャは kernel/ 配下のようなビット互換の契約ではなく、M3 の
Python 実装(markdownify 等)との差分を事前に把握するための資料である
(`comparison_policy` フィールドで明示)。ここでは fixture 自体の構造的健全性と、
採取スクリプトが実際に旧システムの `download-web.js` の設定・前処理ロジックを
再現していることを裏付ける代表的な出力内容を固定する。

再採取の決定性(2回連続実行してバイト同一)は `capture-html.mjs` を手動で
2回実行して確認済み(タスクレポート参照)。Node 実行を伴う検証はこの
テストスイートには含めない(他の M1 fixture テストと同じ方針: 旧 Node
システムへの依存をテスト実行時に持ち込まない)。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from abist_kb.domain.redaction import mask_secrets

FIXTURES_HTML_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "html"
GOLDENS_PATH = FIXTURES_HTML_DIR / "turndown-goldens.json"

EXPECTED_TURNDOWN_OPTIONS = {
    "headingStyle": "atx",
    "codeBlockStyle": "fenced",
    "bulletListMarker": "-",
    "emDelimiter": "*",
    "strongDelimiter": "**",
}


def _load() -> dict[str, Any]:
    data = json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))
    assert data, f"HTML goldens フィクスチャが空: {GOLDENS_PATH}"
    return data


def _iter_b64_fields(obj: Any) -> Iterator[tuple[str, str]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.endswith("_b64") and isinstance(value, str):
                yield key, value
            else:
                yield from _iter_b64_fields(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_b64_fields(item)


def _case_markdown(data: dict[str, Any], case_id: str) -> str:
    case = next(c for c in data["cases"] if c["id"] == case_id)
    return base64.b64decode(case["output_markdown_b64"]).decode("utf-8")


def test_schema_and_comparison_policy() -> None:
    data = _load()
    assert data["schema"] == 1
    assert isinstance(data["source"], str) and "download-web.js" in data["source"]
    assert isinstance(data["comparison_policy"], str)
    assert "advisory" in data["comparison_policy"], (
        "comparison_policy は kernel/ のビット互換契約と違い、"
        "advisory(参考情報)である旨を明示すべき"
    )
    assert data["turndown_addRule_count"] == 0, (
        "download-web.js に turndownService.addRule 呼び出しは無い(grepで確認済み)"
    )


def test_turndown_options_match_download_web_js_exactly() -> None:
    """転記元: download-web.js:37-43。旧システムの設定と1キーも違わないこと。"""
    data = _load()
    assert data["turndown_options"] == EXPECTED_TURNDOWN_OPTIONS


def test_case_count_within_expected_range() -> None:
    data = _load()
    assert 12 <= len(data["cases"]) <= 15, f"ケース数が想定範囲(12〜15)外: {len(data['cases'])}"


def test_case_ids_are_unique() -> None:
    data = _load()
    ids = [case["id"] for case in data["cases"]]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"重複した case id がある: {duplicates}"


def test_all_b64_fields_decode_as_valid_base64() -> None:
    data = _load()
    for key, value in _iter_b64_fields(data["cases"]):
        try:
            base64.b64decode(value, validate=True)
        except binascii.Error as exc:  # pragma: no cover
            raise AssertionError(f"フィールド {key!r} が base64 として不正: {exc}") from exc


def test_every_case_has_non_empty_input_and_output() -> None:
    data = _load()
    for case in data["cases"]:
        input_html = base64.b64decode(case["input_html_b64"]).decode("utf-8")
        output_md = base64.b64decode(case["output_markdown_b64"]).decode("utf-8")
        assert input_html.strip(), f"{case['id']}: 入力HTMLが空"
        assert output_md.strip(), f"{case['id']}: 出力Markdownが空"


def test_expected_case_ids_are_present() -> None:
    """brief が要求する内容カテゴリ(見出し・入れ子リスト・テーブル・コードブロック・
    リンク・画像・インライン要素混在・日本語全角記号・実サイト風合成ページ)が
    それぞれ最低1ケース以上あること。"""
    data = _load()
    ids = {case["id"] for case in data["cases"]}
    required_substrings = [
        "heading",
        "list",
        "table",
        "code_block",
        "link",
        "image",
        "emphasis",
        "japanese",
        "messy_realworld_page",
    ]
    for substring in required_substrings:
        assert any(substring in case_id for case_id in ids), (
            f"'{substring}' を含むケースIDが無い(必須カテゴリの取りこぼし)"
        )


def test_relative_links_rewritten_absolute_but_mailto_and_anchor_are_not() -> None:
    """転記元: download-web.js:126-138。相対hrefは絶対URL化されるが、
    http始まり・mailto:始まり・#始まりは書き換え対象外である境界を固定する。"""
    data = _load()
    markdown = _case_markdown(data, "links_relative_and_absolute")
    assert "https://example.com/docs/reference/other.html" in markdown, (
        "相対リンクが絶対URL化されていない"
    )
    assert "https://example.com/abs" in markdown, "絶対リンクが保持されていない"
    assert "mailto:user@example.com" in markdown, "mailtoリンクが書き換えられてしまっている"
    assert "#section2" in markdown, "アンカーリンクが書き換えられてしまっている"


def test_relative_image_rewritten_but_data_uri_is_not() -> None:
    """転記元: download-web.js:141-150。"""
    data = _load()
    markdown = _case_markdown(data, "images_relative_and_absolute")
    assert "https://example.com/docs/guide/images/diagram.png" in markdown, (
        "相対画像srcが絶対URL化されていない"
    )
    assert "https://example.com/abs.png" in markdown
    assert "data:image/png;base64,AAAA" in markdown, "data:URIが書き換えられてしまっている"


def test_removed_elements_strip_entire_subtree_including_nested_content() -> None:
    """転記元: download-web.js:156。nav/header/footer/aside/.sidebar/.navigation は
    要素ごと削除される(中身のテキストも一緒に消える)ことを固定する。M3実装が
    もし「タグだけ剥がして中身を残す」実装をすると、この出力と食い違って気づける。"""
    data = _load()
    markdown = _case_markdown(data, "removed_elements")
    for removed_text in [
        "ナビゲーション",
        "ヘッダー内テキスト",
        "サイドバー",
        "旧サイドバー",
        "ナビ2",
        "フッター",
    ]:
        assert removed_text not in markdown, (
            f"削除されるべき要素の内容が残っている: {removed_text!r}"
        )
    assert "本文タイトル" in markdown
    assert "残る本文" in markdown


def test_code_block_language_class_is_extracted_into_fence_info_string() -> None:
    """turndown組み込みのfencedコードブロックルールが language-(\\S+) を抽出する
    (プラグイン不要の既定動作)ことを固定する。"""
    data = _load()
    markdown = _case_markdown(data, "code_block_language_js")
    assert "```javascript" in markdown, "言語クラスがフェンス情報文字列に反映されていない"
    assert "function add(a, b)" in markdown


def test_vanilla_turndown_does_not_produce_gfm_pipe_tables() -> None:
    """旧システムは turndown-plugin-gfm を導入していない(node_modules/package.jsonに
    存在しないことを確認済み)。vanilla turndown はtable要素専用のルールを持たず、
    セル内容がパイプ区切りテーブルではなくただのテキストの並びになることを固定する
    (M3がmarkdownify等でGFMテーブルを生成すると、ここが最大の既知の差分になる)。"""
    data = _load()
    markdown = _case_markdown(data, "table_basic")
    assert "|" not in markdown, (
        "vanilla turndownの出力にパイプ文字が含まれている(想定外 — turndown-plugin-gfm相当の"
        "変換が行われている可能性)"
    )
    for cell_text in ["列A", "列B", "a1", "b1", "a2", "b2"]:
        assert cell_text in markdown


def test_japanese_fullwidth_punctuation_survives_conversion() -> None:
    data = _load()
    markdown = _case_markdown(data, "japanese_fullwidth_punctuation")
    for glyph in ["「", "」", "『", "』", "・", "ー", "〜"]:
        assert glyph in markdown, f"全角記号 {glyph!r} が変換過程で失われている"


def test_no_secrets_leak_into_html_goldens() -> None:
    data = _load()
    checked = 0
    for key, value in _iter_b64_fields(data["cases"]):
        try:
            decoded = base64.b64decode(value).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue
        checked += 1
        masked = mask_secrets(decoded)
        assert masked == decoded, f"フィールド {key!r} に mask_secrets が反応する内容が残っている"
    assert checked > 0, "base64 コンテンツを1件も検証できなかった(テスト自体が空振り)"
