"""`LocalEmbeddingProvider` の子プロセス死亡・ハング検知(M4 Task2b、堅牢性修正)。

`.superpowers/sdd/M4-index-search/task-2b-timeout-report.md` 参照。

実モデル(`KB_RUN_MODEL_TESTS=1`)を必要とせず、実際に `multiprocessing` の子
プロセスを起動してテストする。`model_loader` にダミーの「モデル」(`encode` が
`time.sleep` するだけのオブジェクト)を注入することで、モデルダウンロード無しで
本物のプロセス生成・キュー通信・強制終了(kill)を検証する。

`model_loader` は `spawn` コンテキストで子プロセスへ pickle されるため、
クロージャではなくモジュールトップレベルの picklable なクラスとして定義する。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from abist_kb.domain.errors import AppError
from abist_kb.infrastructure.ai.embedding_provider import LocalEmbeddingProvider


class _SleepyModel:
    """`encode` が指定秒数だけ待ってからダミーベクトルを返す軽量モデル代替。"""

    def __init__(self, sleep_seconds: float) -> None:
        self.sleep_seconds = sleep_seconds

    def encode(self, texts, **kwargs):
        time.sleep(self.sleep_seconds)
        return [np.zeros(4, dtype="<f4") for _ in texts]


class _SleepyModelLoader:
    """`model_loader` として渡す picklable な呼び出し可能オブジェクト。"""

    def __init__(self, sleep_seconds: float) -> None:
        self.sleep_seconds = sleep_seconds

    def __call__(self, model_name: str) -> _SleepyModel:
        return _SleepyModel(self.sleep_seconds)


def _make_provider(*, encode_sleep_seconds: float, batch_timeout: float) -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider(
        dimensions=4,
        batch_size=2,
        model_loader=_SleepyModelLoader(encode_sleep_seconds),
        ready_timeout=15.0,
        batch_timeout=batch_timeout,
        max_restart_attempts=0,  # このテストでは復旧させず、死亡を直接観測する
    )


# -- 子プロセスを実際に kill する ---------------------------------------------


def test_dead_child_is_detected_within_timeout_and_reaped():
    """バッチ処理中に子プロセスを実際に kill すると、タイムアウト以内に検知され、
    明確な `AppError` が送出され、プロセスが reap されること。
    """
    provider = _make_provider(encode_sleep_seconds=5.0, batch_timeout=3.0)
    try:
        result: dict = {}

        def call_embed_batch():
            try:
                provider.embed_batch(["a", "b"])
            except BaseException as exc:  # noqa: BLE001 - スレッド越しに例外を伝搬させる
                result["error"] = exc

        thread = threading.Thread(target=call_embed_batch)
        started_at = time.monotonic()
        thread.start()

        # 子プロセスが encode() 中(5秒スリープ)であろうタイミングで実際に kill する。
        time.sleep(0.5)
        assert provider._process.is_alive()
        provider._process.kill()

        thread.join(timeout=10.0)
        elapsed = time.monotonic() - started_at

        assert not thread.is_alive(), "embed_batch がタイムアウト以内に返らなかった"
        assert elapsed < 3.0 + 2.0  # batch_timeout(3秒)+検知・reap の余裕
        assert "error" in result, "子プロセス死亡が検知されずに正常終了してしまった"
        assert isinstance(result["error"], AppError)
        assert result["error"].retryable is False
        assert not provider._process.is_alive()
    finally:
        provider._closed = True  # close() が再度 kill 済みプロセスへ触れないようにする


def test_prior_batches_survive_child_crash(tmp_path):
    """クラッシュ前に書き込み済みの埋め込みは、子プロセス死亡後も失われない
    (再開可能性、brief の要求)。generate_embeddings 経由で確認する。
    """
    from abist_kb.domain.errors import ErrorCode
    from abist_kb.infrastructure.db.connection import connect
    from abist_kb.infrastructure.search.index_schema import (
        DEFAULT_TOKENIZERS,
        create_index_schema,
    )

    conn = connect(tmp_path / "index.sqlite")
    create_index_schema(conn, DEFAULT_TOKENIZERS)
    try:
        conn.execute(
            "INSERT INTO documents (path, document_hash, chunk_count) VALUES (?, 'h', 0)",
            ("a.md",),
        )
        for i in range(4):
            conn.execute(
                "INSERT INTO chunks (path, chunk_index, title, heading_path, text) "
                "VALUES (?, ?, ?, ?, ?)",
                ("a.md", i, "タイトル", None, f"本文{i}"),
            )

        # FakeProvider 相当だが「2バッチ目で異常終了する」ことを模した provider。
        class CrashOnSecondBatch:
            provider = "local"
            model_identity = "test-model"
            dimensions = 4
            batch_size = 2

            def __init__(self) -> None:
                self.calls = 0

            def embed_batch(self, texts):
                self.calls += 1
                if self.calls == 2:
                    raise AppError(
                        code=ErrorCode.FAILURE,
                        message="子プロセスが異常終了しました(テスト用)。",
                        retryable=False,
                    )
                return [np.zeros(4, dtype="<f4") for _ in texts]

        from abist_kb.infrastructure.ai.embedding_provider import generate_embeddings

        provider = CrashOnSecondBatch()
        with pytest.raises(AppError):
            generate_embeddings(conn, provider, corpus="work")

        count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        assert count == 2  # 1バッチ目(先頭2件)はコミット済みのまま残る
    finally:
        conn.close()


# -- 生存中の遅いバッチは誤検知しない ------------------------------------------


def test_slow_but_alive_child_is_not_mistaken_for_crash():
    """バッチが遅いだけ(タイムアウト未満)で、プロセスが生存し続けているなら、
    誤って死亡・タイムアウトと判定されず正常に結果が返ること。
    """
    provider = _make_provider(encode_sleep_seconds=1.0, batch_timeout=10.0)
    try:
        vectors = provider.embed_batch(["a", "b"])
        assert len(vectors) == 2
        assert provider._process.is_alive()
    finally:
        provider.close()


def test_timeout_with_alive_child_raises_distinct_error_and_does_not_restart():
    """タイムアウトしたが子プロセスが生存中の場合、クラッシュとは別のエラーを送出し、
    (このテストでは `max_restart_attempts=0` なので)復旧を試みずに reap すること。
    """
    provider = _make_provider(encode_sleep_seconds=5.0, batch_timeout=1.0)
    try:
        with pytest.raises(AppError) as excinfo:
            provider.embed_batch(["a", "b"])
        assert "生存中" in excinfo.value.message
        assert excinfo.value.retryable is False
    finally:
        provider._closed = True  # 上のcallで既にreap済み
