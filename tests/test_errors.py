import pytest

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode, wrap


def test_exit_codes_match_design():
    assert ExitCode.SUCCESS == 0
    assert ExitCode.FAILURE == 1
    assert ExitCode.INVALID_INPUT == 2
    assert ExitCode.CONFIG_ERROR == 3
    assert ExitCode.EXTERNAL_SERVICE == 4
    assert ExitCode.CONFLICT == 5
    assert ExitCode.CANCELLED == 130


def test_app_error_is_an_exception_with_message():
    err = AppError(code=ErrorCode.INVALID_INPUT, message="パスが不正です")
    assert isinstance(err, Exception)
    assert str(err) == "パスが不正です"
    assert err.hint is None
    assert err.details == {}
    assert err.retryable is False
    assert err.exit_code == ExitCode.FAILURE


def test_app_error_carries_hint_details_and_exit_code():
    err = AppError(
        code=ErrorCode.FTS5_TRIGRAM_UNAVAILABLE,
        message="trigram トークナイザが利用できません",
        hint="SQLite 3.34.0 以上を含む Python で再実行してください",
        details={"sqlite_version": "3.30.0"},
        retryable=False,
        exit_code=ExitCode.CONFIG_ERROR,
    )
    assert err.exit_code == ExitCode.CONFIG_ERROR
    assert err.details["sqlite_version"] == "3.30.0"


def test_to_dict_is_json_serializable_and_stable():
    err = AppError(
        code=ErrorCode.EXTERNAL_SERVICE,
        message="esa API に接続できません",
        hint="ネットワークとトークンを確認してください",
        details={"status": 503},
        retryable=True,
    )
    assert err.to_dict() == {
        "code": "EXTERNAL_SERVICE",
        "message": "esa API に接続できません",
        "hint": "ネットワークとトークンを確認してください",
        "details": {"status": 503},
        "retryable": True,
    }


def test_wrap_preserves_cause_and_does_not_leak_raw_type_into_message():
    original = ValueError("boom")
    err = wrap(original, code=ErrorCode.FAILURE, message="処理に失敗しました")
    assert isinstance(err, AppError)
    assert err.__cause__ is original
    assert err.message == "処理に失敗しました"
    assert err.details["cause_type"] == "ValueError"


def test_to_dict_omits_cause_message_but_keeps_cause_type():
    original = ConnectionError(
        "failed to reach https://api.esa.io/v1/teams?access_token=super-secret-abc123"
    )
    err = wrap(original, code=ErrorCode.EXTERNAL_SERVICE, message="esa API に接続できません")
    dumped = err.to_dict()
    assert "cause_message" not in dumped["details"]
    assert dumped["details"]["cause_type"] == "ConnectionError"
    assert "super-secret-abc123" not in repr(dumped)
    # details 自体には --debug 用に cause_message を残す。
    assert "cause_message" in err.details


def test_error_code_values_are_screaming_snake_strings():
    for member in ErrorCode:
        assert member.value == member.name
        assert member.value.isupper()


def test_app_error_can_be_raised_and_caught():
    with pytest.raises(AppError) as excinfo:
        raise AppError(
            code=ErrorCode.CANCELLED, message="中断しました", exit_code=ExitCode.CANCELLED
        )
    assert excinfo.value.exit_code == ExitCode.CANCELLED
