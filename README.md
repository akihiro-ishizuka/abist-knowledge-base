# ABIST Knowledge Base

Python 移植版。設計の正は [`design/system-design.md`](design/system-design.md)。操作契約の正本は [`design/ui-action-matrix.yaml`](design/ui-action-matrix.yaml)。

## 初回セットアップ

```bash
uv sync
abist-kb init                        # ディレクトリ・設定雛形・app.sqlite を作成
# docs/ に .md を置く（参照コーパス向けは docs/knowledge/B32doc/）
abist-kb document register-disk      # 何が登録されるかを確認（dry-run）
abist-kb document register-disk --apply
abist-kb index build --corpus work
abist-kb index embed --corpus work   # 意味検索が要る場合
```

> **置くだけ／`index build` だけでは索引されません。** work 索引の対象は `app.sqlite` の `documents` テーブルに登録済みの文書だけです。手で置いた `.md` は必ず `abist-kb document register-disk --apply` で台帳へ登録してください（未登録のファイルは `index build` の `disk_only_paths` として報告されるだけで、検索には載りません）。

`abist-kb init` は再実行しても既存のファイル・DB 行を変更せず、欠けている項目だけを追加します。秘密情報は生成された `.env.example` を `.env` へコピーして記入してください（`config/settings.toml` には書きません）。

## 操作面（MCP-first）

| 用途 | 接続 |
|---|---|
| エージェント（推奨） | MCP **`all`** — `abist-kb mcp serve all`（検索 + 管理 + jobs） |
| 管理のみ | MCP `kb-admin` — `search_kb` は含まない（KB 回答は `chat_ask` 経由） |
| 既存スキル互換 | `kb-download`／`kb-search`／`kb-visualize` |
| REST | `abist-kb api serve` → `/api/v1` |
| 長時間ジョブ | `abist-kb worker run` |
| 対話・スクリプト | CLI `abist-kb`（Typer + Rich） |

Web UI／TUI（NiceGUI／Textual）は削除済みです。`ui web`／`ui tui` コマンドはありません。
