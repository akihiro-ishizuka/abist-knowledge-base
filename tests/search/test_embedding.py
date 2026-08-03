"""`infrastructure.ai.embedding_provider`(brief M4 Task2)。

重いモデルの実ロード・ダウンロードを要する検証(`LocalEmbeddingProvider` の実際の
子プロセス+sentence-transformers 統合、§11.2 の再利用ゲート実測)は
`KB_RUN_MODEL_TESTS=1` を明示したときだけ実行する(`tests/sources/test_e2e_byte_identity.py`
の `KB_OLD_REPO` と同じ「既定はスキップ、明示的opt-inで実行」方針)。既定の
`uv run pytest -q` はネットワーク・モデルダウンロードなしで完走する。

`generate_embeddings` のストリーミング・差分・再開可能性・reference guard・
meta書込みは、`EmbeddingProvider` port を実装する軽量な `FakeProvider`
(子プロセス無し、決定的なベクトルを返すだけ)で検証する——`IndexBuilder` の
テストが実チャンカーを使うのと同じ粒度で、モデル推論そのものはこのテストの
関心事ではない。
"""

from __future__ import annotations

import base64
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from abist_kb.infrastructure.ai.embedding_provider import (
    HASH_MODEL_IDENTITY,
    LOCAL_BATCH_SIZE,
    LOCAL_DIMENSIONS,
    LOCAL_LOAD_MODEL_NAME,
    OPENAI_BATCH_SIZE,
    EmbeddingProvider,
    GenerationSummary,
    LocalEmbeddingProvider,
    OpenAIEmbeddingProvider,
    generate_embeddings,
)
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.search.e5_input import EmbeddingInputChunk, embedding_input, input_hash
from abist_kb.infrastructure.search.index_schema import DEFAULT_TOKENIZERS, create_index_schema

_GATE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "embedding" / "gate-samples.json"


# -- テスト用の軽量プロバイダ(子プロセスを起動しない) -------------------------


class FakeProvider:
    """`EmbeddingProvider` port を満たす決定的なテスト用スタブ。"""

    provider = "local"

    def __init__(
        self,
        *,
        model_identity: str = HASH_MODEL_IDENTITY,
        dimensions: int = LOCAL_DIMENSIONS,
        batch_size: int = 2,
    ) -> None:
        self.model_identity = model_identity
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.batch_calls: list[list[str]] = []

    def embed_batch(self, texts):
        self.batch_calls.append(list(texts))
        vectors = []
        for text in texts:
            # テキストのハッシュから決定的な単位ベクトルを作る(モデル推論の代替)。
            rng = np.random.default_rng(abs(hash(text)) % (2**32))
            v = rng.standard_normal(self.dimensions).astype("<f4")
            v /= np.linalg.norm(v)
            vectors.append(v)
        return vectors


def _open_index(tmp_root: Path) -> sqlite3.Connection:
    conn = connect(tmp_root / "index.sqlite")
    create_index_schema(conn, DEFAULT_TOKENIZERS)
    return conn


def _insert_document(conn: sqlite3.Connection, path: str) -> None:
    conn.execute(
        "INSERT INTO documents (path, document_hash, chunk_count) VALUES (?, 'h', 0)",
        (path,),
    )


def _insert_chunk(
    conn: sqlite3.Connection,
    *,
    path: str,
    chunk_index: int,
    title: str | None = "タイトル",
    heading_path: str | None = None,
    text: str = "本文です。",
) -> int:
    cur = conn.execute(
        "INSERT INTO chunks (path, chunk_index, title, heading_path, text) VALUES (?, ?, ?, ?, ?)",
        (path, chunk_index, title, heading_path, text),
    )
    return cur.lastrowid


def _embedding_row(conn: sqlite3.Connection, chunk_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM embeddings WHERE chunk_id = ?", (chunk_id,)).fetchone()


# -- generate_embeddings: 基本の生成 ------------------------------------------


def test_generates_embeddings_for_new_chunks(tmp_root: Path):
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        c1 = _insert_chunk(conn, path="a.md", chunk_index=0, text="最初のチャンク")
        c2 = _insert_chunk(conn, path="a.md", chunk_index=1, text="2番目のチャンク")

        provider = FakeProvider(batch_size=16)
        summary = generate_embeddings(conn, provider, corpus="work")

        assert summary.scanned == 2
        assert summary.generated == 2
        assert summary.skipped == 0

        for chunk_id in (c1, c2):
            row = _embedding_row(conn, chunk_id)
            assert row is not None
            assert row["model"] == HASH_MODEL_IDENTITY
            assert row["dimensions"] == LOCAL_DIMENSIONS
            assert row["created_at"]
            vector = np.frombuffer(row["vector"], dtype="<f4")
            assert vector.shape == (LOCAL_DIMENSIONS,)
            # L2正規化されたベクトルであること(FakeProviderが正規化して返す)。
            assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-5
    finally:
        conn.close()


def test_vector_blob_is_little_endian_float32(tmp_root: Path):
    """brief: 「BLOBは `np.asarray(v, dtype='<f4').tobytes()`」。"""
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        chunk_id = _insert_chunk(conn, path="a.md", chunk_index=0, text="本文")

        provider = FakeProvider(dimensions=4, batch_size=16)
        generate_embeddings(conn, provider, corpus="work")

        row = _embedding_row(conn, chunk_id)
        assert row is not None
        assert len(row["vector"]) == 4 * 4  # 4次元 x float32(4バイト)
        # <f4 として素直に読み直せること。
        np.frombuffer(row["vector"], dtype="<f4")
    finally:
        conn.close()


def test_input_uses_e5_input_module_and_hash_identity(tmp_root: Path):
    """`embedding_input`/`input_hash` を `HASH_MODEL_IDENTITY` で呼んだ結果と一致すること。

    ハッシュ用識別子は `Xenova/...`(ロード先の `intfloat/...` ではない)。
    """
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        _insert_chunk(
            conn,
            path="a.md",
            chunk_index=0,
            title="題名",
            heading_path="A > B",
            text="本文テキスト",
        )

        provider = FakeProvider(batch_size=16)
        generate_embeddings(conn, provider, corpus="work")

        expected_input = embedding_input(
            EmbeddingInputChunk(text="本文テキスト", title="題名", heading_path="A > B"),
            HASH_MODEL_IDENTITY,
        )
        expected_hash = input_hash(HASH_MODEL_IDENTITY, expected_input)

        row = conn.execute("SELECT input_hash FROM embeddings").fetchone()
        assert row["input_hash"] == expected_hash
        assert provider.batch_calls == [[expected_input]]
    finally:
        conn.close()


# -- 差分生成・スキップ・再開可能性 -------------------------------------------


def test_skips_chunks_with_matching_model_and_input_hash(tmp_root: Path):
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        chunk_id = _insert_chunk(conn, path="a.md", chunk_index=0, text="変わらない本文")

        provider = FakeProvider(batch_size=16)
        generate_embeddings(conn, provider, corpus="work")
        assert len(provider.batch_calls) == 1

        first_row = _embedding_row(conn, chunk_id)
        assert first_row is not None

        # 2回目の実行: 何も変わっていないので埋め込みを作り直さない。
        summary2 = generate_embeddings(conn, provider, corpus="work")
        assert summary2.generated == 0
        assert summary2.skipped == 1
        assert len(provider.batch_calls) == 1  # 追加のembed_batch呼び出しが無い

        second_row = _embedding_row(conn, chunk_id)
        assert second_row["vector"] == first_row["vector"]  # 再生成されていない
    finally:
        conn.close()


def test_regenerates_when_model_changes(tmp_root: Path):
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        chunk_id = _insert_chunk(conn, path="a.md", chunk_index=0, text="本文")

        provider_a = FakeProvider(model_identity="Xenova/multilingual-e5-small", batch_size=16)
        generate_embeddings(conn, provider_a, corpus="work")

        provider_b = FakeProvider(model_identity="Xenova/multilingual-e5-base", batch_size=16)
        summary = generate_embeddings(conn, provider_b, corpus="work")

        assert summary.generated == 1
        assert summary.skipped == 0
        row = _embedding_row(conn, chunk_id)
        assert row["model"] == "Xenova/multilingual-e5-base"
    finally:
        conn.close()


def test_regenerates_when_text_changes(tmp_root: Path):
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        chunk_id = _insert_chunk(conn, path="a.md", chunk_index=0, text="旧テキスト")

        provider = FakeProvider(batch_size=16)
        generate_embeddings(conn, provider, corpus="work")
        old_row = _embedding_row(conn, chunk_id)

        conn.execute("UPDATE chunks SET text = ? WHERE id = ?", ("新テキスト", chunk_id))
        summary = generate_embeddings(conn, provider, corpus="work")

        assert summary.generated == 1
        new_row = _embedding_row(conn, chunk_id)
        assert new_row["input_hash"] != old_row["input_hash"]
        assert new_row["vector"] != old_row["vector"]
    finally:
        conn.close()


def test_resumable_after_partial_batch_failure(tmp_root: Path):
    """バッチ途中で例外が起きても、既にコミット済みの前バッチは残り、
    再実行はそこから続きだけを処理する(再開可能性、brief Step3)。
    """
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        chunk_ids = [
            _insert_chunk(conn, path="a.md", chunk_index=i, text=f"本文{i}") for i in range(4)
        ]

        class FlakyProvider(FakeProvider):
            def __init__(self):
                super().__init__(batch_size=2)
                self.calls = 0

            def embed_batch(self, texts):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("2バッチ目で人為的に失敗")
                return super().embed_batch(texts)

        flaky = FlakyProvider()
        with pytest.raises(RuntimeError):
            generate_embeddings(conn, flaky, corpus="work")

        # 1バッチ目(先頭2件)はコミット済みのはず。
        rows_after_failure = [_embedding_row(conn, cid) is not None for cid in chunk_ids]
        assert rows_after_failure[:2] == [True, True]
        assert rows_after_failure[2:] == [False, False]

        # 再実行: 既に埋め込み済みの2件はスキップし、残り2件だけ処理する。
        resume_provider = FakeProvider(batch_size=16)
        summary = generate_embeddings(conn, resume_provider, corpus="work")
        assert summary.generated == 2
        assert summary.skipped == 2
        assert all(_embedding_row(conn, cid) is not None for cid in chunk_ids)
    finally:
        conn.close()


def test_generate_embeddings_source_never_calls_fetchall():
    """`.fetchall()` で候補行を一括メモリ展開していないことを静的に確認する(brief Step3)。

    `sqlite3.Cursor` はCの組み込み型で `monkeypatch.setattr` できない
    (`TypeError: cannot set 'fetchall' attribute of immutable type`)ため、
    実行時スパイの代わりにソースを検査する。候補クエリのカーソルは
    `for row in cursor:` で直接イテレートし、`.fetchall()` を一度も呼ばない
    実装であることの回帰ガード。
    """
    import inspect

    from abist_kb.infrastructure.ai import embedding_provider as module

    source = inspect.getsource(module.generate_embeddings)
    assert "cursor.fetchall(" not in source


def test_streams_in_batches_matching_provider_batch_size(tmp_root: Path):
    """候補が `provider.batch_size` 件たまるたびに埋め込みが分割生成されること。

    ストリーミング処理の外形(全件を一括で1バッチ扱いしていないこと)を、
    `embed_batch` への呼び出し単位で確認する。
    """
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        for i in range(10):
            _insert_chunk(conn, path="a.md", chunk_index=i, text=f"本文{i}")

        provider = FakeProvider(batch_size=3)
        summary = generate_embeddings(conn, provider, corpus="work")

        assert summary.scanned == 10
        assert summary.generated == 10
        # 10件をバッチサイズ3で処理: 3,3,3,1件の4バッチに分かれる。
        assert [len(batch) for batch in provider.batch_calls] == [3, 3, 3, 1]
    finally:
        conn.close()


# -- reference コーパス: 埋め込みを作らない -----------------------------------


def test_reference_corpus_generates_nothing(tmp_root: Path):
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "knowledge/B32doc/x.md")
        _insert_chunk(conn, path="knowledge/B32doc/x.md", chunk_index=0, text="参照コーパス本文")

        provider = FakeProvider(batch_size=16)
        summary = generate_embeddings(conn, provider, corpus="reference")

        assert summary == GenerationSummary()
        assert provider.batch_calls == []
        count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        assert count == 0
        # meta も書き込まれない(生成自体が起きていないため)。
        meta_row = conn.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()
        assert meta_row is None
    finally:
        conn.close()


def test_unknown_corpus_rejected(tmp_root: Path):
    conn = _open_index(tmp_root)
    try:
        with pytest.raises(ValueError):
            generate_embeddings(conn, FakeProvider(), corpus="something-else")
    finally:
        conn.close()


# -- meta 書込み ---------------------------------------------------------------


def test_writes_meta_on_success(tmp_root: Path):
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        _insert_chunk(conn, path="a.md", chunk_index=0, text="本文")

        provider = FakeProvider(batch_size=16)
        generate_embeddings(conn, provider, corpus="work")

        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        assert meta["embedding_model"] == HASH_MODEL_IDENTITY
        assert meta["embedding_provider"] == "local"
        assert meta["embedding_updated_at"]
    finally:
        conn.close()


# -- check_lease / emit の配線 --------------------------------------------------


def test_check_lease_called_before_each_batch_write(tmp_root: Path):
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        for i in range(4):
            _insert_chunk(conn, path="a.md", chunk_index=i, text=f"本文{i}")

        calls = []

        def check_lease():
            calls.append(len(calls))

        provider = FakeProvider(batch_size=2)
        generate_embeddings(conn, provider, corpus="work", check_lease=check_lease)

        assert len(calls) == 2  # 4件 / バッチサイズ2 = 2バッチ
    finally:
        conn.close()


def test_emit_reports_progress(tmp_root: Path):
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        _insert_chunk(conn, path="a.md", chunk_index=0, text="本文")

        events = []

        def emit(**kwargs):
            events.append(kwargs)

        provider = FakeProvider(batch_size=16)
        generate_embeddings(conn, provider, corpus="work", emit=emit)

        assert any(e.get("phase") == "embed" for e in events)
    finally:
        conn.close()


def test_check_lease_stops_generation_when_raised(tmp_root: Path):
    """`check_lease` が例外を送出したら、以後のバッチ書込みが起きないこと。"""
    conn = _open_index(tmp_root)
    try:
        _insert_document(conn, "a.md")
        for i in range(4):
            _insert_chunk(conn, path="a.md", chunk_index=i, text=f"本文{i}")

        class Lost(Exception):
            pass

        calls = {"n": 0}

        def check_lease():
            calls["n"] += 1
            if calls["n"] == 2:
                raise Lost("リースを失った")

        provider = FakeProvider(batch_size=2)
        with pytest.raises(Lost):
            generate_embeddings(conn, provider, corpus="work", check_lease=check_lease)

        # 1バッチ目までは書き込まれているはず。
        count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        assert count == 2
    finally:
        conn.close()


# -- OpenAIEmbeddingProvider: バッチサイズ・リトライ方針 -----------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def json(self):
        return self._json_body


class _FakeHttpClient:
    """`httpx.Client` の `post` だけをスタブ化する(実ネットワークを使わない)。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, *, headers, json):
        response = self._responses[self.calls]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


def test_openai_provider_defaults_batch_size_100():
    assert OPENAI_BATCH_SIZE == 100


def test_openai_provider_succeeds_first_try():
    body = {
        "data": [
            {"index": 1, "embedding": [0.5, 0.5]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    client = _FakeHttpClient([_FakeResponse(200, body)])
    sleeps = []
    provider = OpenAIEmbeddingProvider(
        model_identity="text-embedding-3-small",
        dimensions=2,
        api_key="sk-test",
        client=client,
        sleep=sleeps.append,
    )
    vectors = provider.embed_batch(["a", "b"])
    assert client.calls == 1
    assert sleeps == []
    # index順に並び替えて返す。
    assert list(vectors[0]) == [1.0, 0.0]
    assert list(vectors[1]) == [0.5, 0.5]


def test_openai_provider_retries_on_429_then_succeeds():
    body = {"data": [{"index": 0, "embedding": [1.0]}]}
    client = _FakeHttpClient([_FakeResponse(429), _FakeResponse(200, body)])
    sleeps = []
    provider = OpenAIEmbeddingProvider(
        model_identity="text-embedding-3-small",
        dimensions=1,
        api_key="sk-test",
        client=client,
        sleep=sleeps.append,
    )
    vectors = provider.embed_batch(["a"])
    assert client.calls == 2
    assert sleeps == [1]  # 2**0
    assert list(vectors[0]) == [1.0]


def test_openai_provider_retries_on_500_with_exponential_backoff():
    body = {"data": [{"index": 0, "embedding": [1.0]}]}
    client = _FakeHttpClient([_FakeResponse(500), _FakeResponse(503), _FakeResponse(200, body)])
    sleeps = []
    provider = OpenAIEmbeddingProvider(
        model_identity="text-embedding-3-small",
        dimensions=1,
        api_key="sk-test",
        client=client,
        sleep=sleeps.append,
    )
    provider.embed_batch(["a"])
    assert sleeps == [1, 2]  # 2**0, 2**1


def test_openai_provider_does_not_retry_on_400():
    from abist_kb.domain.errors import AppError

    client = _FakeHttpClient([_FakeResponse(400)])
    sleeps = []
    provider = OpenAIEmbeddingProvider(
        model_identity="text-embedding-3-small",
        dimensions=1,
        api_key="sk-test",
        client=client,
        sleep=sleeps.append,
    )
    with pytest.raises(AppError):
        provider.embed_batch(["a"])
    assert client.calls == 1  # 再試行していない
    assert sleeps == []


def test_openai_provider_gives_up_after_four_attempts():
    from abist_kb.domain.errors import AppError

    client = _FakeHttpClient([_FakeResponse(429)] * 10)
    sleeps = []
    provider = OpenAIEmbeddingProvider(
        model_identity="text-embedding-3-small",
        dimensions=1,
        api_key="sk-test",
        client=client,
        sleep=sleeps.append,
    )
    with pytest.raises(AppError):
        provider.embed_batch(["a"])
    assert client.calls == 4  # brief: 最大4回
    assert sleeps == [1, 2, 4]  # 4回目の後は待たずに諦める


# -- EmbeddingProvider port の型検証 -------------------------------------------


def test_fake_provider_satisfies_protocol():
    provider: EmbeddingProvider = FakeProvider()
    assert provider.provider == "local"
    assert provider.model_identity == HASH_MODEL_IDENTITY
    assert provider.dimensions == LOCAL_DIMENSIONS


# -- 実モデル統合(既定はスキップ、`KB_RUN_MODEL_TESTS=1` で有効化) --------------


_run_model_tests = os.environ.get("KB_RUN_MODEL_TESTS") == "1"

pytestmark_model = pytest.mark.skipif(
    not _run_model_tests,
    reason=(
        "実モデル(sentence-transformers)のダウンロード・ロードを伴うため既定はスキップ。"
        "KB_RUN_MODEL_TESTS=1 を指定すると実行する。"
    ),
)


@pytestmark_model
@pytest.mark.slow
def test_local_provider_real_model_round_trip():
    """`LocalEmbeddingProvider` が実際に子プロセスでモデルをロードし推論できること。"""
    provider = LocalEmbeddingProvider()
    try:
        assert provider.load_model_name == LOCAL_LOAD_MODEL_NAME
        vectors = provider.embed_batch(["passage: テスト文", "passage: another test"])
        assert len(vectors) == 2
        for v in vectors:
            assert v.shape == (LOCAL_DIMENSIONS,)
            assert v.dtype == np.dtype("<f4")
            assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-4
    finally:
        provider.close()


@pytestmark_model
@pytest.mark.slow
def test_local_provider_survives_multiple_batches_without_reloading():
    """複数バッチを送っても子プロセスがモデルを1回だけロードして使い回すこと。"""
    provider = LocalEmbeddingProvider(batch_size=LOCAL_BATCH_SIZE)
    try:
        first = provider.embed_batch(["passage: 一つ目"])
        second = provider.embed_batch(["passage: 二つ目"])
        assert first[0].shape == (LOCAL_DIMENSIONS,)
        assert second[0].shape == (LOCAL_DIMENSIONS,)
    finally:
        provider.close()


@pytestmark_model
@pytest.mark.slow
def test_embedding_gate_against_recorded_vectors():
    """§11.2 の再利用ゲート(100件、コサイン類似度 >= 0.999)を実測する。

    **不合格が既定の想定**(旧: int8量子化ONNX、新: fp32 PyTorch)。このテストは
    「全件合格」を要求しない——実測結果は `.superpowers/sdd/M4-index-search/
    task-2-report.md` と `tests/fixtures/PROVENANCE.md` に記録済み(全件0.999未満、
    最小 0.9925 / 中央値 0.9960)。ここでは巨大な破壊的回帰(類似度が明らかに
    無関係な値まで落ちる)だけを検知する緩い下限を置く。
    """
    import json

    with _GATE_FIXTURE.open(encoding="utf-8") as f:
        data = json.load(f)
    cases = data["cases"]

    provider = LocalEmbeddingProvider()
    try:
        inputs = [base64.b64decode(c["embeddingInput_b64"]).decode("utf-8") for c in cases]
        vectors: list[np.ndarray] = []
        for start in range(0, len(inputs), provider.batch_size):
            vectors.extend(provider.embed_batch(inputs[start : start + provider.batch_size]))
    finally:
        provider.close()

    sims = []
    for case, vector in zip(cases, vectors, strict=True):
        old_vec = np.frombuffer(base64.b64decode(case["vector_b64"]), dtype="<f4")
        new_vec = np.asarray(vector, dtype="<f4")
        denom = np.linalg.norm(old_vec) * np.linalg.norm(new_vec)
        cos = float(np.dot(old_vec, new_vec) / denom) if denom else 0.0
        sims.append(cos)

    assert len(sims) == 100
    assert min(sims) > 0.98  # 破壊的回帰の検知のみ。0.999ゲート自体は不合格が既定想定。
