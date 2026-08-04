# Task 5 実装報告 — TUI チャット・品質の Service 配線

## 実装内容

- `_render_stub` が `available=True` のチャット・品質を実画面へ振り分けるよう修正した。
- TUI チャットを `screens.chat_start` / `screens.chat_ask` に接続し、回答、引用、
  引用検証警告を表示するようにした。
- TUI 品質画面から整合性、重複、矛盾、メタデータ補完の4監査を実行できるようにした。
- メタデータ補完は dry-run のみとし、`apply=True` はハード拒否、適用は CLI 専用で
  あることを画面に明記した。
- チャット・品質が利用可能な場合に「利用不可」を表示しない回帰テストを追加した。

## テスト

- `uv run pytest -q tests/ui/test_tui.py tests/ui/test_web_pages.py`
  - 42 passed
- `uv run ruff check .`
  - All checks passed
- チャット回帰テストでは回答、引用、`citation_warnings` の警告表示まで確認した。
- API キー未設定時のみ「チャットは利用できません」を期待する既存 Web テストも通過した。

## 留意事項

- NiceGUI 依存ライブラリ由来の `pkgutil.find_loader` 非推奨警告が3件出るが、
  既存依存由来でありテスト結果には影響しない。
- 指示どおり、既知の ANSI purity failure を含むフルスイートは実行対象外とした。
