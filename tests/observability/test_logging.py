import io
import logging

import pytest

from abist_kb.infrastructure.observability.logging import (
    SecretMaskingFilter,
    configure_logging,
    get_logger,
    mask_secrets,
)


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("ESA_ACCESS_TOKEN=abcdef123456", "abcdef123456"),
        ("OPENAI_API_KEY=sk-proj-XYZ987", "sk-proj-XYZ987"),
        ("ABIST_KB_ESA_ACCESS_TOKEN: nekopunch", "nekopunch"),
        ("Authorization: Bearer eyJhbGciOi.J9.sig", "eyJhbGciOi.J9.sig"),
        ("Cookie: session=deadbeef", "deadbeef"),
        ("https://user:hunter2@example.com/repo.git", "hunter2"),
        ('{"api_key": "kkk-111"}', "kkk-111"),
    ],
)
def test_mask_secrets_removes_the_value(raw, must_not_contain):
    masked = mask_secrets(raw)
    assert must_not_contain not in masked
    assert "***" in masked


def test_mask_secrets_keeps_the_key_name_for_diagnosis():
    assert "ESA_ACCESS_TOKEN" in mask_secrets("ESA_ACCESS_TOKEN=abcdef123456")


def test_mask_secrets_leaves_innocent_text_alone():
    text = "docs/蛇腹形状の自動設計/議事録.md を同期しました(3件)"
    assert mask_secrets(text) == text


def test_mask_secrets_masks_git_url_but_keeps_host():
    masked = mask_secrets("https://user:hunter2@github.com/abist/repo.git")
    assert "hunter2" not in masked
    assert "github.com/abist/repo.git" in masked


def test_filter_masks_message_and_args():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretMaskingFilter())
    logger = logging.getLogger("test.masking")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("token=%s", "ESA_ACCESS_TOKEN=abcdef123456")
    logger.info("Authorization: Bearer secret-value-here")

    out = stream.getvalue()
    assert "abcdef123456" not in out
    assert "secret-value-here" not in out


def test_configure_logging_writes_to_stderr_stream_with_level():
    stream = io.StringIO()
    configure_logging(level="WARNING", stream=stream)
    logger = get_logger("abist_kb.test")
    logger.info("これは出ない")
    logger.warning("これは出る")
    out = stream.getvalue()
    assert "これは出ない" not in out
    assert "これは出る" in out


def test_configure_logging_is_idempotent_and_does_not_duplicate_handlers():
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    configure_logging(level="INFO", stream=stream)
    get_logger("abist_kb.test").info("一度だけ")
    assert stream.getvalue().count("一度だけ") == 1


def test_configure_logging_never_writes_to_stdout(capsys):
    configure_logging(level="DEBUG")
    get_logger("abist_kb.test").error("エラーです")
    captured = capsys.readouterr()
    assert captured.out == ""
