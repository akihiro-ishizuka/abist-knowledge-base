# M9: `.mcp.json` カットオーバー手順(記録のみ、M5 task-4)

**この文書は手順の記録であり、本タスク(M5 task-4)では一切実行しない。**
切り替えの実行は M9 で行う。旧リポジトリ(`C:/Temp/multi-source-knowledge-base`)は
この計画作成時点で `git status --porcelain` が75行のベースラインのままであり、
本タスクはそれを変更していない(確認済み)。

## 対象

旧リポジトリの `.mcp.json`(`C:/Temp/multi-source-knowledge-base/.mcp.json`)は
現在この形:

```json
{
  "mcpServers": {
    "kb-download": { "command": "node", "args": ["tools/kb-download-mcp.js"] },
    "kb-search": { "command": "node", "args": ["tools/kb-search-mcp.js"] },
    "kb-visualize": { "command": "node", "args": ["tools/kb-visualize-mcp.js"] }
  }
}
```

M9 でのカットオーバーは **`kb-download` と `kb-search` の2キーだけ** を
Python 実装の起動コマンドへ差し替える。`kb-visualize` は M7 まで実装され
ないため、この段階では node のまま残す(または `kb-visualize` が実装され
Python 側の契約が固まった後、別の変更として差し替える)。

## 切り替え後の値

Python 側のエントリポイントは `abist-kb mcp serve <server>`
(`pyproject.toml` の `[project.scripts] abist-kb = "abist_kb.presentation.cli.app:main"`、
`src/abist_kb/presentation/cli/mcp_cmd.py`)。`--root` で旧リポジトリの
ルート(docs/ や app.sqlite のあるディレクトリ)を明示すること — Python 版の
既定カレントディレクトリは MCP クライアントの起動時 cwd に依存するため、
`.mcp.json` からの起動では明示指定が安全(`args` に `--root <旧リポジトリの
絶対パス>` を含める)。

```json
{
  "mcpServers": {
    "kb-download": {
      "command": "abist-kb",
      "args": ["--root", "C:/Temp/multi-source-knowledge-base", "mcp", "serve", "kb-download"]
    },
    "kb-search": {
      "command": "abist-kb",
      "args": ["--root", "C:/Temp/multi-source-knowledge-base", "mcp", "serve", "kb-search"]
    },
    "kb-visualize": {
      "command": "node",
      "args": ["tools/kb-visualize-mcp.js"]
    }
  }
}
```

`command` を `abist-kb` の絶対パス(`uv run --project <python repo> abist-kb`
形式、または `pip install -e` 済みのインタプリタの絶対パス)に置き換える必要が
実運用ではあり得る。MCP クライアント(Claude Desktop 等)が `PATH` を継承
しない場合は `command` をフルパスにする。

## 前提条件(切り替え前に満たしておくこと)

1. Python 側の `app.sqlite`/索引DB(`work-index.sqlite`/`reference-index.sqlite`)
   が旧リポジトリの `docs/` と整合した状態で存在すること(M1〜M4 のマイグレー
   ション/索引構築が完了していること)。
2. `abist-kb` コマンドが切り替え先の環境で解決できること(`where abist-kb`
   相当で確認)。
3. Python 版 `kb-download`/`kb-search` が対象で、`kb-visualize` は対象外
   であることをもう一度確認する(このタスクの範囲外の3ツールを誤って
   切り替えない)。

## 検証手順(切り替え直後)

1. MCP クライアントを再起動し、`kb-download`/`kb-search` が起動することを
   確認する(`tools/list` が返る、`tool_count` が旧 fixture
   (`tests/fixtures/mcp/tools-list.json`)と一致する: kb-download 8、
   kb-search 4)。
2. `kb-search.search_kb`/`kb-download.list_batches` など軽い読み取り系
   ツールを1回ずつ呼び、`ok: true` が返ることを確認する。
3. 書き込み系(`run_batch`/`download_web` 等)は影響範囲が大きいため、
   検証は非破壊なクエリ(`list_batches`/`get_batch`/`search_kb`)に留め、
   実際の同期実行は別途の受け入れテストとして計画する(本文書の範囲外)。
4. `kb-visualize` が変わらず node のまま起動することを確認する
   (誤って一緒に切り替えていないことの確認)。

## 切り戻し手順

`.mcp.json` の `kb-download`/`kb-search` の2エントリを、この文書冒頭の
「対象」節に記載した元の値(`node`, `tools/kb-download-mcp.js` /
`tools/kb-search-mcp.js`)に戻し、MCP クライアントを再起動するだけでよい。
Python 側は旧リポジトリのファイルを一切書き換えないため(`app.sqlite`/
索引DB は Python 側管理下の別ファイルであり、旧 Node 実装が読む状態を
変更しない設計 — `docs/` への書き込みのみ両実装が触るが、書式は
互換フォーマットのまま)、切り戻しに追加のデータ復旧手順は不要という
想定。ただし M9 実施時点で「Python 版稼働中に書き込まれた `docs/` の
差分を Node 版がどう扱うか」を再確認すること(本文書はあくまで
`.mcp.json` の切り替え手順のみを対象とし、データ整合性の検証は M9 の
別タスクの責務とする)。

## 変更してはいけないもの

- `kb-visualize` エントリ(M7 まで node のまま)。
- 旧リポジトリの他のファイル(`docs/`, `data/`, `tools/*.js` など)。
  カットオーバーは `.mcp.json` の2行の書き換えのみ。
