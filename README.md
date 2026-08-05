# ABIST Knowledge Base

Python 移植版。設計の正は [`design/system-design.md`](design/system-design.md)。操作契約の正本は [`design/ui-action-matrix.yaml`](design/ui-action-matrix.yaml)。

## 操作面（MCP-first）

| 用途 | 接続 |
|---|---|
| エージェント管理（推奨） | MCP `kb-admin` または `all` — `abist-kb mcp serve kb-admin`／`all` |
| 既存スキル互換 | `kb-download`／`kb-search`／`kb-visualize` |
| REST | `abist-kb api serve` → `/api/v1` |
| 長時間ジョブ | `abist-kb worker run` |
| 対話・スクリプト | CLI `abist-kb`（Typer + Rich） |

Web UI／TUI（NiceGUI／Textual）は削除済みです。`ui web`／`ui tui` コマンドはありません。
