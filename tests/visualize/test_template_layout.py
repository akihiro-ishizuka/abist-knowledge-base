"""`tools/visualize/templates/layout.py` の純関数テスト(manim 不要)。

テンプレートのレイアウト計算はこれまで manim を import しないと触れず、
主 venv から検証できなかった。矢印の誤描画(後戻りの貫通・折返しの斜め横断)と
ラベル重複による誤接続がテストなしで放置されていた原因がこれ。
"""

from __future__ import annotations

import pytest
from templates.layout import (
    EDGE_ADJACENT,
    EDGE_BACK,
    EDGE_CROSS_BAND_BACK,
    EDGE_SKIP_FORWARD,
    EDGE_WRAP_DOWN,
    classify_edge,
    display_width,
    find_duplicate_labels,
    wrap_cjk,
)


class TestDisplayWidth:
    def test_ascii_counts_as_one(self) -> None:
        assert display_width("abc") == 3

    def test_cjk_counts_as_two(self) -> None:
        assert display_width("蛇腹") == 4

    def test_mixed(self) -> None:
        assert display_width("蛇腹a") == 5

    def test_empty(self) -> None:
        assert display_width("") == 0


class TestWrapCjk:
    def test_every_line_fits_within_max_cols(self) -> None:
        text = "次期版では抽出・提案・編集・出力を単一アプリで完結させる方針とする"
        for line in wrap_cjk(text, 20):
            assert display_width(line) <= 20

    def test_short_text_is_single_line(self) -> None:
        assert wrap_cjk("短い", 20) == ["短い"]

    def test_existing_newlines_are_kept_as_paragraphs(self) -> None:
        assert wrap_cjk("あ\nい", 20) == ["あ", "い"]

    def test_ascii_word_is_not_split_midway(self) -> None:
        lines = wrap_cjk("use pattern_editor now", 12)
        assert all(
            "pattern_editor" not in line or line.count("pattern_editor") == 1 for line in lines
        )
        joined = "".join(line.strip() for line in lines)
        assert "pattern_editor" in joined

    def test_line_does_not_start_with_forbidden_char(self) -> None:
        text = "統合する。次に検証する。さらに出力する。最後に確認する。"
        for line in wrap_cjk(text, 10):
            if line:
                assert line[0] not in "。、）」", f"行頭禁則違反: {line!r}"

    def test_single_char(self) -> None:
        assert wrap_cjk("あ", 20) == ["あ"]

    def test_empty_string(self) -> None:
        assert wrap_cjk("", 20) == [""]

    def test_very_long_ascii_word_still_terminates(self) -> None:
        # 1語がどう頑張っても収まらない場合も無限ループしないこと
        lines = wrap_cjk("a" * 50, 10)
        assert "".join(lines) == "a" * 50

    @pytest.mark.parametrize("cols", [2, 3, 5, 10, 40])
    def test_never_exceeds_budget_for_various_widths(self, cols: int) -> None:
        # 禁則は追い出しで処理するため、幅の上限は例外なく守られる。
        # この不変条件は箱幅の逆算(_step_box)が依存しているので厳密に固定する。
        text = "蛇腹パターン自動化 pattern_editor を使う。"
        for line in wrap_cjk(text, cols):
            assert display_width(line) <= cols


class TestFindDuplicateLabels:
    def test_no_duplicates(self) -> None:
        beats = [{"type": "flow_step", "label": "A"}, {"type": "flow_step", "label": "B"}]
        assert find_duplicate_labels(beats) == []

    def test_reports_second_occurrence_index(self) -> None:
        beats = [
            {"type": "flow_step", "label": "受信"},
            {"type": "flow_step", "label": "受信"},
        ]
        assert find_duplicate_labels(beats) == [(1, "受信")]

    def test_decision_shares_the_label_namespace_with_flow_step(self) -> None:
        # transition は label で接続先を引くので、種別が違っても同名は許されない
        beats = [
            {"type": "flow_step", "label": "審査"},
            {"type": "decision", "label": "審査"},
        ]
        assert find_duplicate_labels(beats) == [(1, "審査")]

    def test_non_node_beats_are_ignored(self) -> None:
        beats = [
            {"type": "statement", "text": "A"},
            {"type": "transition", "from": "A", "to": "B"},
        ]
        assert find_duplicate_labels(beats) == []

    def test_malformed_beats_do_not_raise(self) -> None:
        assert find_duplicate_labels([None, "x", {"type": "flow_step"}]) == []  # type: ignore[list-item]


class TestClassifyEdge:
    @pytest.mark.parametrize(
        ("src", "dst", "expected"),
        [
            ((0, 0), (0, 1), EDGE_ADJACENT),
            ((0, 0), (0, 3), EDGE_SKIP_FORWARD),
            ((0, 3), (0, 1), EDGE_BACK),
            ((0, 3), (1, 0), EDGE_WRAP_DOWN),
            ((1, 0), (0, 0), EDGE_CROSS_BAND_BACK),
            ((0, 0), (2, 0), EDGE_CROSS_BAND_BACK),
        ],
    )
    def test_classification(
        self, src: tuple[int, int], dst: tuple[int, int], expected: str
    ) -> None:
        assert classify_edge(src, dst) == expected

    def test_self_loop_is_back(self) -> None:
        assert classify_edge((0, 2), (0, 2)) == EDGE_BACK
