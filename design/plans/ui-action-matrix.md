# UI 操作マトリクス

> [!IMPORTANT]
> 正本（実装契約）は [`design/ui-action-matrix.yaml`](../ui-action-matrix.yaml) です。
> この Markdown は表示用であり、編集しても実装契約は変わりません。

設計書 §7.1 の9画面について、各操作を Web・TUI・API のどこで提供するかを示します。
`破壊的` が「はい」の操作は、実行前の確認を必須とします。

| 画面 | 操作キー | 表示名 | 破壊的 | Web | TUI | API |
|---|---|---|---:|:---:|:---:|:---:|
| ダッシュボード | — | 参照のみ | — | — | — | — |
| ソース・バッチ | `source_add` | ソース追加 | いいえ | ✓ | — | ✓ |
| ソース・バッチ | `source_edit` | ソース編集 | いいえ | ✓ | — | ✓ |
| ソース・バッチ | `source_remove` | ソース削除 | はい | ✓ | ✓ | ✓ |
| ソース・バッチ | `source_test_connection` | ソース接続テスト | いいえ | ✓ | ✓ | ✓ |
| ソース・バッチ | `batch_add` | バッチ追加 | いいえ | ✓ | — | ✓ |
| ソース・バッチ | `batch_edit` | バッチ編集 | いいえ | ✓ | — | ✓ |
| ソース・バッチ | `batch_remove` | バッチ削除 | はい | ✓ | ✓ | ✓ |
| ソース・バッチ | `batch_run` | バッチ実行 | いいえ | ✓ | ✓ | ✓ |
| ジョブ | `job_cancel` | ジョブキャンセル | はい | ✓ | ✓ | ✓ |
| ジョブ | `job_retry` | ジョブ再試行 | いいえ | ✓ | ✓ | ✓ |
| 文書 | `document_update_metadata` | 文書メタデータ編集（`status`/`document_type` のみ） | いいえ | ✓ | ✓ | ✓ |
| 文書 | `document_delete` | 文書削除 | はい | ✓ | ✓ | ✓ |
| 検索 | `search_run` | 検索実行 | いいえ | ✓ | ✓ | ✓ |
| チャット | `chat_start` | 会話開始 | いいえ | ✓ | ✓ | ✓ |
| チャット | `chat_ask` | 質問 | いいえ | ✓ | ✓ | ✓ |
| チャット | `chat_history` | 会話履歴 | いいえ | ✓ | ✓ | ✓ |
| 可視化 | `visualization_validate` | SceneSpec 検証 | いいえ | ✓ | ✓ | ✓ |
| 可視化 | `visualization_submit_render` | レンダリング投入 | いいえ | ✓ | ✓ | ✓ |
| 可視化 | `visualization_deps` | 可視化依存関係診断 | いいえ | ✓ | ✓ | ✓ |
| 品質 | `quality_run_integrity` | 整合性監査 | いいえ | ✓ | ✓ | ✓ |
| 品質 | `quality_run_duplicates` | 重複監査 | いいえ | ✓ | ✓ | ✓ |
| 品質 | `quality_run_contradictions` | 矛盾候補監査 | いいえ | ✓ | ✓ | ✓ |
| 品質 | `quality_run_search_quality` | 検索品質監査 | いいえ | ✓ | ✓ | ✓ |
| 品質 | `quality_run_backfill_metadata` | メタデータ補完監査（`apply=false` / dry-run） | いいえ | ✓ | ✓ | ✓ |
| 設定・診断 | — | 参照のみ | — | — | — | — |

## 意図的な省略

`source_add`、`source_edit`、`batch_add`、`batch_edit` はフォーム入力が TUI では煩雑なため、
Web/API に限定します。その他の操作は Web・TUI・API の全てで提供します。
