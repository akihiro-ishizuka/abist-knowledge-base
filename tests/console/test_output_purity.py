import json
import re

import pytest

from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.presenter import Presenter
from abist_kb.presentation.console.progress import progress_scope

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def exercise(p: Presenter) -> None:
    """Presenter の人間向けAPIを一通り叩く。"""
    p.line("行")
    p.success("成功")
    p.warning("警告")
    p.danger("失敗")
    p.info("情報")
    p.muted("補助")
    p.table("表", ["列A", "列B"], [["値1", "値2"]])
    p.panel("見出し", "本文")
    p.markdown("# 見出し\n\n本文\n")
    with progress_scope(p, description="処理中", total=3) as handle:
        handle.advance(item="docs/a.md")
        handle.advance(item="docs/b.md")
        handle.advance(item="docs/c.md")
    p.error(AppError(code=ErrorCode.FAILURE, message="エラー"))


@pytest.mark.parametrize("mode", [OutputMode.PLAIN, OutputMode.JSON])
def test_no_ansi_anywhere_in_non_rich_modes(mode):
    p = Presenter(mode, width=80)
    exercise(p)
    assert not ANSI.search(p.stdout_value())
    assert not ANSI.search(p.stderr_value())


def test_json_mode_stdout_stays_empty_until_a_result_is_emitted():
    p = Presenter(OutputMode.JSON, width=80)
    exercise(p)
    assert p.stdout_value() == ""


def test_json_mode_stdout_is_exactly_one_json_document():
    p = Presenter(OutputMode.JSON, width=80)
    exercise(p)
    p.json_result({"ok": True})
    assert json.loads(p.stdout_value()) == {"ok": True}


def test_plain_mode_progress_emits_no_animation_frames():
    p = Presenter(OutputMode.PLAIN, width=80)
    with progress_scope(p, description="索引作成", total=2) as handle:
        handle.advance()
        handle.advance()
    out = p.stdout_value()
    assert "\r" not in out
    assert not ANSI.search(out)
    assert "索引作成" in out


def test_quiet_plain_mode_emits_nothing_for_progress():
    p = Presenter(OutputMode.PLAIN, width=80, quiet=True)
    with progress_scope(p, description="索引作成", total=2) as handle:
        handle.advance()
    assert p.stdout_value() == ""


def test_rich_mode_does_produce_ansi_when_colour_enabled():
    """対照テスト: RICH では色が出ること(純度テストが常に真にならない担保)。"""
    p = Presenter(OutputMode.RICH, width=80, color_system="truecolor", force_terminal=True)
    p.success("成功")
    assert ANSI.search(p.stdout_value())
