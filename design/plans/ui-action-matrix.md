# UI 操作マトリクス

> [!IMPORTANT]
> 正本（実装契約）は [`design/ui-action-matrix.yaml`](../ui-action-matrix.yaml) です。
> この Markdown は表示用であり、編集しても実装契約は変わりません。

MCP-only UI cutover 後の面は **MCP** と **API**（必要に応じて CLI）です。
Web / TUI は削除済み。破壊的操作は実行前の確認を必須とします。

## 推奨接続

| 用途 | 接続 |
|---|---|
| エージェント（推奨） | MCP **`all`** — `abist-kb mcp serve all`（検索 + 管理 + jobs） |
| 管理のみ | MCP `kb-admin` — `search_kb` は含まない（KB 回答は `chat_ask` 経由） |
| 既存スキル互換 | `kb-download`／`kb-search`／`kb-visualize` |
| REST | `abist-kb api serve` → `/api/v1` |
| 長時間ジョブ | `abist-kb worker run` |

| 画面 | 操作キー | 表示名 | 破壊的 | MCP | API |
|---|---|---|---:|:---:|:---:|
| ダッシュボード | — | 参照のみ | — | — | — |
| ソース・バッチ | `source_add` | ソース追加 | いいえ | ✓ | ✓ |
| ソース・バッチ | `source_edit` | ソース編集 | いいえ | ✓ | ✓ |
| ソース・バッチ | `source_remove` | ソース削除 | はい | ✓ | ✓ |
| ソース・バッチ | `source_test_connection` | ソース接続テスト | いいえ | ✓ | ✓ |
| ソース・バッチ | `batch_add` | バッチ追加 | いいえ | ✓ | ✓ |
| ソース・バッチ | `batch_edit` | バッチ編集 | いいえ | ✓ | ✓ |
| ソース・バッチ | `batch_remove` | バッチ削除 | はい | ✓ | ✓ |
| ソース・バッチ | `batch_run` | バッチ実行 | いいえ | ✓ | ✓ |
| ジョブ | `job_cancel` | ジョブキャンセル | はい | ✓ | ✓ |
| ジョブ | `job_retry` | ジョブ再試行 | いいえ | ✓ | ✓ |
| 文書 | `document_update_metadata` | 文書メタデータ編集（`status`/`document_type` のみ） | いいえ | ✓ | ✓ |
| 文書 | `document_delete` | 文書削除 | はい | ✓ | ✓ |
| 検索 | `search_run` | 検索実行 | いいえ | ✓ | ✓ |
| チャット | `chat_start` | 会話開始 | いいえ | ✓ | ✓ |
| チャット | `chat_ask` | 質問 | いいえ | ✓ | ✓ |
| チャット | `chat_history` | 会話履歴 | いいえ | ✓ | ✓ |
| 可視化 | `visualization_validate` | SceneSpec 検証 | いいえ | ✓ | ✓ |
| 可視化 | `visualization_submit_render` | レンダリング投入 | いいえ | ✓ | ✓ |
| 可視化 | `visualization_deps` | 可視化依存関係診断 | いいえ | ✓ | ✓ |
| 品質 | `quality_run_integrity` | 整合性監査（読取専用） | いいえ | ✓ | ✓ |
| 品質 | `quality_apply_integrity_updates` | 整合性監査の DB 更新適用 | はい | ✓ | — |
| 品質 | `quality_run_duplicates` | 重複監査 | いいえ | ✓ | ✓ |
| 品質 | `quality_run_contradictions` | 矛盾候補監査 | いいえ | ✓ | ✓ |
| 品質 | `quality_preview_backfill_metadata` | メタデータ補完 preview（dry-run） | いいえ | ✓ | — |
| 品質 | `quality_apply_backfill_metadata` | メタデータ補完の適用 | はい | ✓ | — |
| 品質 | `quality_run_backfill_metadata` | メタデータ補完監査（API: `apply=false` 既定） | いいえ | — | ✓ |
| 設定・診断 | — | 参照のみ | — | — | — |

## MCP 到達先

大半の管理操作は `kb-admin` ツール（推奨接続は `all`）。例外:

- `job_cancel` → jobs MCP の `cancel_job`
- `search_run` → kb-search の `search_kb`
- `visualization_submit_render` → jobs MCP の `start_render_scene`
- `visualization_deps` → kb-visualize の `check_visualize_deps`

品質の書込系は MCP では preview / apply に分離:

- `quality_run_integrity` → 読取専用
- `quality_apply_integrity_updates` → 破壊的（`confirmed` + `confirm_action`）
- `quality_preview_backfill_metadata` → dry-run
- `quality_apply_backfill_metadata` → 破壊的（`confirmed` + `confirm_action`）
- API の `quality_run_backfill_metadata` は MCP には無い（API 専用）

到達性は `tests/mcp/test_matrix_actions_contract.py` がマトリクスを正本として検査する。
