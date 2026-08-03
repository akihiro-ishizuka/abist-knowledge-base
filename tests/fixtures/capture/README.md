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

## 実行方法

```bash
node tests/fixtures/capture/capture-kernel.mjs
node tests/fixtures/capture/capture-batch-config.mjs
node tests/fixtures/capture/capture-real-docs.mjs
node tests/fixtures/capture/capture-mcp.mjs
```

既定では旧リポジトリを `C:\Temp\multi-source-knowledge-base` に想定する。
別の場所にある場合は `KB_OLD_REPO` 環境変数でパスを指定する。

## 冪等性の確認

採取は決定的でなければならない（タイムスタンプ・`Math.random`・
ファイルシステムの列挙順に依存する値を含めない。オブジェクトキーは
`writeDeterministicJson` が再帰的にソートする）。2回連続で実行し、
出力が完全に同一であることを次のように確認できる:

```bash
node tests/fixtures/capture/capture-kernel.mjs
sha256sum tests/fixtures/kernel/*.json > /tmp/run1.sha256
node tests/fixtures/capture/capture-kernel.mjs
sha256sum tests/fixtures/kernel/*.json > /tmp/run2.sha256
diff /tmp/run1.sha256 /tmp/run2.sha256   # 差分が無いこと
```

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
