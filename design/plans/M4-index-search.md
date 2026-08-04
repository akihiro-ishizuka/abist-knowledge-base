# M4 索引・埋め込み・ハイブリッド検索 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development でタスク単位に実行する。ステップは `- [ ]` で進捗管理。

**Goal:** Markdown から FTS5 索引とベクトル索引を構築し、キーワード検索・2文字和語補助・ベクトル検索を RRF で統合するハイブリッド検索を実装する。**M1 で採取した検索ベースライン(hybrid Recall@5 = 0.9545)に対し低下 0.01 以内**をもって完了とする。

**Architecture:** M2 のカーネル(`chunker`・`e5_input`・`b32doc_filter`)を入力生成に使い、work / reference の2つの独立した SQLite 索引を構築する。検索は設定されたトークナイザ1つ(既定 trigram)で FTS を引き、2文字和語の LIKE 補助とベクトル検索を加えて RRF で統合する。

**Tech Stack:** Python 3.12 / SQLite FTS5(unicode61 + trigram)/ NumPy / sentence-transformers / OpenAI SDK(任意)

## Context

親計画: `C:\Users\a1199118\.claude\plans\design-system-design-md-push-glowing-tulip.md`。設計の正は [../system-design.md](../system-design.md) §4(FTS5 方針)、§9.2(索引テーブル)、§11.2(埋め込み再利用ゲート)、§13.1。

**M1 の採取物(着手前に `tests/fixtures/PROVENANCE.md` を読むこと):**
- `tests/fixtures/eval/baseline.json` — 22クエリ × 2方式のランクと指標。**これが M4 の合否を決める。** bm25_raw: Recall@5 0.7955 / MRR 0.7323 / nDCG@10 0.7627。hybrid: 0.9545 / 0.8788 / 0.8779。
- `tests/fixtures/eval/queries.jsonl` — 22クエリ(SHA 記録済み)。
- `tests/fixtures/embedding/gate-samples.json` — 100件。`title`/`heading_path`/`text` の入力と、旧版が生成した `input_hash`・`vector_b64`(Float32 LE)・`model`・`dimensions`。**M2 で入力と `input_hash` の 100/100 一致は実証済み**なので、残るのはベクトル値の比較(§11.2 のゲート)だけ。
- `tests/fixtures/kernel/b32doc-filter.json` — 参照コーパス選別の46ケース。M2 で実装済み。
- MCP 契約 fixture の `kb-search` 4ツール分 — M5 で使うが、`search_kb` の応答形状は M4 の `SearchService` が生成するので、**M4 の時点で形を合わせておく**と M5 が楽になる。

**旧システムの実態(設計書 §9.2 の前提を覆す。PROVENANCE.md §4):**
- 参照コーパスは `reference-index.sqlite` にあり、**`knowledge/B32doc` のみ**(7,563文書・35,398チャンク・**埋め込み 0 件**=FTS のみ)。
- `docs/knowledge/catiadoc`(1,313ファイル)と `docs/knowledge/generated` はどちらの DB にも登録されていない。**索引対象の選定を `sync-state` の DB だけに頼ってはならない。**
- work 索引(`kb-index.sqlite`)は 3,469文書・51,391チャンク・**51,411埋め込み**。差の20件は旧版のバグ(再索引時にチャンクを削除しても埋め込みが残る)。**移植版では同一トランザクションで削除する。**

**M2 で確立済み、そのまま使う:**
- `infrastructure/search/chunker.py` — 見出しベース分割。実データ40件と合成24ケースでビット一致。
- `infrastructure/search/e5_input.py` — 埋め込み入力構築。**ハッシュ用モデル識別子は `Xenova/multilingual-e5-small`、ローダ用は `intfloat/multilingual-e5-small`。この2つは別物として共存させる**(`input_hash` が文字列をハッシュに畳み込むため)。UTF-16 単位での切詰と孤立サロゲート → U+FFFD も実装済み。
- `domain/{frontmatter,line_range,metadata_schema,b32doc_filter}.py`。

**M3 で確立済み:**
- 永続ジョブ基盤とリース。**索引更新は `corpus-write:<corpus>` リースの下で実行する。** 側効果のあるループでは `run.check_lease()` を呼ぶ(リースを失った後も書き続ける事故が実証済み)。
- `documents` テーブル(29列)、`SyncService`、`AppTyper`、`Presenter`。

## Global Constraints(全タスク共通)

- **旧リポジトリ `C:\Temp\multi-source-knowledge-base` には書き込まない。** SQLite を開くときは**サンドボックスコピー方式のみ**(`readonly:true` でも `-shm` の mtime が動く)。
- 索引 DB は work / reference の2ファイルに分ける。スキーマは旧版互換(列名・型・索引)。**FTS5 は unicode61 と trigram の2テーブルを両方構築**し、実行時は設定された1つ(既定 trigram)を使う。
- FTS5 は external content(`content='chunks', content_rowid='id'`)。投入は `'rebuild'` コマンドで行う(トリガは使わない)。
- 埋め込みベクトルは L2 正規化済み Float32 リトルエンディアンの BLOB。`np.asarray(v, dtype='<f4').tobytes()`。
- 埋め込み生成は再開可能・差分のみ。`input_hash` と `model` が一致するものはスキップ。**全チャンクをメモリに載せない**(旧版は載せていた。51,391件ある)。
- CPU 負荷の高い埋め込み生成は子プロセスで実行する(§10.3)。
- `doctor` が FTS5・unicode61・trigram・external content・bm25 を実測済み(M0)。索引構築前にこれらを前提にしてよい。
- ruff・format クリーン、`uv run pytest -q` 全通過を各タスクのコミット条件とする。

## ファイル構成

```
src/abist_kb/
  infrastructure/search/
    index_schema.py       # T1: 索引DBスキーマ・FTS5テーブル
    indexer.py            # T1: 索引構築・差分更新・canonical_path
    corpus.py             # T1: work/reference の選定
    query_terms.py        # T3: クエリ語抽出
    search_engine.py      # T3: FTS + LIKE補助 + ベクトル + RRF + boost + dedup
    metrics.py            # T5: recall@k / MRR / nDCG / fold-to-document
  infrastructure/ai/
    embedding_provider.py # T2: sentence-transformers / OpenAI
  application/
    index_service.py      # T4
    search_service.py     # T4
    audit/search_quality.py # T5
  presentation/cli/
    index_cmd.py search_cmd.py  # T4
tests/search/
```

---

## Task 1: 索引スキーマと indexer

**Files:** `infrastructure/search/{index_schema,indexer,corpus}.py`, `tests/search/test_indexer.py`

**Interfaces:**
- Produces: `create_index_schema(conn, tokenizers)` — documents/chunks/embeddings/meta + `chunks_fts_unicode61` + `chunks_fts_trigram`
- Produces: `IndexBuilder.build(rows, docs_dir, *, emit, check_lease) -> IndexSummary`
- Produces: `select_work_targets(...)`, `select_reference_targets(...)`
- Produces: `resolve_canonical_paths(rows) -> dict[str, str]`

- [ ] **Step 1: スキーマのテストを書く(赤)**

旧版の列を正確に再現する。`documents`(path PK, post_number, title, source, document_type, status, url, category, document_hash NOT NULL, chunk_count, indexed_at, canonical_path)、`chunks`(id INTEGER PRIMARY KEY, path, chunk_index, title, heading_path, text, start_line, end_line, content_hash, token_estimate, UNIQUE(path, chunk_index))、`embeddings`(chunk_id PK, vector BLOB, model, dimensions, input_hash, created_at)、`meta`(key PK, value)。

FTS5 の2テーブルを external content で作り、`'rebuild'` で投入できること、`bm25()` が使えること、trigram で3文字以上がマッチすることを検証する。

- [ ] **Step 2: 実装し緑を確認**

- [ ] **Step 3: 差分索引と孤児埋め込みの修正**

`document_hash`(= `hash_body`)が一致すれば再チャンクしない(`canonical_path` だけ更新)。変更時はそのパスのチャンクを削除して再挿入するが、**同一トランザクションで該当 `chunk_id` の埋め込みも削除する**。旧版はこれを怠り 20件の孤児を残していた。孤児が残らないことをテストで固定する。

`rows` に無くなった文書は documents/chunks/embeddings ごと削除する。

- [ ] **Step 4: canonical_path**

`post_number` でグループ化し(NULL はスキップ)、**辞書順で最小のパス**を代表とする。決定性のための規則であって品質判断ではない。未変更の高速パスでも `canonical_path` は更新する。

- [ ] **Step 5: コーパス選定**

- **work**: `documents` から `source ∈ {esa, web, git, manual}` かつ `document_type != reference`。ただし **DB を完全な台帳と仮定しない**(PROVENANCE.md §4)。`docs/` の実走査と DB の突合を行い、DB に無いが `docs/` にある文書を検出して報告する(黙って落とさない)。
- **reference**: `docs/knowledge/B32doc` をファイル走査し、M2 の `b32doc_filter` で選別する。除外理由を集計して報告する。タイトルは `## Summary Keys` の `- タイトル:` 行から取る(ファイル名ではない)。

- [ ] **Step 6: `meta` への記録**

`schema_version`, `tokenizers`(JSON配列), `last_indexed_at`, `chunk_options`(JSON), `embedding_model`, `embedding_provider`, `embedding_updated_at`。

- [ ] **Step 7: コミット**

---

## Task 2: 埋め込みプロバイダ

**Files:** `infrastructure/ai/embedding_provider.py`, `tests/search/test_embedding.py`

- [ ] **Step 1: スループット spike(実装前)**

`sentence-transformers` で `intfloat/multilingual-e5-small` を読み、100件を埋め込んで実測する。ロードマップの想定は約17件/秒・全体1時間。実測値を記録し、大きく外れるならバッチサイズや `torch` の設定を調整する。**この数字は M8 の再生成計画の根拠になる。**

- [ ] **Step 2: §11.2 の埋め込み再利用ゲートを測る**

`tests/fixtures/embedding/gate-samples.json` の100件について、Python で埋め込みを生成し、記録されている旧版ベクトル(`vector_b64`)とのコサイン類似度を計算する。

**判定**: 全100件で 0.999 以上なら再利用可。1件でも下回れば再生成が必要。

**不合格が既定の想定である。** 旧版は Xenova の int8 量子化 ONNX、こちらは fp32 の PyTorch なので、ビット一致はしない。**不合格それ自体は失敗ではない**——設計書 §11.2 が再生成を既定経路として定めている。結果(全件の類似度分布・最小値・不合格件数)を記録し、M8 の判断材料にする。

- [ ] **Step 3: プロバイダ実装**

`intfloat/multilingual-e5-small`(mean pooling + L2 正規化、384次元)。**入力構築は M2 の `e5_input` を使う**(ハッシュ用識別子は `Xenova/...` のまま)。BLOB は `np.asarray(v, dtype='<f4').tobytes()`。

差分生成: `chunks` を `LEFT JOIN embeddings` して `input_hash` と `model` が一致するものをスキップ。**ストリーミングで処理し、全件をメモリに載せない。**

バッチサイズは local 16 / OpenAI 100。OpenAI は 429/5xx のみ指数バックオフで再試行(`2**attempt` 秒、最大4回)。

CPU 負荷が高いので子プロセスで実行する。中断・再開が効くこと。

- [ ] **Step 4: 参照コーパスは埋め込みを作らない**

旧版の `reference-index.sqlite` は埋め込み 0 件。同じにする(FTS のみ)。

- [ ] **Step 5: コミット**

---

## Task 3: クエリ語抽出と検索エンジン

**Files:** `infrastructure/search/{query_terms,search_engine}.py`, `tests/search/test_search_engine.py`

- [ ] **Step 1: クエリ語抽出のテストを書く(赤)**

旧 `test/search-engine.test.js` の17ケースを移植する(M1 では fixture 化していない。実行で確認しながら書く)。抽出順序:
1. `#\d+`
2. ドット/アンダースコア連結識別子 `[A-Za-z][A-Za-z0-9_]*(?:[._][A-Za-z0-9_]+)+`
3. 英数語 `[A-Za-z][A-Za-z0-9]*`
4. 漢字2文字以上 `[一-鿿]{2,}`
5. カタカナ2文字以上 `[ァ-ヶー]{2,}`
6. `[φΦ]\s*[\d.]+`(内部空白を除去)

重複は順序を保って除去。長さ1は捨てる。ストップワード15語(`どう, どこ, なぜ, いつ, 場合, 方法, 手段, 内容, 対応, よい, ある, する, いる, こと, もの, ため`)を除く。**ひらがなは抽出しない。**

**Python の `\d` は Unicode 全角も拾う。JS は ASCII のみ。`[0-9]` を使うこと**(M2 で同じ罠を踏んでいる)。

- [ ] **Step 2: 3つのランキングを実装**

**keyword**: trigram のときは3文字以上の語だけを使う(trigram は2文字以下をインデックスできない)。各語を `"` で囲む(内部の `"` は二重化)。` OR ` で連結。使える語が無ければ生クエリを引用符で囲む。`bm25(<table>, 1.0, 2.0, 3.0)`(text 1.0 / heading_path 2.0 / title 3.0)で昇順(SQLite の bm25 は負値なので昇順が良い順)。`LIMIT candidate_limit`。

**short_term**: 2文字の漢字/カタカナ語だけを対象に LIKE 検索。該当語が無ければ空を返す(純 ASCII クエリで全表走査しないこと)。`term_hits DESC, length(text) ASC` で並べる。**これは trigram が2文字を扱えない穴を埋めるためにある**——蛇腹・要件・図面は業務語彙の中核なので、無いと実用にならない。

**vector**: `is_pure_identifier_query()` が真なら**丸ごとスキップする**。旧版の計測で、識別子クエリに混ぜると Recall@5 が 1.000 → 0.917 に落ちた。それ以外は NumPy の行列(プロセス寿命でキャッシュ、`embeddings` の件数変化で再構築)との内積。埋め込みが無い、または次元が合わなければ空を返し、FTS のみで動作する。

- [ ] **Step 3: RRF・ブースト・重複排除**

RRF: `score[chunk_id] += 1 / (k + index + 1)`(k=60、index は0始まり)。3つのランキングは等重み。空のランキングは含めない。

ブースト(RRF スコアに**加算**。RRF は最大でも約0.016なので、0.08 のブーストは支配的):
- `title == query.lower()` または `heading_path.endswith(query)` → +0.05
- そうでなく `title` に query を含む → +0.025
- 最初に見つかった識別子語が `text` か `title` にある → +0.08(1つ見つけたら打ち切り)
- `status == 'active'` → +0.01

重複排除の順序: (1) 同一 `content_hash` を落とす → (2) `post:<post_number>`、無ければ `path:<canonical_path or path>` でグループ化 → (3) 1文書あたり最大2チャンク。落とした側のパスは残った側の `other_paths` に集約する。最後に `limit` で切る。

- [ ] **Step 4: 結果とフィルタ**

結果1件は `path, title, heading_path, start_line, end_line, snippet, url, status, source, document_type, post_number, document_hash, indexed_at, index_stale, score(6桁), matched_by[], boosts[], other_paths[], chunk_id`。

`snippet`: クエリ語の最初の出現位置を探し、`max(0, pos - maxLength//3)` から240文字。空白を圧縮し、切り詰めた側に `…` を付ける。

`index_stale`: `docs/<path>` を読み直して `hash_body` を `document_hash` と比較。読めなければ真。**出典行が信用できるかの指標**なので必ず出す。

フィルタ: `source`, `document_type`, `status`, `path_prefix`(末尾 `/` を除去して LIKE)。3つのランキングすべてに同じ条件を適用する。

診断: `query, terms, tokenizer, keyword_candidates, short_term_candidates, vector_candidates, identifier_only_query, vector_available, elapsed_ms`。

- [ ] **Step 5: コミット**

---

## Task 4: IndexService / SearchService と CLI

**Files:** `application/{index_service,search_service}.py`, `presentation/cli/{index_cmd,search_cmd}.py`, `tests/search/test_services.py`

- [ ] **Step 1: サービス層**

`IndexService.build(corpus)` / `.embed(corpus)` / `.status()`。すべて `corpus-write:<corpus>` リースの下で、永続ジョブとして実行する。ループでは `check_lease()` を呼ぶ。

`SearchService.search(query, **filters)` — 応答形状は **M5 の MCP `search_kb` fixture に合わせる**(`tests/fixtures/mcp/kb-search/search_kb/*.json` を読んで確認する)。ここで合わせておけば M5 は薄い変換で済む。

索引鮮度判定: `documents.indexed_at` と実ファイルの `hash_body` を比較。

- [ ] **Step 2: CLI**

`index build|embed|status`、`search <query>`(フィルタ: `--source --document-type --status --path-prefix --corpus --limit`)。出典行を常時表示。`--output json` 純度を守る。

- [ ] **Step 3: コミット**

---

## Task 5: 検索評価と M4 ゲート

**Files:** `infrastructure/search/metrics.py`, `application/audit/search_quality.py`, `tests/search/test_eval.py`

- [ ] **Step 1: 指標を1箇所に集約**

`recall_at_k`, `reciprocal_rank`, `ndcg_at_k`, `fold_to_documents`。**旧版はこれを3ファイルに重複させていた**(`eval-search.js`・`compare-tokenizers.js`・その他)。1実装にする。

`fold_to_documents`: `post:<n>`、無ければ `path:<p>` でグループ化して文書単位に畳む。**スコアリングは畳んだ後に行う**——畳まないと1文書2チャンクの hybrid で nDCG が1を超える。

- [ ] **Step 2: 評価コマンド**

`audit search-quality` — `tests/fixtures/eval/queries.jsonl` の22クエリを `hybrid` と `bm25_raw` で実行し、Recall@5 / MRR / nDCG@10 のマクロ平均を出す。前回結果と比較して悪化(-0.01 超)を警告する。`reports/eval/eval-<ts>.json` へ出力。

`index compare-tokenizers` — unicode61 と trigram を同条件で比較し、既定値変更の可否を判断する材料を出す。

- [ ] **Step 3: M4 ゲートを測る**

実 `docs/` のスナップショット(**コピーして使う。旧 `docs/` には書き込まない**)から索引を再構築し、埋め込みを生成し、22クエリを評価する。

**合格条件:**
- hybrid の Recall@5 が **0.9545 - 0.01 = 0.9445 以上**
- bm25_raw の Recall@5 が **0.7955 - 0.01 = 0.7855 以上**
- 出典行一致率 95% 以上(ベースラインの各結果の `start_line`/`end_line` と比較)
- FTS の2テーブルが構築され、実行時に trigram が既定で使われる

不合格なら、どのクエリでどう落ちたかをベースラインと突き合わせて原因を特定する。**閾値を緩めない。** チャンク境界・クエリ語抽出・RRF・ブーストのどれかが違うはずで、M1 のベースラインには per-query のランクが記録されているので追跡できる。

- [ ] **Step 4: コミット**

---

## Verification(M4 ゲート)

1. 22クエリ評価で hybrid Recall@5 ≥ 0.9445、bm25_raw ≥ 0.7855。
2. 出典行一致率 ≥ 95%。

### 全チャンク(52,234件)投入後の再測定結果

M4完了後、52,234チャンク全件の埋め込みが揃った時点で条件1・2を再測定した。

- **条件1(Recall@5)は合格、かつ採取済みベースラインとビット同一。** bm25_raw
  0.7955、hybrid 0.9545 —— いずれも `tests/fixtures/eval/baseline.json` の値と
  完全一致(小数点以下も含め差分ゼロ)。
- **条件2(出典行一致率 ≥95%)は不合格。** bm25_raw 96.9%(合格)、hybrid
  77.6%(不合格)、combined 86.1%(不合格、閾値95%に対し-8.9pt)。**閾値は
  変更していない。**

50件の不一致を内訳分析した結果、3.6%サブセットでの初期調査時の診断
(「hybridの行不一致の88%はvectorが関与する結果に起因する」)は、全コーパス
規模では成り立たない形に修正が必要であることが判明した:

| 内訳 | 件数 | 割合 | 説明 |
|---|---|---|---|
| `matched_by` に `vector` を含む | 37件 | 74% | 既知の埋め込み差異(§埋め込み再利用ゲート不合格)に起因すると推定される範囲。サブセット調査の診断と方向性は一致する |
| q19/q22のBM25タイブレークパターン | 5件 | 10% | サブセット調査時から不変。ほぼ重複した日付違い文書間のタイブレーク非決定性であり、原因は特定済み・許容している |
| **フルコーパス規模で新たに出現、hybrid限定、keyword/short_term起因(vector不関与)** | **6件** | **12%** | **未解明。埋め込み差異でもタイブレークでも説明できない残差** |

この最後の6件が、この再測定で最も正直に扱うべき事実である。埋め込み差異
という既知の説明では説明できず、タイブレークという既知のパターンとも
一致しない。**仮説(未検証)**: フルコーパスを検索するとRRFが折り込む
keyword候補プールが(3.6%サブセットの60件規模ではなく)遥かに広がり、
埋め込みの差異とは独立に上位10件へ到達する文書の顔ぶれが変わっている
のではないか、というもの。これは仮説であり結論ではない。

**結論**: 設計が最初に挙げ最も重く扱う基準であるRecall@5は全チャンク規模で
ビット同一のまま合格している一方、出典行一致率は不合格であり、そのうち
6件は既知の要因(埋め込み差異・タイブレーク)のいずれにも帰着しない残差
として未解明のまま残っている。§15の検索品質受入条件(`design/plans/M6-M10-remaining.md`
Task 9.4)は、この状態を踏まえて判断すること。
3. FTS5 の unicode61 / trigram 両テーブルが構築され、実行時 trigram が既定。
4. 再索引後に孤児埋め込みが 0 件(旧版の 20件バグを再現しない)。
5. 埋め込み再利用ゲート(§11.2)の測定結果が記録されている(合否いずれでも可。不合格なら再生成が既定経路)。
6. 参照コーパスは B32doc のみ・埋め込み 0 件。
7. 索引更新が `corpus-write` リース下で実行され、リース喪失時に書き込みが止まる。
8. `--output json` 純度。
9. Windows・Linux 両 CI で緑。

## 次のマイルストーン

M4 完了後は **M5(互換 MCP サーバー kb-download / kb-search)**。M1 の契約 fixture(15ツール28ケース)をリプレイして合否を決める。`sdk_validation_error` の散文は zod/SDK バージョン固有なので、比較は `response_kind` + `isError` + `-32602` で行う(`tests/fixtures/PROVENANCE.md` 参照)。
