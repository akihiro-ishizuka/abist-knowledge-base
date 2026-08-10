import io
import json
import sys

import pytest

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.domain.redaction import mask_secrets
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


def test_success_does_not_crash_on_cp932_console_stream():
    """レビュー指摘1(CRITICAL): 日本語ロケール Windows の既定コードページ(cp932)は
    SUCCESS の記号 '✓'(U+2713)を表現できない。Presenter は StringIO 以外の実ストリーム
    (TextIOWrapper 等)を渡されても cp932 のまま UnicodeEncodeError を起こしてはならない。
    """
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp932", errors="strict")
    p = Presenter(OutputMode.PLAIN, stdout=stream, width=80)
    p.success("完了")
    stream.flush()
    written = buffer.getvalue().decode("utf-8")
    assert "完了" in written
    assert "✓" in written


def test_success_does_not_crash_when_reconfigure_is_refused():
    """レビュー再指摘A: 既に読み取り済みの `TextIOWrapper` は `reconfigure()` 自体を
    `io.UnsupportedOperation`(`OSError`/`ValueError` の両方のサブクラス)で拒否する
    ことがある。`contextlib.suppress` で握りつぶすだけでは、ストリームが cp932/strict
    のまま残り、修正前に直したはずの `UnicodeEncodeError` がそのまま再発する。
    フォールバックの書き込みプロキシで、この場合でもクラッシュしないことを検証する。
    """
    buffer = io.BytesIO("初期データ".encode("cp932"))
    stream = io.TextIOWrapper(buffer, encoding="cp932", errors="strict")
    stream.read(1)  # 一度でも読むと reconfigure() が拒否されるようになる
    assert stream.encoding == "cp932"  # 前提: reconfigure 前はまだ cp932

    p = Presenter(OutputMode.PLAIN, stdout=stream, width=80)
    p.success("完了")  # ここで UnicodeEncodeError を起こしてはならない
    stream.flush()

    written = buffer.getvalue().decode("cp932")
    assert "完了" in written
    # テスト網羅の抜け: 上の1行だけでは「✓ が丸ごと落ちても」テストは通ってしまう。
    # `_CrashSafeTextStream` の実際の存在意義は「エンコードできない文字を
    # 消さずに `backslashreplace` で書き込み可能な形へ落とす」ことなので、
    # 実際に `✓`(バックスラッシュ+u2713 という文字列そのもの。cp932 では
    # '✓' が表現できないため `str.encode(..., "backslashreplace")` がこの
    # ASCII 表現に変換する)が出力に含まれることまで確認する。
    assert "\\u2713" in written


def test_json_result_called_twice_raises_runtime_error():
    """レビュー指摘2: stdout は「payload のみ」の契約。二重出力は壊れた JSON を
    黙って生成せず、はっきり失敗させる。
    """
    p = make(OutputMode.JSON)
    p.json_result({"a": 1})
    with pytest.raises(RuntimeError):
        p.json_result({"b": 2})


def test_json_result_does_not_substitute_rich_emoji_codes():
    """C1(CRITICAL)の回帰テスト: `Console.print(str)` は既定で `:100:` のような
    Rich の絵文字コードを実際の絵文字へ置換してしまう(`markup=False`/`highlight=False`
    は絵文字置換を止めない)。`json_result` はバイト同一性が契約であり、ユーザーの
    文書タイトルやパスに `:xxx:` 形式の文字列がたまたま含まれていても、
    出力は入力と完全に同じでなければならない。
    """
    p = make(OutputMode.JSON)
    payload = {"title": "release :100: notes", "path": "docs/:cd:/a.md"}
    p.json_result(payload)
    assert json.loads(p.stdout_value()) == payload
    assert "💯" not in p.stdout_value()
    assert "💿" not in p.stdout_value()


def test_error_json_mode_does_not_substitute_rich_emoji_codes():
    p = make(OutputMode.JSON)
    p.error(AppError(code=ErrorCode.FAILURE, message="release :100: failed"))
    payload = json.loads(p.stderr_value())
    assert payload["message"] == "release :100: failed"
    assert "💯" not in p.stderr_value()


def test_table_does_not_substitute_rich_emoji_codes():
    p = make(OutputMode.PLAIN)
    p.table("release :100:", ["path"], [["docs/:cd:/a.md"]])
    out = p.stdout_value()
    assert "release :100:" in out
    assert "docs/:cd:/a.md" in out
    assert "💯" not in out
    assert "💿" not in out


def test_error_masks_secrets_in_details_and_traceback_under_debug():
    """I3(IMPORTANT)の回帰テスト: `Presenter.error()` は `details`(`wrap()` が
    保持する `cause_message` を含む)とトレースバックのどちらもマスクせずに
    そのまま出力していた(`mask_secrets` は `presentation/` から一切 import
    されていなかった)。`--debug` を付けると同じ秘密情報が details とトレース
    バックの2箇所に生のまま現れる。CI がこの stderr をキャプチャするため、
    `--debug` を付けたユーザーの自己責任では済まされない(§15/§13.2)。
    """
    from abist_kb.domain.errors import wrap

    secret_url = "https://api.esa.io/v1/teams/abist/posts?access_token=SUPER-SECRET-abc123"
    try:
        raise ConnectionError(f"GET {secret_url} failed")
    except ConnectionError as exc:
        err = wrap(exc, code=ErrorCode.EXTERNAL_SERVICE, message="esa API に接続できません")

    p = Presenter(OutputMode.PLAIN, width=120, debug=True)
    p.error(err)
    out = p.stderr_value()
    assert "SUPER-SECRET-abc123" not in out
    assert out.count("***") >= 2, "details とトレースバックの両方でマスクされていること"


def test_error_json_mode_masks_secrets_in_details():
    """I3 の派生ケース: `--output json` の details にも同じ `cause_message` が
    載りうるため、JSON モードの機械可読エラー出力でもマスクされること。
    """
    from abist_kb.domain.errors import wrap

    secret_url = "https://api.esa.io/v1/teams/abist/posts?access_token=SUPER-SECRET-abc123"
    try:
        raise ConnectionError(f"GET {secret_url} failed")
    except ConnectionError as exc:
        err = wrap(
            exc,
            code=ErrorCode.EXTERNAL_SERVICE,
            message="esa API に接続できません",
            details={"note": secret_url},
        )

    p = make(OutputMode.JSON)
    p.error(err)
    raw = p.stderr_value()
    assert "SUPER-SECRET-abc123" not in raw
    payload = json.loads(raw)
    assert payload["details"]["note"] == mask_secrets(secret_url)


def test_confirm_raises_in_plain_mode_when_stdin_is_not_a_tty(monkeypatch):
    """レビュー指摘3: `mode is JSON or not _stdin_is_tty()` の右側が実際に評価される
    経路を、JSON 以外のモードで直接検証する(短絡評価で握りつぶされないことの担保)。
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    p = make(OutputMode.PLAIN)
    err = None
    try:
        p.confirm("削除しますか", assume_yes=False)
    except AppError as exc:
        err = exc
    assert err is not None
    assert err.code == ErrorCode.INVALID_INPUT
    assert "--yes" in (err.hint or "")
