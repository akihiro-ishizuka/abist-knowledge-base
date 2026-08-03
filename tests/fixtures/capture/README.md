# M1 フィクスチャ採取スクリプト

このディレクトリの `.mjs` スクリプトは、旧 Node システム
(`C:\Temp\multi-source-knowledge-base`、環境変数 `KB_OLD_REPO` で上書き可) の
ESM モジュールを直接 `import` して実行し、その戻り値を
`tests/fixtures/kernel/*.json` にゴールデン値として保存する。

Python 実装の「正しさ」は、ここで採取した値と一致するかどうかで決まる。
そのため期待値はこのスクリプトの中で計算・推測しない。すべて旧リポジトリの
実コードを実行して得た値をそのまま記録する。

## ハードな制約

**旧リポジトリを一切変更しない。** 読むだけで、書き込みは一切行わない。
`npm install` も実行しない（`package-lock.json` が書き換わるため。
旧リポジトリの `node_modules` は採取済みのものをそのまま使う）。

各スクリプトは `assertReadOnly()` を冒頭と末尾で呼び出す。1回目の呼び出しで
`git status --porcelain` と `docs/` `data/` `batch-config.js` の mtime を
基準点として記録し、2回目の呼び出しで再度取得して比較する。差異があれば
例外を投げてスクリプトを異常終了させる。

## 全体の再採取手順(M1完了後、旧システムに変更があった場合)

**前提:**
- Node v22.18.0(旧リポジトリの `node_modules` を直接 import して使うため、
  旧リポジトリで `npm install` 済みであること。このスクリプト群からは
  `npm install` を絶対に実行しない — `package-lock.json` が書き換わるため)。
- 旧リポジトリの場所: 既定 `C:\Temp\multi-source-knowledge-base`。
  別の場所にある場合は環境変数 `KB_OLD_REPO` でパスを上書きする。
- uv (CPython 3.12) — Python 側の検証テスト実行に使う。
- 旧リポジトリを**絶対に変更しない**(下記「読み取り専用SQLiteの教訓」参照)。

**実行順序**(依存関係は無いが、レポート等での参照のため以下の順を推奨する):

```bash
node tests/fixtures/capture/capture-kernel.mjs
node tests/fixtures/capture/capture-batch-config.mjs
node tests/fixtures/capture/capture-real-docs.mjs
node tests/fixtures/capture/capture-mcp.mjs
node tests/fixtures/capture/capture-eval.mjs
node tests/fixtures/capture/capture-embeddings.mjs
node tests/fixtures/capture/capture-html.mjs
node tests/fixtures/capture/build-manifest.mjs
uv run pytest tests/fixtures_check -q
```

`build-manifest.mjs` は他の全採取スクリプトが生成した `tests/fixtures/**` を
集計するため、必ず最後(他の全スクリプト実行後)に実行する。

### 読み取り専用SQLiteの教訓(必ず守ること)

`data/kb-index.sqlite`・`data/reference-index.sqlite` を **better-sqlite3 で
素朴に `new Database(path)` で開くだけでファイルの mtime が変化する**ことが
Task 3 で実測判明している(ファイルサイズ・内容は不変。WALチェックポイントや
SQLiteのファイル変更カウンタ更新のような、論理内容を変えない内部housekeeping
が原因と推測されるが、事前ハッシュを取っていなければ「内容が一切変わって
いないこと」を遡って証明する手段が無い)。

対処方法は2つある(このリポジトリでは一貫して**サンドボックスコピー方式**を
採用している):

1. **サンドボックスコピー方式(採用)**: `os.tmpdir()` 配下の使い捨て
   ディレクトリへ対象DBファイル(`kb-index.sqlite`・`kb-index.sqlite-wal`・
   `kb-index.sqlite-shm`・`reference-index.sqlite`)を**コピー**し、
   コピーだけを開く。旧リポジトリの実ファイルには一切触れない。
   `capture-mcp.mjs`(kb-search)・`capture-eval.mjs`・`capture-embeddings.mjs`
   がこの方式を採る。`docs/`・`node_modules/` が必要な場合は
   `mklink /J`(ディレクトリジャンクション、読み取り専用参照)を使う
   (`fs.rmSync(recursive)` はジャンクションを `isSymbolicLink()` として検出し、
   リンク先には再帰しないことを事前に隔離環境で実証済み)。
2. **`{readonly: true, fileMustExist: true}` オプション方式**: better-sqlite3
   にはこのオプションで mtime を変化させずに開けるという報告があるが、
   このリポジトリでは方式1(サンドボックスコピー)の方が実証済みで安全側に
   倒せるため、一貫してこちらだけを使っている。新しく採取スクリプトを書く
   場合も、方式1を踏襲すること。

いずれの方式を採っても、**採取スクリプトの冒頭・末尾で `assertReadOnly()` を
呼び、旧リポジトリの `git status --porcelain` と `docs/`・`data/`・
`batch-config.js` の (size, mtime) フィンガープリントが変化していないことを
機械的に確認する。**「読んだだけのはず」という願望では止めない。

### 冪等性の確認(全スクリプト共通)

```bash
node tests/fixtures/capture/<script>.mjs
sha256sum tests/fixtures/<category>/*.json > /tmp/run1.sha256
node tests/fixtures/capture/<script>.mjs   # 2回目
sha256sum tests/fixtures/<category>/*.json > /tmp/run2.sha256
diff /tmp/run1.sha256 /tmp/run2.sha256   # 差分が無いこと
```

### フィクスチャは手で編集しない

**`tests/fixtures/**` 配下の JSON・JSONL を直接手編集してはならない。**
値を1つ直したいだけでも、必ず対応する `capture-*.mjs` を直して再実行し、
生成された差分をレビューする。手編集を許すと「このフィクスチャは旧システムを
実行して得た値である」という前提(このディレクトリ全体の存在理由)が壊れ、
Python移植の正しさの基準がフィクスチャ作成者の主観にすり替わってしまう。
旧システムに変更があった場合も同様に、該当する `capture-*.mjs` を再実行して
差分をレビューする(旧システムが変わった事実そのものを握りつぶさない)。

## ファイル構成

- `_shared.mjs` — 共通基盤。`oldRepoRoot()` / `b64()` /
  `writeDeterministicJson()` / `assertReadOnly()` を提供する。
- `capture-kernel.mjs` — frontmatter / chunker / line-range / embeddings(e5) /
  sync-planner / metadata-schema の6ファイルを
  `tests/fixtures/kernel/*.json` に出力する。
- `capture-batch-config.mjs` — `tools/lib/batch-config-store.js` の
  `formatConfig` / `loadBatchConfigsFresh` と、実物の `batch-config.js` との
  ラウンドトリップ結果を `tests/fixtures/kernel/batch-config.json` に出力する。
- `capture-real-docs.mjs` — `data/sync-state.sqlite`（read-only）の
  `documents` から9層（esa/web/git/reference/日本語パス/長いパス/BOM付き/
  CRLF本文/frontmatter無し）を決定的に層化抽出し、選ばれた各実文書について
  Task 1 と同じカーネル出力（frontmatter/hashBody/chunker/rangeHash/e5入力）を
  `tests/fixtures/real-docs/samples.json` に出力する。64KB超のファイルと
  秘密情報パターンに当たったサンプルは除外し、除外件数・理由・層ごとの
  実採取数（0件の層も含む）を manifest（`layers[]` / `exclusions[]`）に記録する。
- `capture-mcp.mjs` — kb-download(8ツール)/kb-search(4ツール)/
  kb-visualize(3ツール)の3つの MCP stdio サーバーを子プロセスで起動し、
  JSON-RPC 2.0 の生レスポンスをそのまま `tests/fixtures/mcp/tools-list.json`
  (`tools/list` の全スキーマ)と `tests/fixtures/mcp/<server>/<tool>/<case>.json`
  (各ケースの生レスポンス)へ出力する。他の採取スクリプトと違い期待値は
  カーネル関数の戻り値ではなく MCP プロトコル応答そのものであり、
  `{schema, request, response, response_raw_line, response_kind,
  is_error_present/value, stderr_tail, run2, nondeterministic_fields}` という
  専用の形式を持つ（`{schema, source, cases}` 形式ではない）。
  破壊的なツール(`run_batch`/`download_*`/`render_scene`)は正常系を実行せず、
  spawn 前に(zod スキーマ検証またはハンドラ内の早期リターンで)弾かれる
  エラー系のみを採る。`add_web_batch` は呼び出さず `tools/list` のスキーマのみ
  `skipped_reason` 付きで記録する。**kb-search だけは実 SQLite 索引
  (`data/kb-index.sqlite`・`data/reference-index.sqlite`)を開くと
  ファイルの mtime が変化することが実測で判明した(サイズは不変)ため、
  旧リポジトリ外の使い捨てサンドボックスへ `tools/kb-search-mcp.js` と
  依存を丸ごとコピーし(`docs/`・`node_modules/` はジャンクションで読み取り
  専用参照、`data/*.sqlite` はコピー)、そこで動かしてから破棄する。**
  各ケースは2回実行し、応答の構造的 diff を `nondeterministic_fields` として
  記録する(採取そのものは決定的でなくてよいが、どのフィールドが揺れるかは
  記録する。詳細は `.superpowers/sdd/M1-fixture-capture/task-3-report.md`)。

  **M5 への注意(`response_kind: "sdk_validation_error"` の4ケース):**
  `download_esa_post`/`download_esa_category`/`download_esa_search`/
  `download_git` の zod スキーマ検証エラーは `content[0].text` が
  `"MCP error -32602: Input validation error: ..."` という平文になるが、
  この文言は旧リポジトリの zod + `@modelcontextprotocol/sdk` の
  バージョンに固有の生成物であり、Python 版(pydantic + MCP Python SDK)は
  必然的に異なる文言を返す。M5 の比較器はこの4ケースを `content[0].text` の
  素の文字列一致で比較してはいけない。`response_kind`・`isError`・
  JSON-RPC エラーコード `-32602` の一致で判定すること。
- `capture-eval.mjs` — `eval/queries.jsonl`(22クエリ)をバイト保持コピーし、
  `tools/eval-search.js`(loadQueries/recallAtK/reciprocalRank/ndcgAtK/
  foldToDocuments)と `tools/lib/search-engine.js`(search/keywordSearch)を
  実行して `bm25_raw`(FTS5のみ)/`hybrid`(統合検索)の2方式で
  Recall@5・MRR・nDCG@10 のベースラインを `tests/fixtures/eval/baseline.json`
  へ出力する。`data/kb-index.sqlite` はサンドボックスへコピーして使う
  (下記「読み取り専用SQLiteの教訓」参照)。`baseline.json` は BOM/CRLF を
  保持すべき生テキストを含まないため、他のフィクスチャと違い `*_b64` を
  使わず素の JSON 文字列で記録する(意図的な形式の違い。理由は
  `.superpowers/sdd/M1-fixture-capture/task-4-report.md §7`)。
- `capture-embeddings.mjs` — `data/kb-index.sqlite`(サンドボックスコピー)の
  `chunks JOIN embeddings` を `id` 昇順で走査し、文字数(短/中/長)×文字種構成
  (日本語主体/英数主体/混在)の9層から決定的に100件を層化抽出して
  `tests/fixtures/embedding/gate-samples.json` へ出力する。各サンプルは
  e5入力文字列(`tools/lib/embeddings.js::embeddingInput`)・`input_hash`・
  保存済みベクトル BLOB(Float32 LE、base64、数値配列化しない)を持つ。
  design/system-design.md §11.2 の埋め込み再利用ゲート判定(コサイン類似度
  >=0.999 かつ Recall@5低下<=0.01)の測定入力であり、ゲート判定自体(合格/
  不合格)はここでは出さない(判定はM8の役目)。
- `capture-html.mjs` — `download-web.js` の `TurndownService` 設定
  (`:37-43`)と `htmlToMarkdown`(非export、`:126-161`)の前処理ロジック
  (相対リンク/画像の絶対URL化、`nav/header/footer/aside/.sidebar/.navigation`
  の削除)を複製し、合成HTML(見出し・入れ子リスト・テーブル・言語付き
  コードブロック・相対/絶対リンク・画像・インライン強調混在・日本語全角記号・
  実サイト風合成ページ、計15ケース)を変換して
  `tests/fixtures/html/turndown-goldens.json` へ出力する。実サイトへは
  一切アクセスしない。`comparison_policy: "advisory"` — kernel/ のビット
  互換契約と違い、M3のPython実装(markdownify等)との差分を事前に把握する
  ための資料であり、完全一致は要求しない。
- `build-manifest.mjs` — `tests/fixtures/` 配下の全カテゴリ
  (kernel/real-docs/mcp/eval/embedding/html/capture/root)を集計し、
  カテゴリごとのファイル数・総バイト数・各ファイルのSHA-256、
  採取日時(このリポジトリの git log 由来。壁時計の「今」は使わない)、
  Node版数、旧リポジトリのパスと git HEAD、Task 1〜5で判明した除外・逸脱の
  レジストリ(`exclusions_registry`)、非決定項目一覧
  (`nondeterministic_items`)を `tests/fixtures/capture-manifest.json` へ
  出力する。自分自身(`capture-manifest.json`)は集計対象から除外する
  (自己参照の循環を避けるため)。他の全採取スクリプトの後、最後に実行する。

## フィクスチャの形式

各 JSON は `{"schema": 1, "source": "<旧リポジトリ内のモジュールパス>", "cases": [...]}`
の形をとる。各 `case` は `id` と `expected` を持ち、生の文字列を保持する必要が
ある入力・期待値は `*_b64` サフィックスの付いたキーに base64 で格納する
（BOM・CRLF・日本語を含む文字列が改行変換や JSON 往復で壊れないようにするため。
`tests/fixtures/.gitattributes` で `-text` を指定し、git 側の改行変換も止めている）。

## 転記のルール

旧テストファイル（`test/*.test.js`）からインライン定数を転記する箇所には、
必ず直前に `転記元: test/xxx.test.js:<行番号>` の形でコメントを付けている。
レビュワーはそのコメントを頼りに旧ファイルと transcribe 元を diff できる。

転記した定数を旧テストの `assert.equal` / `assert.deepEqual` と突き合わせる
自己検証 (`assertMatchesOldTest` / インラインの `assertMatchesOldTest` 呼び出し)
もスクリプト内に含めている。転記ミスや旧テストの読み違いがあれば、値を
その場で合わせるのではなく例外を投げてスクリプトを止める。
