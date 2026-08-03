import json

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.presentation.console.output import OutputMode
from abist_kb.presentation.console.presenter import Presenter
from abist_kb.presentation.console.theme import SemanticToken


def make(mode: OutputMode, **kwargs) -> Presenter:
    return Presenter(mode, width=80, **kwargs)


def test_success_line_carries_symbol_not_only_colour():
    p = make(OutputMode.PLAIN)
    p.success("同期が完了しました")
    out = p.stdout_value()
    assert "✓" in out
    assert "同期が完了しました" in out


def test_plain_mode_emits_no_ansi():
    p = make(OutputMode.PLAIN)
    p.success("完了")
    p.warning("注意")
    p.danger("失敗")
    p.table("文書", ["パス", "状態"], [["docs/a.md", "synced"]])
    assert "\x1b[" not in p.stdout_value()


def test_table_renders_all_cells_in_plain_mode():
    p = make(OutputMode.PLAIN)
    p.table("文書一覧", ["パス", "状態"], [["docs/蛇腹/a.md", "synced"], ["docs/b.md", "conflict"]])
    out = p.stdout_value()
    for fragment in ("文書一覧", "パス", "状態", "docs/蛇腹/a.md", "synced", "conflict"):
        assert fragment in out


def test_json_mode_writes_only_payload_to_stdout():
    p = make(OutputMode.JSON)
    p.success("これは stdout に出てはいけない")
    p.info("これも")
    p.json_result({"ok": True, "count": 2})
    assert json.loads(p.stdout_value()) == {"ok": True, "count": 2}


def test_json_mode_routes_human_messages_to_stderr():
    p = make(OutputMode.JSON)
    p.warning("索引が古い可能性があります")
    assert "索引が古い可能性があります" in p.stderr_value()


def test_json_result_is_utf8_not_escaped():
    p = make(OutputMode.JSON)
    p.json_result({"title": "蛇腹形状"})
    assert "蛇腹形状" in p.stdout_value()


def test_error_presentation_order_is_code_summary_cause_recovery():
    p = make(OutputMode.PLAIN)
    err = AppError(
        code=ErrorCode.FTS5_TRIGRAM_UNAVAILABLE,
        message="trigram トークナイザが利用できません",
        hint="SQLite 3.34.0 以上の Python で再実行してください",
        details={"sqlite_version": "3.30.0"},
        exit_code=ExitCode.CONFIG_ERROR,
    )
    p.error(err)
    out = p.stderr_value()
    i_code = out.index("FTS5_TRIGRAM_UNAVAILABLE")
    i_msg = out.index("trigram トークナイザが利用できません")
    i_cause = out.index("sqlite_version")
    i_hint = out.index("SQLite 3.34.0 以上")
    assert i_code < i_msg < i_cause < i_hint
    assert "--debug" in out


def test_error_goes_to_stderr_never_stdout():
    p = make(OutputMode.PLAIN)
    p.error(AppError(code=ErrorCode.FAILURE, message="失敗"))
    assert "失敗" not in p.stdout_value()
    assert "失敗" in p.stderr_value()


def test_json_mode_error_is_machine_readable_on_stderr():
    p = make(OutputMode.JSON)
    p.error(AppError(code=ErrorCode.NOT_FOUND, message="文書が見つかりません"))
    payload = json.loads(p.stderr_value())
    assert payload["code"] == "NOT_FOUND"
    assert payload["message"] == "文書が見つかりません"
    assert p.stdout_value() == ""


def test_quiet_suppresses_info_and_success_but_not_errors():
    p = make(OutputMode.PLAIN, quiet=True)
    p.success("完了")
    p.info("補足")
    p.danger("失敗")
    out = p.stdout_value()
    assert "完了" not in out
    assert "補足" not in out
    assert "失敗" in out


def test_confirm_returns_true_without_prompting_when_assume_yes():
    p = make(OutputMode.PLAIN)
    assert p.confirm("削除しますか", assume_yes=True) is True


def test_confirm_raises_when_non_interactive_and_not_assumed():
    p = make(OutputMode.JSON)
    err = None
    try:
        p.confirm("削除しますか", assume_yes=False)
    except AppError as exc:
        err = exc
    assert err is not None
    assert err.code == ErrorCode.INVALID_INPUT
    assert "--yes" in (err.hint or "")


def test_token_helper_prefixes_symbol():
    p = make(OutputMode.PLAIN)
    p.line("進行中", token=SemanticToken.INFO)
    assert "i" in p.stdout_value()
    assert "進行中" in p.stdout_value()
