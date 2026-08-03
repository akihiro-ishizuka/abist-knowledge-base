# M1 fixture 採取 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development でタスク単位に実行する。ステップは `- [ ]` で進捗管理。

**Goal:** 現行 Node.js 版 `multi-source-knowledge-base` が無傷なうちに、Python 移植の正解データ(ゴールデン fixture)を機械的に採取し、新リポジトリへ保存する。以降の M2〜M9 はこの fixture との一致をもって「移植できた」と判定する。

**Architecture:** 期待値を人手で書かない。**入力は旧テストのインライン定数と実データから採り、期待出力は旧 Node コードを実行して得る。** 採取スクリプトは新リポジトリの `tests/fixtures/capture/` に置き、旧リポジトリを read-only で参照する。入力バイト列は base64 で保存し、BOM・CRLF・日本語を git や エディタに壊されないようにする。

**Tech Stack:** Node.js v22.18.0(旧システムの ESM ライブラリを直接 import)/ Python 3.12(MCP stdio クライアント・検証)/ JSON + base64

## Context

親計画: `C:\Users\a1199118\.claude\plans\design-system-design-md-push-glowing-tulip.md`。設計の正は [../system-design.md](../system-design.md)、特に §7.4.1(MCP 互換性の3層定義と契約テスト)と §11.2(埋め込み再利用ゲート)。

**なぜ今やるのか。** ゴールデン値は動いている Node コードを実行して初めて得られる。ソースが変わった時点で同等性を証明する手段が永久に失われ、コードを読み直しても再構築できない。したがって M1 は旧システムに一切手を入れる前に完了させる。M0 完了後の最優先タスクである。

**M0 で確立済みの前提**(そのまま使う):
- `src/abist_kb/` レイアウト、`identity.py`、`Settings`、`AppError`/`ExitCode`、Console(`--output` 各モード)、SQLite 接続・マイグレーション、秘密マスク付きロギング、`abist-kb` CLI と `doctor`。
- テスト基盤: pytest + Hypothesis、`tests/conftest.py` の `tmp_root`(日本語ディレクトリ名)。

**旧システムの構造**(調査済み、採取対象):

| 領域 | ファイル | 採取する関数 |
|---|---|---|
| front matter | `tools/lib/frontmatter.js` | `parseFrontmatter`, `setFrontmatterValues`, `bodyOf`, `hashBody`, `sha256` |
| チャンク分割 | `tools/lib/chunker.js` | `chunkMarkdown`, `DEFAULT_CHUNK_OPTIONS`, `estimateTokens` |
| 行範囲ハッシュ | `tools/lib/line-range.js` | `rangeHash` |
| 埋め込み入力 | `tools/lib/embeddings.js` | `embeddingInput`, `queryInput`, `EMBEDDING_MODELS`, `inputHash` 相当 |
| 同期判定 | `tools/lib/sync-planner.js` | `decideSyncAction`, `decideMissingCandidate`, `SYNC_ACTIONS`, `ACTION_BUCKET` |
| メタデータ | `tools/lib/metadata-schema.js` | `classifyDocument`, `REFERENCE_CORPUS_PREFIXES`, `FRONTMATTER_KEY_ORDER`, `safeBatchName`, sanitize 系 |
| バッチ設定 | `tools/lib/batch-config-store.js` | `formatConfig`, `loadBatchConfigsFresh` |
| MCP | `.mcp.json` → `tools/kb-{download,search,visualize}-mcp.js` | 15ツール |
| 検索評価 | `tools/eval-search.js`, `eval/queries.jsonl` | 22クエリのランクと指標 |
| 索引 | `data/kb-index.sqlite` | chunks / embeddings(層化100件) |

旧テストは `test/*.js` 28ファイル。**fixture ディレクトリは存在せず、すべてインライン定数**なので、転記が必要。

## Global Constraints(全タスク共通)

- **移行元 `C:\Temp\multi-source-knowledge-base` は絶対に変更しない。** 採取スクリプトは読み取りのみ。DB は read-only で開く。一時ファイルは新リポジトリ側か OS の一時領域に作る。
- 期待値を手書きしない。旧コードの実行結果のみを期待値とする。定数の転記ミスを防ぐため、転記した入力は必ず旧テストの該当行を引用コメントで併記する。
- 入力バイト列は base64 で保存。`tests/fixtures/` に `.gitattributes` で `* -text` を設定し、改行変換を禁止する。
- 採取スクリプトは冪等。2回実行してバイト同一の JSON を出すこと(時刻・所要時間など非決定値を出力へ入れない)。
- MCP 応答は**生の JSON-RPC をそのまま保存**する。正規化(時刻・所要時間・一時パス)は比較側で行い、fixture には手を入れない。
- 秘密情報を fixture に含めない。採取前に `.env` の値がレスポンスへ混ざらないことを確認し、混ざる場合はそのケースを除外して記録する。
- Node は v22.18.0 を使う。旧リポジトリの `node_modules` をそのまま利用する(`npm install` を実行しない=ロックファイルを変更しない)。
- 採取結果には必ず `capture-manifest.json` を伴わせ、採取日時・Node 版数・旧リポジトリの `git rev-parse HEAD`(取得できる場合)・各 fixture のファイル数と SHA-256 を記録する。
- TDD の対象は「採取スクリプトが期待どおりの構造を出すこと」。fixture そのものは検証対象ではなく**正解**である。

## ファイル構成

```
tests/fixtures/
  .gitattributes                        # * -text(改行変換禁止)
  capture-manifest.json                 # 採取メタデータ(T5 で生成)
  capture/
    _shared.mjs                         # 旧リポジトリ解決・base64・決定的JSON書き出し
    capture-kernel.mjs                  # T1: frontmatter/chunker/line-range/e5入力/sync判定/metadata
    capture-batch-config.mjs            # T1: formatConfig と実 batch-config.js のパース結果
    capture-real-docs.mjs               # T2: 実 docs/ の層化サンプル
    capture-mcp.mjs                     # T3: 3サーバー×15ツールの生 JSON-RPC
    capture-eval.mjs                    # T4: 22クエリのランクと指標
    capture-embeddings.mjs              # T5: 層化100チャンクのベクトルと input_hash
    capture-html.mjs                    # T6: turndown の HTML→MD 出力
    README.md                           # 再採取手順と前提
  kernel/
    frontmatter.json  chunker.json  line-range.json  e5-input.json
    sync-planner.json  metadata-schema.json  batch-config.json
  real-docs/
    samples.json                        # 層化サンプル(入力 base64 + 全カーネル出力)
  mcp/
    kb-download/<tool>/<case>.json
    kb-search/<tool>/<case>.json
    kb-visualize/<tool>/<case>.json
    tools-list.json                     # 3サーバーの tools/list スナップショット
  eval/
    baseline.json
  embedding/
    gate-samples.json
  html/
    turndown-goldens.json
tests/fixtures_check/                   # 採取物の自己検証テスト(pytest)
  test_fixture_integrity.py
  test_mcp_fixture_coverage.py
```

---

## Task 1: 共通基盤とカーネルゴールデン採取

**Files:**
- Create: `tests/fixtures/.gitattributes`, `tests/fixtures/capture/_shared.mjs`, `tests/fixtures/capture/capture-kernel.mjs`, `tests/fixtures/capture/capture-batch-config.mjs`, `tests/fixtures/capture/README.md`
- Generate: `tests/fixtures/kernel/*.json`
- Test: `tests/fixtures_check/test_fixture_integrity.py`

**Interfaces:**
- Produces: `_shared.mjs` exports `oldRepoRoot()`(既定 `C:\Temp\multi-source-knowledge-base`、`KB_OLD_REPO` で上書き可)、`b64(str)`、`writeDeterministicJson(path, obj)`(キーをソートし LF 固定・末尾改行あり)、`assertReadOnly()`(旧リポジトリへ書き込まないことの自己チェック)
- Produces: 各 kernel JSON は `{"schema": 1, "source": "<node module path>", "cases": [{"id", "input_b64"|"input", "expected": {...}}]}` 形式

- [ ] **Step 1: `.gitattributes` と共通モジュール**

`tests/fixtures/.gitattributes`:
```
* -text
*.json -text
```

`_shared.mjs` の要件: 旧リポジトリのパス解決(存在しなければ明示エラーで停止)、base64 変換、決定的 JSON 書き出し(`JSON.stringify(obj, sortedReplacer, 2) + "\n"`、改行は LF 固定)。`assertReadOnly()` は旧リポジトリ配下の `mtime` を採取前後で比較し、変化していたら異常終了する。

- [ ] **Step 2: 旧テストのインライン定数を転記**

`test/frontmatter.test.js` から `ESA_MIXED`(LF front matter + CRLF 本文)、`WEB_CRLF`(全 CRLF)、`B32DOC_BLOCK`(CRLF + ブロック値 + ネスト + Windows パス + sha1 値)、`NO_FRONTMATTER`、`EMPTY`、`BOM_DOC`(`\uFEFF` 始まり)を転記する。各定数の直前に `// 転記元: test/frontmatter.test.js:<行番号>` を付ける。

同様に `test/chunker.test.js` の14ケース、`test/sync-planner.test.js` の判定行列(11アクション全部+force+hash優先+`decideMissingCandidate` の3条件)、`test/metadata-schema.test.js` の分類表、`test/downloaders.test.js` の sanitize 系ケースを転記する。

- [ ] **Step 3: カーネル採取スクリプトを書く**

`capture-kernel.mjs` は旧リポジトリの ESM を直接 import して実行する:

```js
const root = oldRepoRoot();
const fm = await import(pathToFileURL(join(root, "tools/lib/frontmatter.js")));
const chunker = await import(pathToFileURL(join(root, "tools/lib/chunker.js")));
// line-range.js, embeddings.js, sync-planner.js, metadata-schema.js も同様
```

出力する期待値:
- **frontmatter**: `parseFrontmatter` の全戻り値(`hasFrontmatter, bom, eol, data, keys, blockKeys, body, raw`。`body`/`raw` は base64)、`hashBody`、`setFrontmatterValues` で4メタキーを付けた結果(base64)。
- **chunker**: 各チャンクの `index, heading_path, text(base64), start_line, end_line, token_estimate, content_hash`。`estimateTokens` の単体結果も CJK/ASCII 混在ケースで記録。
- **line-range**: `rangeHash(content, start, end)` を BOM 有無 × CRLF/LF × 境界(先頭行・末尾行・単一行)で記録。
- **e5 入力**: `embeddingInput(chunk, model)` の文字列(base64)と `input_hash`。**512字切詰の「後」に接頭辞が付く順序**を検証できるよう、切詰境界をまたぐ長さのケースを必ず含める。`queryInput` も記録。
- **sync-planner**: 入力(`remote`/`record`/`local`/`force`)と `decideSyncAction` の戻り値、`decideMissingCandidate` の戻り値、`ACTION_BUCKET` 全体。
- **metadata-schema**: `classifyDocument` の入出力、`REFERENCE_CORPUS_PREFIXES`、`FRONTMATTER_KEY_ORDER`、`safeBatchName`/`sanitizeFileName`/`sanitizeCategoryPath` の入出力(Windows 予約名・255バイト切詰・日本語を含む)。

- [ ] **Step 4: batch-config 採取**

`capture-batch-config.mjs`: `formatConfig` に旧テストの `SAMPLE_CONFIG` と、esa(配列)/web/git の3型を含む合成設定を渡して出力文字列(base64)を記録。加えて**実物の `batch-config.js` を読み、`loadBatchConfigsFresh` のパース結果と、`formatConfig` で再生成した文字列が実ファイルとバイト同一になること**を記録する(M2 のパーサーはこの JSON と一致すればよい)。

- [ ] **Step 5: 採取を実行し、冪等性を確認**

```bash
node tests/fixtures/capture/capture-kernel.mjs
node tests/fixtures/capture/capture-batch-config.mjs
# 2回目を別ディレクトリへ出して diff
```
Expected: 2回の出力がバイト同一。旧リポジトリの `git status` が変化していないこと。

- [ ] **Step 6: 自己検証テストを書く**

`tests/fixtures_check/test_fixture_integrity.py`:
- 各 kernel JSON が読め、`schema == 1`、`cases` が空でない。
- 全 `input_b64` が base64 デコードできる。
- BOM ケースがデコード後に `\ufeff` で始まる(改行変換で壊れていない)。
- CRLF ケースがデコード後に `\r\n` を含む(同上)。
- e5 入力ケースに 512 字切詰境界をまたぐものが最低1件ある。
- sync-planner ケースが11アクションすべてを網羅する。

Run: `uv run pytest tests/fixtures_check -v` → PASS

- [ ] **Step 7: コミット**

```bash
git add tests/fixtures/.gitattributes tests/fixtures/capture tests/fixtures/kernel tests/fixtures_check
git commit -m "$(cat <<'EOF'
feat(m1): カーネルゴールデン fixture を Node 版から採取

旧 test/*.test.js のインライン定数を転記し、期待値は旧 ESM を実行して採取。
front matter / チャンク / range_hash / e5入力 / 同期判定 / メタデータ / batch-config。
入力は base64 保存(BOM・CRLF 保護)、採取スクリプトは冪等。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 実データ層化サンプル採取

**Files:**
- Create: `tests/fixtures/capture/capture-real-docs.mjs`
- Generate: `tests/fixtures/real-docs/samples.json`
- Test: `tests/fixtures_check/test_fixture_integrity.py` に追記

転記した手書き fixture は境界ケースに強いが、実データの分布は別物である。実 `docs/` から層化抽出し、同じカーネル出力を記録する。

- [ ] **Step 1: 層化抽出ロジック**

`data/sync-state.sqlite` を **read-only** で開き(`file:...?mode=ro`)、`documents` から次の各層について最大 N 件ずつ決定的に選ぶ(パスの辞書順で先頭から。乱数を使わない):

| 層 | 条件 | 件数 |
|---|---|---|
| esa | `source='esa'` | 10 |
| web | `source='web'` | 10 |
| git | `source='git'` | 10 |
| reference | `document_type='reference'` | 10 |
| 日本語パス | パスに CJK を含む | 5 |
| 長いパス | パス長が上位 | 3 |
| BOM 付き | 実ファイル先頭が `EF BB BF` | 見つかる限り全部(最大5) |
| CRLF 本文 | 本文に `\r\n` を含む | 5 |
| front matter 無し | `parseFrontmatter().hasFrontmatter === false` | 5 |

重複は除く。各サンプルにつきファイルを読み、`frontmatter` / `chunker` / `line-range` / `e5入力` の出力を Task 1 と同じ形式で記録する。**巨大ファイルは fixture を肥大化させるため、64KB を超えるものは除外し、除外件数を manifest に記録する**(黙って落とさない)。

- [ ] **Step 2: 秘密情報スキャン**

出力 JSON に対し、`*_TOKEN` / `*_KEY` / `Authorization` / `Cookie` / URL 資格情報のパターン検査をかけ、1件でも当たったらそのサンプルを除外して除外理由を manifest に記録する。M0 の `mask_secrets` と同じパターンを使う(Python 側から呼び出して検査するか、同等の正規表現を JS 側に持つ)。

- [ ] **Step 3: 採取と冪等性確認**

Run: `node tests/fixtures/capture/capture-real-docs.mjs` を2回、出力が同一であること。旧リポジトリ無変更であること。

- [ ] **Step 4: 検証テストを追記**

`samples.json` が全層を最低1件ずつ含むこと、BOM 層と CRLF 層が空でないこと(空なら層化が壊れている)、除外件数が manifest に記録されていること。

- [ ] **Step 5: コミット**

---

## Task 3: MCP 契約 fixture 採取(15ツール)

**Files:**
- Create: `tests/fixtures/capture/capture-mcp.mjs`
- Generate: `tests/fixtures/mcp/tools-list.json`, `tests/fixtures/mcp/<server>/<tool>/<case>.json`
- Test: `tests/fixtures_check/test_mcp_fixture_coverage.py`

設計書 §7.4.1 が要求する3層(ツール名 / 入出力 / サーバー構成)の証拠を、**Python 移植を書く前に**確保する。

- [ ] **Step 1: stdio クライアントを書く**

`capture-mcp.mjs` は各サーバーを子プロセスで起動し(`.mcp.json` の `command`/`args` をそのまま使う。cwd は旧リポジトリ)、JSON-RPC を stdin/stdout でやり取りする:
1. `initialize` → `initialized` 通知
2. `tools/list` → 全ツールのスキーマを `tools-list.json` へ
3. ツールごとに `tools/call` を実行し、**生のレスポンス JSON をそのまま**ファイルへ

stdout は JSON-RPC 専用なので、行単位でパースする。stderr は別に捕捉し、`stderr_tail` としてケースファイルへ添える(stdout に混ざっていないことの証拠になる)。

- [ ] **Step 2: ケース定義(15ツール × 各2件以上)**

**破壊的な副作用を起こさないこと。** `run_batch` / `download_*` / `render_scene` は実行すると `docs/` に書き込む。したがって:
- 正常系は**実行しない**。代わりに存在しないバッチ名・不正な URL・不正な post 番号でエラー系を採り、`tools/list` のスキーマで入出力契約を押さえる。
- 例外として `list_batches` / `search_kb` / `get_document` / `get_chunk` / `index_status` / `list_scene_kinds` / `check_visualize_deps` は副作用が無いので正常系を採る。
- `render_scene` は SceneSpec 検証エラー(`INVALID_SCENE_SPEC`)と出典ハッシュ不一致(`SOURCE_HASH_MISMATCH`)を採る。実レンダリングはしない。
- `add_web_batch` は `batch-config.js` を書き換えるため**呼ばない**。`tools/list` のスキーマのみ記録し、その旨をケースファイルに `"skipped_reason"` として明示する。

| サーバー | ツール | 採るケース |
|---|---|---|
| kb-download | `list_batches` | 正常(全バッチ) |
| | `run_batch` | 不明バッチ名エラー |
| | `add_web_batch` | スキーマのみ(skipped) |
| | `download_esa_post` | 不正 post 番号エラー |
| | `download_esa_category` | 空文字カテゴリエラー |
| | `download_esa_search` | 空クエリエラー |
| | `download_web` | 不正 URL エラー |
| | `download_git` | 不正リポジトリ URL エラー |
| kb-search | `search_kb` | 正常(日本語クエリ)/ 正常(識別子クエリ)/ 0件クエリ / 各フィルタ / corpus=reference |
| | `get_document` | 正常 / 行範囲付き / 存在しないパス / パストラバーサル試行 |
| | `get_chunk` | 正常 / 存在しない chunk_id |
| | `index_status` | 正常 |
| kb-visualize | `list_scene_kinds` | 正常 |
| | `check_visualize_deps` | 正常 |
| | `render_scene` | INVALID_SCENE_SPEC / SOURCE_HASH_MISMATCH |

`search_kb` の正常系は `eval/queries.jsonl` から実際のクエリを使う(Task 4 のベースラインと突き合わせられる)。

- [ ] **Step 3: 採取実行**

Run: `node tests/fixtures/capture/capture-mcp.mjs`
実行後、旧リポジトリの `git status` と `docs/` の mtime が無変化であることを確認する。変化していたら**そのケースを削除し、副作用のあるツールとして記録し直す**。

- [ ] **Step 4: カバレッジ検証テスト**

`tests/fixtures_check/test_mcp_fixture_coverage.py`:
- 15ツールすべてが `tools-list.json` に存在し、`kb-download` 8 / `kb-search` 4 / `kb-visualize` 3 の所属になっている。
- 各ツールに fixture ケースが1件以上(`add_web_batch` は `skipped_reason` 付きで可)。
- 副作用のないツールは2件以上。
- 各ケースの `stdout` が単一の JSON-RPC レスポンスとしてパースでき、ANSI 制御文字を含まない。
- レスポンスの `content[0].type == "text"` で、`text` が JSON としてパースできる。
- `isError` フィールドの有無と値が記録されている。

Run: `uv run pytest tests/fixtures_check -v` → PASS

- [ ] **Step 5: コミット**

---

## Task 4: 検索評価ベースライン採取

**Files:**
- Create: `tests/fixtures/capture/capture-eval.mjs`
- Generate: `tests/fixtures/eval/baseline.json`(および `eval/queries.jsonl` を新リポジトリへコピー)

- [ ] **Step 1: `eval/queries.jsonl` を新リポジトリへコピー**

22クエリをバイト保持でコピーし、SHA-256 を manifest に記録する。これは移植後も同じクエリで測るための正本になる。

- [ ] **Step 2: ベースライン採取**

`tools/eval-search.js` の関数を import して(main を走らせずに)、`hybrid` と `bm25_raw` の両方式で22クエリを実行し、次を記録:
- クエリごとの**ランク付き結果リスト**(`path`, `post_number`, `chunk_id`, `score`, `start_line`, `end_line`)
- クエリごとの Recall@5 / RR / nDCG@10
- 方式ごとのマクロ平均(Recall@5 / MRR / nDCG@10)
- `foldToDocuments` 適用後のリストも別途記録(スコアリングは文書単位で行うため)

応答時間は非決定値なので **fixture には入れない**(冪等性を壊す)。参考値としてのみ manifest へ書く。

- [ ] **Step 3: 冪等性確認**

同じ索引に対して2回実行し、ランクとスコアがバイト同一であること。異なる場合はベクトル検索のキャッシュ順序など非決定要素があるので、原因を特定して記録する(移植後の比較で許容差を決める材料になる)。

- [ ] **Step 4: 検証テスト追記**

22クエリ全部がある / 各クエリに `relevant` がある / 方式ごとのマクロ平均が 0〜1 に収まる。

- [ ] **Step 5: コミット**

---

## Task 5: 埋め込みゲート素材と HTML 変換ゴールデン、manifest 生成

**Files:**
- Create: `tests/fixtures/capture/capture-embeddings.mjs`, `tests/fixtures/capture/capture-html.mjs`
- Generate: `tests/fixtures/embedding/gate-samples.json`, `tests/fixtures/html/turndown-goldens.json`, `tests/fixtures/capture-manifest.json`

- [ ] **Step 1: 埋め込みゲート素材**

`data/kb-index.sqlite` を read-only で開き、`chunks` を `id` 昇順で走査して**決定的に**100件を層化抽出(短文/中文/長文 × 日本語主体/英数主体/混在)。各件について:
- `chunk_id`, `path`, `chunk_index`, `content_hash`
- `embeddingInput(chunk, model)` の文字列(base64)と `input_hash`
- `embeddings.vector` BLOB を Float32 LE として読み、**base64 のまま**保存(数値配列にすると精度が落ちる)
- `model`, `dimensions`

加えて全体の件数(`documents` / `chunks` / `embeddings`)と `meta` テーブル全体を記録する。

これは設計書 §11.2 のゲート判定に使う: Python の sentence-transformers で同じ入力を埋め込み、全100件でコサイン類似度 ≥0.999 なら既存ベクトル再利用可、そうでなければ全件再生成。**旧版は q8 量子化 ONNX なので不合格が既定想定**であり、不合格自体は失敗ではない。

- [ ] **Step 2: HTML→Markdown ゴールデン**

`download-web.js` が使う turndown + cheerio の設定を再現し、代表的な HTML 断片(見出し / ネストしたリスト / テーブル / コードブロック / 相対リンク / 画像 / インライン要素の混在 / 日本語)に対する Markdown 出力を記録する。実サイトへは取りに行かず、合成 HTML を使う(再現性のため)。M3 の Python 実装(markdownify 等)との差分を事前に把握するのが目的で、**完全一致は要求しない**——差分を許容判断するための材料である。

- [ ] **Step 3: manifest 生成**

`capture-manifest.json` に記録する:
- 採取日時(UTC)、Node 版数、旧リポジトリのパスと `git rev-parse HEAD`(取得できれば)
- fixture カテゴリごとのファイル数・総バイト数・各ファイルの SHA-256
- 除外したもの(64KB 超のファイル、秘密情報を含むサンプル、副作用のためスキップした MCP ツール)とその理由・件数
- 非決定だった項目(応答時間など)の一覧

**除外は黙って行わない。** manifest に理由付きで残すことで、「網羅した」と誤読されるのを防ぐ。

- [ ] **Step 4: 全体検証テスト**

`tests/fixtures_check/test_fixture_integrity.py` に追記:
- manifest の各 SHA-256 が実ファイルと一致する。
- 埋め込みサンプルが100件、全件に `vector_b64` と `input_hash` がある。
- ベクトルを base64 デコードした長さが `dimensions * 4` バイトに一致する。
- fixture 全体に秘密情報パターンが含まれない(M0 の `mask_secrets` を適用して変化しないこと)。

- [ ] **Step 5: 再採取手順を書く**

`tests/fixtures/capture/README.md`: 前提(Node 版数、旧リポジトリの場所、`KB_OLD_REPO` 上書き)、実行順、冪等性の確認方法、**旧システムが変更された場合は fixture を手で編集せず必ず再採取して差分をレビューする**という原則。

- [ ] **Step 6: コミット**

---

## Verification(M1 ゲート)

1. 採取スクリプトを全部再実行し、生成物がバイト同一であること(冪等性)。
2. 旧リポジトリが無変更であること: `git -C C:\Temp\multi-source-knowledge-base status --porcelain` が空、`docs/` と `data/` の mtime が採取前後で不変。
3. `uv run pytest tests/fixtures_check -q` が全 PASS。
4. 15 MCP ツールすべてに fixture があり、サーバー所属が `kb-download` 8 / `kb-search` 4 / `kb-visualize` 3 で一致。
5. 旧テスト28ファイルに対する転記カバレッジチェックリストが埋まっている(どのテストファイルのどの定数を採ったか、採らなかったものはなぜか)。
6. `capture-manifest.json` に未説明の除外が無い。
7. fixture 全体に秘密情報が含まれない。

## 次のマイルストーン

M1 完了後は **M2(ビット互換カーネル移植)**。M1 のゴールデンを読むパラメトリックテストを先に書き、全件一致するまで実装する。M2 のゲートを満たさないうちは M3 以降に着手しない。

## fixture 由来の恒久記録

各 fixture カテゴリの契約(ビット互換 / advisory)、旧テスト28ファイルへの転記カバレッジ、既知のギャップ・除外、旧システムのデータ層に関する訂正事実、read-only 規律の教訓は [`tests/fixtures/PROVENANCE.md`](../../tests/fixtures/PROVENANCE.md) に記録した(このタスクの `.superpowers/sdd/` 配下の作業ログは gitignore 対象でマイルストーン完了後に削除されるため)。
