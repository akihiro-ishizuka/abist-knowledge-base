---
name: downloading-kb-docs
description: Use when asked to download/収集/取り込む external sources into this repo's docs/ — e.g. "○○バッチを回して", "docsを最新化して", "esaのカテゴリ△△をダウンロード", mirror a web site or clone a Git repo into docs/. Covers the kb-download MCP tools (list_batches / run_batch / add_web_batch / download_esa_post / download_esa_category / download_esa_search / download_web / download_git) and how to read their JSON results. NOT for reading/updating/creating individual esa 記事 on esa itself — use managing-esa-posts for that.
---

# Downloading knowledge-base docs (kb-download MCP)

## Overview

stdio MCP サーバー `kb-download`（実体: `tools/kb-download-mcp.js`）が既存の
download-*.js をラップし、esa 記事・Web サイト・Git リポジトリのローカル docs/ への
収集をツール呼び出しで実行できる。

登録済み: `.cursor/mcp.json`（Cursor 用）と `.mcp.json`（Claude Code 用。初回は承認プロンプトが出る）。

## 役割分担

- **本スキル / kb-download MCP**: ローカル docs/ への一括ミラー（バッチ・カテゴリ・Web・Git）
- **managing-esa-posts**: esa 上の個別記事の読み書き・検索（esa API 直叩き、tools/*.py）

## ツール一覧

| ツール | 用途 | 主要パラメータ | タイムアウト |
|--------|------|----------------|--------------|
| `list_batches` | バッチ一覧（名前・型・出力先） | なし | - |
| `run_batch` | 定義済みバッチ実行（esa/web/git 自動判別） | `batch` | 60分 |
| `add_web_batch` | Web バッチを batch-config.js に登録 | `name`, `url`, `outputDir?`, `maxDepth?`, `delay?`, `overwrite?` | - |
| `download_esa_post` | esa 記事1件 | `post`, `outputDir?` | 10分 |
| `download_esa_category` | esa カテゴリ一括 | `category`, `outputDir?` | 10分 |
| `download_esa_search` | esa 検索結果一括 | `query`, `outputDir?` | 10分 |
| `download_web` | Web 再帰クロール→Markdown 保存 | `url`, `outputDir?`, `maxDepth?`, `delay?`, `concurrency?` | 30分 |
| `download_git` | Git shallow クローン | `repository`, `branch?`, `outputDir?` | 15分 |

実行系ツール（`run_batch` / `download_*`）の応答は JSON テキスト:
`{ok, exitCode, command, outputDir, stdoutTail, stderrTail, error?}`
（`list_batches` は `{ok, count, batches}`、`add_web_batch` は `{ok, action, name, batch, warnings?}` を返す）

## Workflow

1. 「最新化して」「バッチを回して」「docs を同期」→ まず `list_batches` で正確な名前を確認する。
   バッチ名を推測で打たない。名前が曖昧なら一覧をユーザーに見せて確認する
   （勝手に重い web バッチを回さない）。
2. `run_batch` には `list_batches` の `name` をそのまま渡す（日本語・記号含む）。
3. 応答の `ok: false` なら `stderrTail` → `stdoutTail` の順で原因を確認して報告する。
   成功時は `outputDir` を出力先として報告する。
4. 「このサイトも収集対象にして」「○○をバッチ登録して」→ `add_web_batch` で登録する。
   登録は再起動なしで `list_batches` / `run_batch` に反映される。初回収集まで頼まれたら
   続けて `run_batch` を呼ぶ（web クロールは重いので勝手に回さない）。
   同名バッチの上書き（`overwrite:true`）はユーザーに確認してから行う。

## 注意

- esa 系ツールは `.env` の `ESA_TEAM_NAME` / `ESA_ACCESS_TOKEN` が必要。トークンは決して表示しない。
- `download_esa_category` / `download_esa_search` は**ヒット0件でも `ok: true`**。
  `stdoutTail` の件数表示を必ず確認する。
- `download_git` は出力先の既存フォルダを**削除して置き換える**。
  `outputDir` が `docs` 始まりでなければ `docs/` が前置される。
- web バッチ / `download_web` の出力先は設定値・指定値がそのまま使われ、`docs/` の自動前置は**ない**
  （git と異なる）。例: `C#ATIA` バッチはリポジトリ直下の `C#ATIA/` に出力される。
  ただし `add_web_batch` 経由の登録時は `docs/` が前置された値が保存される。
- 同時実行は busy エラーで即時拒否される。ツールは直列で呼ぶこと。
  web は最長30分、`run_batch` は最長60分ブロックしうる。
- Web バッチの追加・更新は `add_web_batch` でツール化済み（同名は `overwrite:true` が必要。
  esa/git 型の同名は上書き不可）。esa / git バッチの定義はツール化されておらず
  `batch-config.js` を直接編集する（編集後のサーバー再起動は不要。呼び出し毎に再読込される）。
- `C#ATIA` のような **docs/ 外出力の既存 web バッチは `add_web_batch` で上書きしない**
  （`docs/` 前置で出力先が変わり、応答の `warnings` に出る）。これらの変更は
  `batch-config.js` の手編集で行う。
- MCP 未接続時のフォールバック: `node download-batch.js <バッチ名>` を直接実行。
- OpenAI チャット（`npm run chat`）や `/api/chat` はこの用途では使わない。
