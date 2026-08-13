# ABIST Knowledge Base

Python 移植版。設計の正は [`design/system-design.md`](design/system-design.md)。操作契約の正本は [`design/ui-action-matrix.yaml`](design/ui-action-matrix.yaml)。

## 初回セットアップ

Windows では `scripts\bootstrap.bat` で一括実行できます（先に `docs\` へ `.md` を置いてから実行）。

```bat
scripts\bootstrap.bat
scripts\bootstrap.bat --dry-run-register
scripts\bootstrap.bat --skip-embed
```

手動で進める場合:

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

## 動画生成

`docs/` の Markdown から、章立ての社内動画（Manim シーン列 + テロップ + 効果音 + BGM）を
作れます。成果物は `reports/videos/<id>/` に置かれる**手動アップロード用の一式**で、
外部への自動投稿は行いません。

**台本はエージェントが書きます。** 題材から台本を機械生成する経路はありません
（何を語りどう見せるかは書き手の判断で、見出しや箇条書きを拾って組める類のもの
ではありませんでした）。MCP `all` の `validate_video_script` →
`create_video_project` → `start_render_video` → `get_video_preview` →
`run_video_qa` が作成ループで、書き方は
`.claude/skills/creating-kb-videos/SKILL.md` にあります。

書いた台本を手元で描くときは CLI:

```bash
abist-kb video render --script script.json --min-sec 120 --max-sec 240
abist-kb video qa <video_id>          # QA を再実行
abist-kb video show <video_id>        # 成果物パスと配布判定
```

台本ファイルは `{title, inputs, script, story_requirements?, image_assets?}`。
見本は `tests/video/fixtures/golden/activity_story.json`。

設計の要点:

- **ナレーション音声は無い。** 台本の文はテロップとして映像へ焼き込まれ、
  音を切ったままでも内容が伝わる。音は効果音と BGM だけ
- **出典必須。** 事実を述べる beat には出典が要り、`content_hash` は実ファイルから計算される
- **効果音・BGM は社内制作**（`scripts/generate-video-*.py` で決定的に再生成できる）
- 16:9 と 9:16（Shorts）、`draft` / `standard` / `high`（2560x1440・60fps）に対応
