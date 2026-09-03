# abist-knowledge-base

社内ナレッジベース。esa 記事・議事録・仕様・メモを `docs/` に集約し、検索・可視化・動画化する。

## 日本語文書は natural-japanese を通す

**このリポジトリで日本語の文章を書く・直すときは、必ず `natural-japanese` スキルを使う。**

対象は議事録・レポート・社内ガイド・ナレッジ記事・esa 記事・提案書・メールなど、
人が読む散文すべて。コミットメッセージ、コード内コメント、
設定ファイル、コードそのものは対象外。

使い分け:

| 場面 | モード |
|---|---|
| 日常の文書（既定） | クイック。lint 1回 + 通読で仕上げる |
| 対外・経営向け、1万字超、「しっかり」と言われたとき | フル |
| 書き換えず診断だけしたいとき | `/natural-japanese score <ファイル>` |

`writing-teirei-minutes` や `promoting-knowledge` で成果物を書き終えたら、
esa へ publish する前に natural-japanese を通すこと。
順序は「内容を固める → natural-japanese で文章を整える → publish」。

各スキルが持つ固有ルール（定例資料の平易化テーブル、`**` の後ろの空白など）は
natural-japanese では検出できないドメイン知識なので、両方適用する。

### lint の実行

`uv` 必須。Git Bash では出力が CP932 で描画され文字化けするので PowerShell を使う:

```powershell
$env:PYTHONIOENCODING='utf-8'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
uv run .agents/skills/natural-japanese/scripts/lint.py <対象ファイル>
```

検出があると終了コードが非ゼロになる。これは異常ではなく仕様。

### セットアップ（clone 直後）

スキル本体は `.agents/skills/` に含まれているので取得は不要。
Claude Code から見えるようにリンクだけ張る:

```
npx skills sync
```

`.claude/skills/natural-japanese` は絶対パスのジャンクション（Windows）または
シンボリックリンクで、環境ごとに作り直すため gitignore してある。
