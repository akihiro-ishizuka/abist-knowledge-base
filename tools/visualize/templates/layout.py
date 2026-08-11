"""テンプレート共通のレイアウト計算(manim 非依存の純関数のみ)。

**このモジュールは manim を import してはならない。**

`tools/visualize/templates/*.py` は manim を import するため、主 venv(Python 3.12)
で走る pytest からは import できず、レイアウトのロジックが構造的にテスト不能だった。
グラフ計算・文字列処理をここへ集約することで、`tests/visualize/test_template_layout.py`
が主 venv から直接検証できる(conftest が `tools/visualize` を sys.path へ入れる)。

manim を使う描画そのものは呼び出し側(base.py / data_flow_v1.py など)の責務。
"""

from __future__ import annotations

import unicodedata

#: 行頭に置きたくない文字(禁則)。ぶら下げて前行の末尾に送る。
_NO_LINE_START = "。、，．・）」』】〉》!?！？：；ー々ぁぃぅぇぉっゃゅょゎ…"
#: 行末に置きたくない文字(禁則)。次行の先頭へ送る。
_NO_LINE_END = "（「『【〈《"

#: `display_width` が全角(2桁)として数える east_asian_width の区分。
_WIDE = frozenset({"W", "F", "A"})


def display_width(text: str) -> int:
    """半角を1・全角を2として数えた表示幅を返す。

    Manim の `Text` は等幅ではないので厳密な値にはならないが、折り返し桁数の
    見積もりには十分。`east_asian_width` の Ambiguous(A) は日本語環境で全角に
    倒れるため2桁として扱う。
    """
    return sum(2 if unicodedata.east_asian_width(c) in _WIDE else 1 for c in text)


def wrap_cjk(text: str, max_cols: int) -> list[str]:
    """半角換算 `max_cols` 桁で折り返した行のリストを返す。

    `textwrap` は空白でしか折らないため日本語では機能せず、Manim の
    `Text(width=...)` は折り返しではなく `scale_to_fit_width`(縮小)なので、
    CJK 対応の折り返しは自前で持つしかない。

    - 既存の改行は段落境界として保持する
    - ASCII の連続語は空白位置を優先して切る(英単語を途中で割らない)
    - 行頭・行末の禁則を1文字だけ調整する(追い出し。ぶら下げないので
      戻り値の各行は必ず `display_width(line) <= max_cols` を満たす)
    """
    if max_cols < 2:
        max_cols = 2
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        lines.extend(_wrap_paragraph(paragraph, max_cols))
    return lines


def _wrap_paragraph(paragraph: str, max_cols: int) -> list[str]:
    lines: list[str] = []
    current = ""
    width = 0
    for char in paragraph:
        char_width = display_width(char)
        if width + char_width <= max_cols:
            current += char
            width += char_width
            continue
        # 行が溢れた。まず ASCII 語の途中で切らないよう後退を試みる。
        head, tail = _break_ascii_word(current, char)
        if tail:
            lines.append(head)
            current = tail + char
            width = display_width(current)
            continue
        # 禁則(追い出し): 次行の先頭に来てほしくない文字は、直前の1文字ごと次行へ送る。
        # ぶら下げ(前行に足す)ではなく追い出しを選ぶのは、`display_width(line) <= max_cols`
        # の不変条件を保つため。この幅は箱幅の逆算にも使うので、超過を許すと壊れる。
        if char in _NO_LINE_START and len(current) > 1:
            lines.append(current[:-1])
            current = current[-1] + char
            width = display_width(current)
            continue
        # 禁則: 前行の末尾が来てほしくない文字なら、その1文字を次行へ送る。
        if current and current[-1] in _NO_LINE_END:
            lines.append(current[:-1])
            current = current[-1] + char
            width = display_width(current)
            continue
        lines.append(current)
        current = char
        width = char_width
    if current:
        lines.append(current)
    return lines or [""]


def _break_ascii_word(current: str, next_char: str) -> tuple[str, str]:
    """ASCII 語の途中で折り返そうとしている場合に、語の直前で切り直す。

    戻り値 `(前行, 次行へ送る断片)`。切り直す必要がなければ `("", "")`。
    """
    if not current or not _is_ascii_word_char(next_char):
        return "", ""
    if not _is_ascii_word_char(current[-1]):
        return "", ""
    index = len(current)
    while index > 0 and _is_ascii_word_char(current[index - 1]):
        index -= 1
    if index == 0:
        # 行全体が1つの長い ASCII 語。切り直すと無限後退になるのでそのまま折る。
        return "", ""
    return current[:index].rstrip(), current[index:]


def _is_ascii_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "_-.")


def find_duplicate_labels(beats: list[dict]) -> list[tuple[int, str]]:
    """ノードを名乗る beat のラベル重複を `(index, label)` で返す(2件目以降)。

    `data_flow_v1` は `boxes[label] = box` でノードを引くため、ラベルが重複すると
    後勝ちで上書きされ、前のノードには矢印が繋がらない。検証層とテンプレート層の
    双方から使う。
    """
    seen: dict[str, int] = {}
    duplicates: list[tuple[int, str]] = []
    for index, beat in enumerate(beats):
        if not isinstance(beat, dict):
            continue
        if beat.get("type") not in _NODE_BEAT_TYPES:
            continue
        label = beat.get("label")
        if not isinstance(label, str):
            continue
        if label in seen:
            duplicates.append((index, label))
        else:
            seen[label] = index
    return duplicates


#: ラベルでノードとして参照される beat type(transition / domain_relation の接続先)。
_NODE_BEAT_TYPES = frozenset({"flow_step", "decision"})

#: `classify_edge` が返す分類。
EDGE_ADJACENT = "adjacent"
EDGE_SKIP_FORWARD = "skip_forward"
EDGE_BACK = "back"
EDGE_WRAP_DOWN = "wrap_down"
EDGE_CROSS_BAND_BACK = "cross_band_back"


def classify_edge(src: tuple[int, int], dst: tuple[int, int]) -> str:
    """ノード位置 `(band, col)` の関係から矢印の描き方を決める。

    従来は「中心 y が一致すれば常に 右辺 -> 左辺 の直線」だったため、同じ行の
    後戻り(4番目 -> 2番目)が間のノードを貫通し、行をまたぐ矢印は右端から左端へ
    斜めに全幅を横切っていた。位置関係で描き分けることでこれを解消する。
    """
    src_band, src_col = src
    dst_band, dst_col = dst
    if src_band == dst_band:
        if dst_col == src_col + 1:
            return EDGE_ADJACENT
        if dst_col > src_col:
            return EDGE_SKIP_FORWARD
        return EDGE_BACK
    if dst_band == src_band + 1:
        return EDGE_WRAP_DOWN
    return EDGE_CROSS_BAND_BACK
