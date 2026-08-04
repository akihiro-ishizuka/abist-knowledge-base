# Task 4 実装報告 — 文書のメタデータ編集と削除

## 実装内容

- Web 文書詳細に `status` / `document_type` の編集フォームを追加した。
- Web のその他メタデータを読み取り専用カードとして明示した。
- Web に対象パスを先に示す削除確認と、成功時の一覧遷移を追加した。
- TUI 文書詳細に同等の編集入力、結果表示、確認付き削除を追加した。
- Web / TUI とも `screens.document_update_metadata` /
  `screens.document_delete` のみを呼び出す画面配線とした。
- エラー時はコードとメッセージを表示する。

## テスト

- `uv run pytest -q tests/ui/test_web_pages.py tests/ui/test_tui.py`
  - 40 passed
- `uv run ruff check .`
  - All checks passed
- 削除確認で「いいえ」を選んだ場合について、Web / TUI の両方で文書が残り、
  `audit_events` の件数が増えないことを確認した。

## 留意事項

- NiceGUI 依存ライブラリ由来の `pkgutil.find_loader` 非推奨警告が3件出るが、
  既存依存由来でありテスト結果には影響しない。
- 指示どおり、既知の ANSI purity failure を含むフルスイートは実行対象外とした。
