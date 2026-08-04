"""埋め込みプロバイダと差分生成(設計書 §11.2、`design/plans/M4-index-search.md` Task2)。

**モデル名の二重身分(`tests/fixtures/PROVENANCE.md` / `e5_input.py` 参照)。**
`embeddings.model` / `input_hash` に刻む識別子は旧実装がハッシュに使った文字列
`Xenova/multilingual-e5-small`(`HASH_MODEL_IDENTITY`)のまま固定する。実際に
`sentence-transformers` でロードするモデル名は非量子化の `intfloat/multilingual-
e5-small`(`LOCAL_LOAD_MODEL_NAME`)であり、この2つは同じモデルを指しながら
文字列としては別物のまま共存させる。**このモジュールを触る誰かが「名前を統一
しよう」と `HASH_MODEL_IDENTITY` をロード先へ寄せてはならない**——`input_hash`
はモデル名の文字列そのものをハッシュに含めるため、変えると
`tests/fixtures/embedding/gate-samples.json` との照合が壊れる。

**§11.2 の再利用ゲートは不合格が既定の想定である。** 旧実装は int8 量子化 ONNX
(Xenova)、Python側は fp32 PyTorch のため、ビット一致はしない。実測結果
(スループット・コサイン類似度分布)は
`.superpowers/sdd/M4-index-search/task-2-report.md` と
`tests/fixtures/PROVENANCE.md` に記録する。

**入力構築は `infrastructure.search.e5_input` を再利用する**(切り詰め・
サロゲート処理を再実装しない)。

**参照コーパスは埋め込みを作らない**(旧 `reference-index.sqlite` は
`embeddings` 0行、実測で確認済み)。`generate_embeddings(..., corpus="reference")`
は何もせずに空の `GenerationSummary` を返す(brief Step4)。

**ストリーミングと再開可能性(brief Step3)。** `chunks` を `LEFT JOIN
embeddings` した結果を `sqlite3` のカーソルとして直接イテレートし、
`.fetchall()` で51,391行を一括メモリ展開しない。バッチが埋まるたびに
即座に `embeddings` へ書き込む(バッチ単位のコミット)ため、生成の途中で
中断しても、次回実行時は「`model` と `input_hash` が一致する行」をスキップする
差分ロジックだけで再開できる——専用のチェックポイント機構は不要。

**CPU負荷の高いモデル推論は子プロセスで実行する(brief Step3)。**
`LocalEmbeddingProvider` はモデルを1回だけロードして常駐する子プロセスへ
バッチを送り、結果を受け取る。メインプロセス側は推論中もCPUを使い切らないため、
`run_job` のリース更新スレッド(`infrastructure/jobs/execution.py`)や
進捗イベントの配信が滞らない。
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from queue import Empty
from typing import Any, Protocol

import numpy as np

from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.connection import transaction
from abist_kb.infrastructure.search.e5_input import (
    EmbeddingInputChunk,
    embedding_input,
    input_hash,
)

EmitFn = Callable[..., None]
CheckLeaseFn = Callable[[], None]

#: `embeddings.model` / `input_hash` に刻む識別子(ハッシュ用、モジュール docstring 参照)。
HASH_MODEL_IDENTITY = "Xenova/multilingual-e5-small"

#: `sentence-transformers` が実際にロードするモデル名(非量子化 fp32、ハッシュ識別子とは別物)。
LOCAL_LOAD_MODEL_NAME = "intfloat/multilingual-e5-small"

LOCAL_DIMENSIONS = 384
LOCAL_BATCH_SIZE = 16
OPENAI_BATCH_SIZE = 100
OPENAI_MAX_ATTEMPTS = 4

#: 子プロセスのモデルロード完了(`"ready"` メッセージ)を待つ上限秒数。
#: 実測ロード時間は約55.07秒(`task-2-report.md`)。負荷の高いマシンでの
#: 遅延や初回のOSキャッシュミスも吸収できるよう、実測値の約3.3倍を確保する。
LOCAL_READY_TIMEOUT_SECONDS = 180.0

#: 1バッチ分の推論応答を待つ上限秒数(モデルロード後、`"ready"` 受信後に適用)。
#: 実測スループット8.84件/秒(=1件あたり約0.113秒)から、既定バッチサイズ16件の
#: 推論は約1.8秒で終わる計算になる。ブラウザやエディタが同居する実行環境での
#: 負荷変動を許容しつつ、応答が返らない子プロセスをタイムリーに検知できるよう、
#: 実測所要時間の約16倍(30秒)を上限とする。
LOCAL_BATCH_TIMEOUT_SECONDS = 30.0

#: 子プロセス異常終了時に内部で再起動・再試行する回数の上限。
#: 1回まで(ゼロから起動し直すコストが約55秒あるため、繰り返しクラッシュする
#: 場合はここで打ち切って呼び出し元へ失敗を伝える——無限ループにしない)。
LOCAL_MAX_RESTART_ATTEMPTS = 1


class EmbeddingProvider(Protocol):
    """埋め込みプロバイダの共通port。

    `LocalEmbeddingProvider`/`OpenAIEmbeddingProvider` が実装する。
    """

    provider: str
    model_identity: str
    dimensions: int
    batch_size: int

    def embed_batch(self, texts: Sequence[str]) -> list[np.ndarray]:
        """`texts` と同じ長さ・同じ順序でベクトルを返す。"""
        ...


# -- ローカルプロバイダ(sentence-transformers、子プロセス常駐) ----------------


def _default_model_loader(model_name: str) -> Any:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


def _local_worker_main(
    model_name: str,
    batch_size: int,
    model_loader: Callable[[str], Any],
    in_queue: Any,
    out_queue: Any,
) -> None:
    """子プロセスのエントリポイント。

    モデルを1回だけロードして常駐し、以後は `in_queue` からバッチを受け取り
    `out_queue` へ結果を返し続ける。`None` を受け取ったら終了する。
    バッチごとにプロセスを起動し直すとモデルロード(実測で約55秒)が
    バッチ回数分かかってしまうため、プロセス自体をワーカーとして使い回す。
    """
    try:
        model = model_loader(model_name)
    except Exception as exc:  # ロード失敗も親プロセスへ伝える
        out_queue.put((-1, "load_error", str(exc)))
        return

    # ロード完了を親プロセスへ通知する。親側はこの合図の受信でロード待ちを終え、
    # 以後のバッチ応答タイムアウト(短い方)へ切り替える。
    out_queue.put((-1, "ready", None))

    while True:
        message = in_queue.get()
        if message is None:
            break
        batch_id, texts = message
        try:
            vectors = model.encode(
                list(texts),
                batch_size=batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            out_queue.put((batch_id, "ok", [np.asarray(v, dtype="<f4") for v in vectors]))
        except Exception as exc:  # 推論failureも親プロセスへ伝える(プロセスは落とさない)
            out_queue.put((batch_id, "error", str(exc)))


class LocalEmbeddingProvider:
    """`sentence-transformers` を子プロセスで実行するローカル埋め込みプロバイダ。

    mean pooling + L2 正規化、384次元(`intfloat/multilingual-e5-small`)。
    `model_identity` は `HASH_MODEL_IDENTITY`(モジュール docstring の二重身分を参照)。
    """

    provider = "local"

    def __init__(
        self,
        *,
        model_identity: str = HASH_MODEL_IDENTITY,
        load_model_name: str = LOCAL_LOAD_MODEL_NAME,
        dimensions: int = LOCAL_DIMENSIONS,
        batch_size: int = LOCAL_BATCH_SIZE,
        model_loader: Callable[[str], Any] = _default_model_loader,
        ready_timeout: float = LOCAL_READY_TIMEOUT_SECONDS,
        batch_timeout: float = LOCAL_BATCH_TIMEOUT_SECONDS,
        max_restart_attempts: int = LOCAL_MAX_RESTART_ATTEMPTS,
    ) -> None:
        self.model_identity = model_identity
        self.load_model_name = load_model_name
        self.dimensions = dimensions
        self.batch_size = batch_size
        self._model_loader = model_loader
        self._ready_timeout = ready_timeout
        self._batch_timeout = batch_timeout
        self._max_restart_attempts = max_restart_attempts
        self._ctx = mp.get_context("spawn")
        self._in_queue: Any = self._ctx.Queue()
        self._out_queue: Any = self._ctx.Queue()
        self._process: Any = None
        self._next_id = 0
        self._closed = False
        self._start_process()

    def _start_process(self) -> None:
        """子プロセスを(再)起動し、モデルロード完了(`"ready"`)まで待つ。

        `embed_batch` からの再起動時にも呼ばれる(子プロセス死亡からの復旧)。
        """
        self._process = self._ctx.Process(
            target=_local_worker_main,
            args=(
                self.load_model_name,
                self.batch_size,
                self._model_loader,
                self._in_queue,
                self._out_queue,
            ),
            daemon=True,
        )
        self._process.start()
        self._wait_ready()

    def _wait_ready(self) -> None:
        try:
            received_id, status, payload = self._out_queue.get(timeout=self._ready_timeout)
        except Empty:
            self._reap()
            raise AppError(
                code=ErrorCode.FAILURE,
                message=(
                    "埋め込みモデルの読み込みがタイムアウトしました"
                    f"(子プロセス、{self._ready_timeout:g}秒)。"
                ),
                retryable=False,
                details={"timeout_seconds": self._ready_timeout},
            ) from None
        if status == "load_error":
            self._reap()
            raise AppError(
                code=ErrorCode.FAILURE,
                message="埋め込みモデルの読み込みに失敗しました(子プロセス)。",
                retryable=False,
                details={"cause_message": str(payload)},
            )
        if status != "ready" or received_id != -1:
            self._reap()
            raise AppError(
                code=ErrorCode.FAILURE,
                message="埋め込み子プロセスの起動応答が不正です。",
                retryable=False,
            )

    def _reap(self) -> None:
        """存命なら停止させ、既に死んでいれば zombie を回収する。

        タイムアウト・異常終了・close() のすべての退出経路から呼ばれ、子プロセスが
        親プロセスの利用終了後も残り続けないようにする。
        """
        process = self._process
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        else:
            process.join(timeout=1)

    def embed_batch(self, texts: Sequence[str]) -> list[np.ndarray]:
        if self._closed:
            raise AppError(
                code=ErrorCode.FAILURE,
                message="既に close() 済みの LocalEmbeddingProvider は使えません。",
            )
        return self._send_and_wait(list(texts), attempt=0)

    def _send_and_wait(self, texts: list[str], *, attempt: int) -> list[np.ndarray]:
        batch_id = self._next_id
        self._next_id += 1
        self._in_queue.put((batch_id, texts))
        try:
            received_id, status, payload = self._out_queue.get(timeout=self._batch_timeout)
        except Empty:
            alive = self._process.is_alive()
            self._reap()
            if alive:
                # 生存中だが応答が無い: 負荷が高いだけの遅いバッチをクラッシュと
                # 誤診しない(brief の要求)。ここでは復旧を試みず、原因を明示して
                # 呼び出し元(run_job)に委ねる——プロセスは既に terminate 済み。
                raise AppError(
                    code=ErrorCode.FAILURE,
                    message=(
                        "埋め込み子プロセスの応答がタイムアウトしました"
                        f"(生存中、{self._batch_timeout:g}秒)。"
                    ),
                    retryable=False,
                    details={"timeout_seconds": self._batch_timeout},
                ) from None
            return self._restart_and_retry(texts, attempt=attempt)
        if received_id != batch_id:
            raise AppError(
                code=ErrorCode.FAILURE,
                message="埋め込み子プロセスの応答順序が不正です。",
            )
        if status == "error":
            raise AppError(
                code=ErrorCode.FAILURE,
                message="埋め込み子プロセスでの推論に失敗しました。",
                details={"cause_message": str(payload)},
            )
        return list(payload)

    def _restart_and_retry(self, texts: list[str], *, attempt: int) -> list[np.ndarray]:
        """子プロセスの死亡を検知した後の復旧(bounded restart)。

        `max_restart_attempts` を超えたら、繰り返しクラッシュする子プロセスを
        黙って無限リトライせず、明確な `AppError` として呼び出し元へ伝える。
        """
        if attempt >= self._max_restart_attempts:
            raise AppError(
                code=ErrorCode.FAILURE,
                message="埋め込み子プロセスが異常終了しました(再起動上限に到達)。",
                retryable=False,
                details={"restart_attempts": attempt},
            )
        self._start_process()
        return self._send_and_wait(texts, attempt=attempt + 1)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process is not None and self._process.is_alive():
            self._in_queue.put(None)
            self._process.join(timeout=10)
        self._reap()

    def __enter__(self) -> LocalEmbeddingProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# -- OpenAI プロバイダ(429/5xx のみ指数バックオフ再試行) ----------------------


class OpenAIEmbeddingProvider:
    """OpenAI Embeddings API を叩くプロバイダ(同じ `EmbeddingProvider` port の裏側)。

    429・5xx のみ再試行する(brief: 「429/5xx のみ指数バックオフで再試行
    (`2**attempt` 秒、最大4回)」)。それ以外のHTTPエラー(4xx)は即座に送出する
    ——リトライしても直らない呼び出し側の誤りだから。
    """

    provider = "openai"

    def __init__(
        self,
        *,
        model_identity: str,
        dimensions: int,
        api_key: str,
        batch_size: int = OPENAI_BATCH_SIZE,
        base_url: str = "https://api.openai.com/v1",
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = OPENAI_MAX_ATTEMPTS,
    ) -> None:
        import httpx

        self.model_identity = model_identity
        self.dimensions = dimensions
        self.batch_size = batch_size
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=60.0)
        self._sleep = sleep
        self._max_attempts = max_attempts

    def embed_batch(self, texts: Sequence[str]) -> list[np.ndarray]:
        last_exc: Exception | None = None
        last_status: int | None = None

        for attempt in range(self._max_attempts):
            try:
                response = self._client.post(
                    f"{self._base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self.model_identity, "input": list(texts)},
                )
            except Exception as exc:  # httpx.HTTPError系(接続断など): 再試行対象
                last_exc = exc
                if attempt + 1 >= self._max_attempts:
                    break
                self._sleep(2**attempt)
                continue

            if response.status_code == 200:
                payload = response.json()
                ordered = sorted(payload["data"], key=lambda item: item["index"])
                return [np.asarray(item["embedding"], dtype="<f4") for item in ordered]

            last_status = response.status_code
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt + 1 >= self._max_attempts:
                raise AppError(
                    code=ErrorCode.EXTERNAL_SERVICE,
                    message=(
                        f"OpenAI 埋め込みAPIがエラーを返しました(status={response.status_code})。"
                    ),
                    retryable=retryable,
                    exit_code=ExitCode.EXTERNAL_SERVICE,
                    details={"status_code": response.status_code},
                )
            self._sleep(2**attempt)

        raise AppError(
            code=ErrorCode.EXTERNAL_SERVICE,
            message="OpenAI 埋め込みAPIへの接続に失敗しました(再試行上限到達)。",
            retryable=True,
            exit_code=ExitCode.EXTERNAL_SERVICE,
            details={
                "cause_message": str(last_exc) if last_exc is not None else None,
                "status_code": last_status,
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenAIEmbeddingProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# -- 差分生成(brief Step3〜4) -------------------------------------------------


@dataclass(slots=True)
class GenerationSummary:
    """1回の `generate_embeddings` 呼び出しの集計。"""

    scanned: int = 0
    generated: int = 0
    skipped: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)


def generate_embeddings(
    conn: sqlite3.Connection,
    provider: EmbeddingProvider,
    *,
    corpus: str,
    emit: EmitFn | None = None,
    check_lease: CheckLeaseFn | None = None,
) -> GenerationSummary:
    """`chunks` を `embeddings` と突き合わせ、`model`/`input_hash` が一致しない行だけ
    埋め込みを生成する。

    `corpus="reference"` は何もしない(brief Step4、旧 `reference-index.sqlite` は
    埋め込み0件)。`corpus="work"` のみが実際に生成する。

    候補チャンクは `.fetchall()` せず `sqlite3` カーソルを直接イテレートして
    ストリーミング処理する(`provider.batch_size` 件たまるたびに1トランザクションで
    書き込み、次のバッチへ進む)。バッチ単位で即座にコミットするため、途中で
    中断しても未処理分だけが次回スキップされずに残る(再開可能)。

    副作用のあるループ(バッチ書き込み)の直前で `check_lease` を呼ぶ
    (`JobRunContext.check_lease` の契約、`indexer.py`/`git.py` と同じ規約)。
    """
    summary = GenerationSummary()

    if corpus == "reference":
        if emit is not None:
            emit(
                phase="embed",
                message="reference コーパスは埋め込みを生成しません(FTSのみ、設計どおり)。",
            )
        return summary
    if corpus != "work":
        raise ValueError(f"未知の corpus です: {corpus!r}('work' または 'reference' を指定)。")

    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    cursor = conn.execute(
        "SELECT c.id AS chunk_id, c.title AS title, c.heading_path AS heading_path, "
        "c.text AS text, e.model AS existing_model, e.input_hash AS existing_input_hash "
        "FROM chunks c LEFT JOIN embeddings e ON e.chunk_id = c.id "
        "ORDER BY c.id"
    )

    pending: list[tuple[int, str, str]] = []

    def flush() -> None:
        if not pending:
            return
        if check_lease is not None:
            check_lease()
        texts = [item[1] for item in pending]
        vectors = provider.embed_batch(texts)
        if len(vectors) != len(pending):
            raise AppError(
                code=ErrorCode.FAILURE,
                message="埋め込みプロバイダの戻り件数がバッチ件数と一致しません。",
                details={"expected": len(pending), "actual": len(vectors)},
            )
        now = datetime.now(UTC).isoformat()
        with transaction(conn):
            for (chunk_id, _text, hash_value), vector in zip(pending, vectors, strict=True):
                conn.execute(
                    "INSERT INTO embeddings "
                    "(chunk_id, vector, model, dimensions, input_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (chunk_id) DO UPDATE SET "
                    "vector = excluded.vector, model = excluded.model, "
                    "dimensions = excluded.dimensions, input_hash = excluded.input_hash, "
                    "created_at = excluded.created_at",
                    (
                        chunk_id,
                        np.asarray(vector, dtype="<f4").tobytes(),
                        provider.model_identity,
                        provider.dimensions,
                        hash_value,
                        now,
                    ),
                )
        summary.generated += len(pending)
        if emit is not None:
            emit(
                phase="embed",
                current=summary.scanned,
                total=total,
                message=f"{len(pending)}件の埋め込みを生成しました",
            )
        pending.clear()

    for row in cursor:
        summary.scanned += 1
        chunk = EmbeddingInputChunk(
            text=row["text"], title=row["title"], heading_path=row["heading_path"]
        )
        text_input = embedding_input(chunk, provider.model_identity)
        hash_value = input_hash(provider.model_identity, text_input)

        already_current = (
            row["existing_model"] == provider.model_identity
            and row["existing_input_hash"] == hash_value
        )
        if already_current:
            summary.skipped += 1
            continue

        pending.append((row["chunk_id"], text_input, hash_value))
        if len(pending) >= provider.batch_size:
            flush()

    flush()
    _write_meta(conn, provider)
    return summary


def _write_meta(conn: sqlite3.Connection, provider: EmbeddingProvider) -> None:
    """`meta` へ `embedding_model`/`embedding_provider`/`embedding_updated_at` を書く。

    brief Step5。
    """
    now = datetime.now(UTC).isoformat()
    values = {
        "embedding_model": provider.model_identity,
        "embedding_provider": provider.provider,
        "embedding_updated_at": now,
    }
    with transaction(conn):
        for key, value in values.items():
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


__all__ = [
    "HASH_MODEL_IDENTITY",
    "LOCAL_BATCH_SIZE",
    "LOCAL_BATCH_TIMEOUT_SECONDS",
    "LOCAL_DIMENSIONS",
    "LOCAL_LOAD_MODEL_NAME",
    "LOCAL_MAX_RESTART_ATTEMPTS",
    "LOCAL_READY_TIMEOUT_SECONDS",
    "OPENAI_BATCH_SIZE",
    "OPENAI_MAX_ATTEMPTS",
    "EmbeddingProvider",
    "GenerationSummary",
    "LocalEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "generate_embeddings",
]
