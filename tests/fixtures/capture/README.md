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
