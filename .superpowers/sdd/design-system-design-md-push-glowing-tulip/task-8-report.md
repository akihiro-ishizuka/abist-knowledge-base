# Task 8 実施報告 — 操作可能性の横断契約テスト

## 結果

Task 8 を完了した。`design/ui-action-matrix.yaml` を正本として、Web・TUI・API の各操作が実際の画面またはルートから到達できること、破壊的操作の確認拒否時に副作用がないこと、意図的な surface 省略に理由があること、検査自体が非空虚であることを横断テストで固定した。

実装コミット: `544aca6 test(ui): enforce cross-surface action contracts`

## 主な変更

- `tests/ui/test_ui_actions_contract.py` を追加（91 tests）。
  - YAML から全操作と対象 surface を列挙する。
  - Web は実ページを開いてボタン・入力・marker を検査し、チャットなどは操作結果まで確認する。
  - TUI は画面遷移後の Button/Input/Static と操作結果を検査する。
  - API は FastAPI に登録された method/path を検査する。
  - `*_omitted_reason` の必須化と、surface が空の孤児操作を禁止する。
  - 破壊的操作（source/batch 削除、job cancel、document delete）について Web/TUI の拒否時無副作用と API の未確認 400 を検査する。
  - registry から一件削除した場合、および偽の route/button/widget selector を使った場合に検査が失敗することを確認する。
- 正本 YAML と派生 Markdown から、実装に存在しない `quality_run_search_quality` を削除し、監査を4種に統一した。
- `job_cancel` に keyword-only `confirmed` を追加した。
  - Web/TUI は確認後のみ `confirmed=True` を渡す。
  - API は未指定時に `400 INVALID_INPUT` となり、ジョブ状態を変更しない。
- Web のチャット・可視化に到達性検査用 marker を追加した。
- TUI チャットログを追記式にし、複数往復の履歴が画面上に残るよう修正した。
- YAML を直接読むテスト依存として PyYAML を dev dependency に明示した。
- 全リポジトリを現行 Ruff formatter で整形し、format check を通した。

## 検証

- `uv run pytest -q tests/ui/test_ui_actions_contract.py`
  - `91 passed`
- `uv run pytest -q tests/ui/test_api.py tests/ui/test_tui.py tests/ui/test_web_pages.py tests/ui/test_view_models.py`
  - `103 passed`
- 最終統合再実行:
  - `uv run pytest -q tests/ui/test_ui_actions_contract.py tests/ui/test_api.py tests/ui/test_tui.py tests/ui/test_web_pages.py tests/ui/test_view_models.py`
  - `194 passed`
- `uv run ruff check .`
  - passed
- `uv run ruff format --check .`
  - `266 files already formatted`
- `uv run pytest -q`
  - `1908 passed, 5 skipped, 1 failed`
  - 失敗は許容指定済みの既知 ANSI purity: `tests/console/test_output_purity.py::test_no_ansi_in_rich_mode_when_color_system_is_none`
  - multiprocess lease timing failureは今回発生しなかった。

## 懸念・補足

- Full pytest の唯一の失敗は Task 8 と無関係な既知 ANSI purity であり、今回の変更による追加失敗はない。
- Ruff の非互換な未整形箇所が既存ファイルにも残っていたため、Task 8 の変更ファイル以外にも機械的な format-only 差分を含む。

## Round 1/5 修正

- TUI `search_run` の到達性チェックを、入力欄の存在確認から実操作の検証へ強化した。検索サービスを決定的な結果に差し替え、検索語を入力して Enter を送信し、`#search-results` に期待する文書パスが描画されることを確認する。入力ハンドラを削除すると失敗する。
- API の破壊的操作確認テストは `API_DECLINE_CHECKS.get()` を使い、API surface を意図的に省略した操作を skip するよう変更した。`api_omitted_reason` 付きの正当な省略で `KeyError` にならない。
- 検証:
  - `uv run pytest -q tests/ui/test_ui_actions_contract.py`: `91 passed`
  - `uv run ruff check .`: passed
  - `uv run ruff format --check .`: `266 files already formatted`
