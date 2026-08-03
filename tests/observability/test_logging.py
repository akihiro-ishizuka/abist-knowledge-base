import io
import logging
import time

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


# 以下はコードレビュー(CRITICAL 1 / CRITICAL 2 / IMPORTANT 4 / IMPORTANT 6)で
# 指摘された不具合の回帰テスト。


def test_mask_secrets_handles_long_underscore_runs_quickly():
    """CRITICAL 1 の回帰テスト。

    `_KEY_VALUE_RE` の `[A-Za-z0-9_]*` が、直後の必須リテラル `_` と文字クラスとして
    重複していたため、TOKEN/KEY/SECRET/PASSWORD を含まない長いアンダースコア連結文字列
    (長いパス・ドキュメントID列・base64url 等、日常的なログ行に現れうる)に対して
    二次関数的なバックトラックが発生し、数秒〜数十秒単位で応答が止まっていた。
    修正後は長さに対してほぼ線形になるはずなので、寛容な上限(1.0秒)で
    「秒〜分単位に逆戻りしていないか」を検知する(マイクロベンチマークが目的ではない)。
    """
    text = "a_" * 32000  # 64,000 文字、秘密情報のキーワードは一切含まない
    start = time.perf_counter()
    mask_secrets(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"mask_secrets が遅すぎます(elapsed={elapsed:.2f}s)"


def test_mask_secrets_handles_long_hyphen_runs_quickly():
    """I6 の作業中に発見した、本レビュー指摘対象外の既存 ReDoS の回帰テスト。

    `_URL_CREDENTIAL_RE` のスキーム部が無制限の `[A-Za-z0-9+.\\-]*` だったため、
    `://` を含まない長いハイフン/ドット区切り文字列(バージョン文字列・
    kebab-case の識別子等)に対して二次関数的なバックトラックが発生し、
    64,000文字で2秒、128,000文字で8.8秒も応答が止まっていた
    (`test_mask_secrets_handles_long_underscore_runs_quickly` が使う
    アンダースコア区切りのテキストはこの文字クラスに含まれないため、
    既存のテストではこの不具合を検知できていなかった)。
    """
    text = "a-" * 32000  # 64,000 文字、`://` を一切含まない
    start = time.perf_counter()
    mask_secrets(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"mask_secrets が遅すぎます(elapsed={elapsed:.2f}s)"


def test_mask_secrets_cookie_masks_to_end_of_line_with_multiple_pairs():
    """IMPORTANT 4 の回帰テスト。

    `_COOKIE_RE` の value 部が `\\S+`(最初の空白区切りトークンのみ)だったため、
    実際の Cookie ヘッダで一般的な複数ペア(`a=1; b=2; ...`)のうち最初の1つしか
    マスクされず、2つ目以降がログに残っていた。
    """
    masked = mask_secrets("Cookie: sessionid=abc123; csrftoken=def456")
    assert "abc123" not in masked
    assert "def456" not in masked
    assert "***" in masked


def test_mask_secrets_json_key_word_boundary_masks_access_token():
    """IMPORTANT 6 の回帰テスト(陽性ケース)。

    `_KEY_VALUE_RE` と同じ「suffix の直前は `_` でなければならない」という
    語境界の制約を `_JSON_SECRET_RE` にも適用してよいことを確認する
    (`access_token` のような正当な名前は引き続きマスクされる)。
    """
    masked = mask_secrets('{"access_token": "abc123"}')
    assert "abc123" not in masked
    assert "***" in masked


@pytest.mark.parametrize(
    "raw",
    [
        "X-Api-Key: SECRETVAL",
        "x-api-key: SECRETVAL",
        "X-Auth-Token: SECRETVAL",
        "api-key=SECRETVAL",
    ],
)
def test_mask_secrets_masks_hyphenated_header_names(raw):
    """I6(IMPORTANT)の回帰テスト。

    `_KEY_VALUE_RE` はサフィックス直前の区切り文字として `_` のみを許していたため、
    `X-Api-Key: SECRETVAL` のようなハイフン区切りのヘッダ名(HTTP ヘッダの
    実世界での主流の綴り。httpx 等は `request.headers` をハイフン区切りで
    レンダリングする)を素通りさせていた。M3 の esa/Web アダプタが
    `request.headers` をログへ流す経路でこれを見逃すと、資格情報全体が漏れる。
    """
    masked = mask_secrets(raw)
    assert "SECRETVAL" not in masked
    assert "***" in masked


@pytest.mark.parametrize("innocent_key", ["monkey", "donkey", "jockey", "turkey_count"])
def test_mask_secrets_json_key_word_boundary_leaves_innocent_keys_alone(innocent_key):
    """IMPORTANT 6 の回帰テスト(陰性ケース)。

    `_JSON_SECRET_RE` が語境界を無視していたため、`key`/`token`等の文字列で
    「たまたま終わる」だけの無害な JSON キー(`monkey`, `donkey`, `jockey`,
    `turkey_count` 等)の値まで `***` に潰されていた。このツールは任意のユーザー/API
    の JSON をログへ通すため、正当な内容を無言で破壊すると診断が信頼できなくなる。
    """
    text = f'{{"{innocent_key}": "not a secret at all"}}'
    assert mask_secrets(text) == text


def test_configure_logging_masks_exception_traceback(capsys):
    """CRITICAL 2 の回帰テスト。

    `SecretMaskingFilter.filter()` は `record.msg`/`record.args` だけを書き換え、
    `record.exc_info` には触れていなかった。`logging.Formatter` はフィルタ適用後に
    トレースバックを整形して追記するため、`logger.exception(...)` で記録された
    例外メッセージ中の秘密情報がマスクされずにそのまま出力されていた。
    `wrap()` は捕捉した sqlite3 例外の生メッセージを `__cause__`/`details` に
    保持する設計(`AppError.to_dict()` はそれを機械可読出力から除外する)であり、
    `logger.exception(...)` でその連鎖トレースバックを出力する経路がこの保護を
    台無しにしていた。
    """
    stream = io.StringIO()
    configure_logging(level="DEBUG", stream=stream)
    logger = get_logger("abist_kb.test")
    try:
        raise RuntimeError("failed to connect using DB_PASSWORD=supersecretvalue at somehost")
    except RuntimeError:
        logger.exception("unexpected failure while connecting")
    out = stream.getvalue()
    assert "supersecretvalue" not in out
    assert "***" in out
    captured = capsys.readouterr()
    assert captured.out == ""


# `configure_logging(log_file=...)` のテスト。以前は一切カバーされていなかった
# (テスト計画書は §12 の「全ハンドラに SecretMaskingFilter」を要求しているが、
# ファイルハンドラにも実際に付いているかを確認するテストが存在しなかった)。


def _close_abist_kb_log_handlers() -> None:
    """テスト後にファイルハンドラを明示的に閉じる。

    Windows ではハンドルを閉じるまでファイルがロックされたままになり、
    次のテストが同じ `tmp_path` を再利用しなければ pytest のクリーンアップが
    `PermissionError` で失敗しうる。
    """
    logger = logging.getLogger("abist_kb")
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
    logger.handlers.clear()


def test_configure_logging_log_file_creates_parent_directory(tmp_path):
    log_file = tmp_path / "深い" / "階層" / "app.log"
    assert not log_file.parent.exists()

    try:
        configure_logging(level="INFO", stream=io.StringIO(), log_file=log_file)
        get_logger("abist_kb.test").info("作成確認")
    finally:
        _close_abist_kb_log_handlers()

    assert log_file.parent.is_dir()
    assert log_file.is_file()


def test_configure_logging_log_file_is_utf8(tmp_path):
    """ファイルハンドラが UTF-8 で書くこと(日本語を書いて読み戻して確認する)。"""
    log_file = tmp_path / "app.log"
    try:
        configure_logging(level="INFO", stream=io.StringIO(), log_file=log_file)
        get_logger("abist_kb.test").info("蛇腹形状の自動設計は正常に完了しました")
    finally:
        _close_abist_kb_log_handlers()

    content = log_file.read_text(encoding="utf-8")
    assert "蛇腹形状の自動設計は正常に完了しました" in content


def test_configure_logging_log_file_masks_secrets_too(tmp_path):
    """§12: ファイルハンドラにも `SecretMaskingFilter` が付いていること。
    stderr のストリームハンドラだけがマスクされ、ファイルには秘密情報が生で
    残る、という取りこぼしを防ぐ。
    """
    log_file = tmp_path / "app.log"
    try:
        configure_logging(level="INFO", stream=io.StringIO(), log_file=log_file)
        get_logger("abist_kb.test").info("ESA_ACCESS_TOKEN=abcdef123456 を使って同期しました")
    finally:
        _close_abist_kb_log_handlers()

    content = log_file.read_text(encoding="utf-8")
    assert "abcdef123456" not in content
    assert "***" in content
