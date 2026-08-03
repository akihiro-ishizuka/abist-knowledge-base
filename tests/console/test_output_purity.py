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


def test_no_ansi_in_rich_mode_when_color_system_is_none():
    """テスト網羅の抜け: 以前は PLAIN/JSON しか純度契約(ANSI皆無)を検証しておらず、
    RICH モードで `color_system=None`(§6.3 の非TTY・`NO_COLOR` 解決結果。
    `resolve_color_system()` が両方の場合に返す値そのもの)になったケースは
    一切カバーされていなかった。§6.3 は「非TTY・NO_COLOR・TERM=dumb では ANSI
    制御文字とアニメーションを一切出さない」ことを契約にしており、§15 も
    受け入れ基準として挙げているため、RICH というモード名だけで判断せず、
    実際に解決された `color_system` の値で純度を検証する。
    """
    p = Presenter(OutputMode.RICH, width=80, color_system=None)
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


def test_rich_mode_progress_scope_shows_live_bar_current_item_and_survives_exception():
    """レビュー指摘4・5:
    - RICH の progress_scope は実際にライブ表示(ANSI)を出す。
    - 終了時の静的な最終行が完了数・失敗数を正しく反映する。
    - `with` 内で例外が起きても進捗表示は静かに停止し、例外はそのまま伝播する
      (`finally` で `progress.stop()` する設計の担保)。
    - `advance`/`fail` に渡した現在項目(設計書 §6.2 の「現在項目」)が
      RICH のライブ表示に現れる。
    """
    p = Presenter(OutputMode.RICH, width=80, color_system="truecolor", force_terminal=True)
    with (
        pytest.raises(RuntimeError, match="boom"),
        progress_scope(p, description="索引作成", total=3) as handle,
    ):
        handle.advance(item="docs/a.md")
        handle.advance(item="docs/b.md")
        handle.fail(item="docs/c.md")
        raise RuntimeError("boom")
    out = p.stdout_value()
    assert ANSI.search(out)
    assert "完了 2件" in out
    assert "失敗 1件" in out
    assert "docs/c.md" in out


def test_progress_handle_set_total_updates_total_and_is_reflected_in_rich_display():
    """`ProgressHandle.set_total` は M3 が必要とする「列挙が終わってから総数が
    判明する」ケース(例: ディレクトリを再帰走査しながら総ファイル数が後から
    確定する)向けの唯一の API だが、これまで一切演習されていなかった。
    基底クラス(JSON/quiet 用)の単純な値更新と、RICH のライブ表示への反映
    (`_RichProgressHandle._on_set_total` のフック配線)の両方を確認する。
    """
    from abist_kb.presentation.console.progress import ProgressHandle

    handle = ProgressHandle(total=None)
    assert handle.total is None
    handle.set_total(42)
    assert handle.total == 42

    p = Presenter(OutputMode.RICH, width=80, color_system="truecolor", force_terminal=True)
    with progress_scope(p, description="列挙中", total=None) as rich_handle:
        # `set_total` だけを呼ぶ(直後に `advance` を呼ぶと、そちらの
        # `_on_advance` が現在の `self._total` を読んでライブ表示を更新して
        # しまい、`set_total` 自身のフック配線が実際に動いたかを区別できなく
        # なる)。
        rich_handle.set_total(5)
    out = p.stdout_value()
    assert "/5" in out
