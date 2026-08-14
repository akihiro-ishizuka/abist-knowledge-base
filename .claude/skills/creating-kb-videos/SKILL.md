---
name: creating-kb-videos
description: Use when creating a full 社内動画 (複数シーン・テロップ・効果音・BGM) from docs/ — e.g. 「今期の活動報告を動画にして」「この手順書をチュートリアル動画にして」「新人研修用の動画を作って」「Shorts にまとめて」. Covers the kb-video MCP tools (validate_video_script / create_video_project / update_video_script / start_render_video / get_video_preview / run_video_qa / approve_video). NOT for a single 図解 — use visualizing-kb. NOT for searching itself — use searching-kb.
---

# ナレッジベースから社内動画を作る

## 原則

1. **出典のない事実を書かない。** 事実を述べる beat には `source_refs` を付ける。
   出典の `content_hash` は**書かない** —— あなたが宣言した `{path, start, end}` から
   サーバが実ファイルを読んで計算する。だから**行範囲が正しいこと**があなたの責任。
2. **音を切っても伝わる動画にする。** ナレーション音声は無い。台本の `narration.text` は
   **画面に焼き込まれるテロップ**であり、視聴者はこれを読む。音は効果音と BGM だけ。
3. **検証してから作る、作ってから描く。** `validate_video_script`（何も書かない）→
   `create_video_project`（保存）→ `start_render_video`（描画）の順。
   描画は数分かかるので、直せる不備は描く前に全部潰す。
4. **自分で承認しない。** `approve_video` は人間が内容を観てから押すもの。

## 役割分担

| やりたいこと | 使うもの |
| --- | --- |
| 素材を探す・原文を読む | `searching-kb`（search_kb / get_document） |
| docs/ を最新化する | `downloading-kb-docs` |
| 図解1枚だけ作る | `visualizing-kb` |
| **章立ての動画を作る** | **このスキル（kb-video MCP）** |
| テンプレートで描けない複雑な図 | 自分で HTML/SVG を描く → `tools/visualize/rasterize_diagram.py` → `image_assets` |

## ツール

| ツール | 用途 | 書き込み |
| --- | --- | --- |
| `validate_video_script` | 自分で書いた台本を検証。`{sceneId, path, code, message, fixHint}` が返る | しない |
| `create_video_project` | 台本を保存してプロジェクトを作る。`preview/storyboard-review.md` も出る | する |
| `update_video_script` | 台本を差し替える（全置換）。`changedSceneIds` が返り、state は draft へ戻る | する |
| `start_render_video` | 非同期ジョブとして描画。進捗は `job_status` | する |
| `get_video_preview` | 絵コンテ・コンタクトシート・シーン別成果物・QA 要約 | しない |
| `run_video_qa` | QA を再実行 | する |
| `approve_video` | 社内プレビュー承認（**人間が観てから**） | する |

シーン種別と制約の正本は `list_scene_kinds`（kb-visualize）。上限（要素数・文字数）は
そこに書いてあるので、迷ったら引く。

## 構成の型

### 活動・進捗報告

```
表紙(title) → 全体像(key_points) → 期間の流れ(timeline)
  → [章扉(chapter) → 案件の中身(key_points / comparison / chart)] × 章数
  → 見送り・課題(key_points, warning 音) → まとめ(summary) → エンドカード(ending)
```

- **一部の案件だけを並べない。** 「3案件やりました」ではなく活動全体が見える構成にする
- 数値（工数・件数・率）は `chart` で見せる。文字で並べるより速く伝わる
- 見本: `tests/video/fixtures/golden/activity_story.json` / `fiscal_year_story.json`

### 手順・チュートリアル

```
表紙(title) → 前提とゴール(key_points) → 全体の流れ(flow)
  → [手順ごとに explain / code / image] → 注意点(key_points, warning 音)
  → まとめ(summary) → 次の一歩(cta)
```

- 最初に `flow` で全体像を見せてから個別の手順に入る（迷子にさせない）
- 画面の操作は文字で書くより `image`（スクリーンショット）が速い

### 教育・研修

```
表紙(title) → 学習目標(key_points) → 概念の関係(domain)
  → [章扉(chapter) → 解説(explain) → 誤解と正しい理解(comparison)] × 章数
  → 原典(quote) → 確認(summary) → 次の教材(cta)
```

- `comparison` で「よくある誤解 / 正しい理解」を並べると定着しやすい
- 原文を示すときは要約せず `quote`（引用は言い換えない）

## テロップの書き方

- **読速は 4.5 字/秒。** 本編1シーン（12〜22秒）なら **80〜160字**が目安
- `on_screen_text` は 1 行 60 字以内、1 シーン 2 行まで（画面に出る見出し）
- **1シーン1メッセージ。** 2つ言いたいならシーンを分ける
- Markdown / HTML を残さない（`MARKUP_IN_VIDEO_TEXT`）
- 「凡例」「目次」「会議概要」のような資料の構造見出しは動画に出せない
  （`FORBIDDEN_VIDEO_HEADING`）。内容を表す見出しに書き換える
- 縦型（9:16）は 1 行 12 字。図の要素は **4 個まで**（`PORTRAIT_TOO_DENSE`）

## シーン種別は内容で選ぶ

| 内容 | kind |
| --- | --- |
| 時系列・経緯 | `timeline` |
| 対比・Before/After・案の比較 | `comparison` |
| 手順・処理の流れ | `flow` |
| 構造・用語の関係 | `domain` |
| 数値・実績 | `chart` |
| 同格の列挙 | `key_points` |
| 原文の提示 | `quote` / `code` |
| 章の切れ目 | `chapter` |
| 画面・持ち込みの図 | `image` |

尺予算で機械的に決めない。**その内容がどう見えると分かりやすいか**で選ぶ。

**見た目は `list_scene_kinds` の `preview` で確認できる**（`assets/scene-gallery/<kind>.png`）。
17種は説明文だけでは違いが分からない。**選ぶ前に見る。**

**同じ種別を4つ以上続けない。** 続くと「同じ絵がずっと出ている」動画になる。
`validate_video_script` が `composition` として測った数字を返すので、
`MONOTONOUS_RUN` / `SLIDESHOW_RISK` が出たら見せ方を変える。
図・グラフを伴わない面（title / chapter / key_points / quote / summary / cta / ending）
だけで 75% を超えると、動画である意味が薄くなる。

## 効果音は意味で書く

音源ファイルは指定しない（できない）。意味イベントだけを書く:

`intro` / `chapter_change` / `key_point` / `comparison_change` / `decision` /
`warning` / `success` / `error` / `outro` / `beat_reveal`（細かい刻み） /
`chart_draw`（グラフ描画）

アンカーは `scene.start` / `scene.end` / `chapter.enter` / `beat-N.reveal`。
目安は 1 シーンあたり 1〜2 件。BGM は用途（purpose）から自動で選ばれる。

## 持ち込み画像・自分で描いた図

1. 図を HTML か SVG で描く（テーマに合わせる: 背景 `#0d1b2a` 系 / アクセント `#5b9bd5` /
   フォント `Yu Gothic UI`。動画側と見た目が揃う）
2. `tools/visualize/rasterize_diagram.py --input fig.html --output fig.png` で PNG 化
   （ネットワークは遮断される。動画解像度の2倍で出る）
3. `create_video_project` の `image_assets` に `{id, path, license, caption}` で登録
4. 台本の `image` beat から `asset_id` で参照する

**license は必須。** 出所の分からない画像は載せられない。
図の中で事実を述べるなら、そのシーンにも `source_refs` を付ける。

## 手順

1. **調べる** —— `search_kb` で素材を探し、`get_document` で原文と行範囲を確認する。
   引用する箇所の `path` / `start_line` / `end_line` をメモする
2. **型を選ぶ** —— 上の3種から。尺は `target_duration_sec` で宣言する
3. **台本を書く** —— ScriptDraft 形式（`{title, scenes[], sound_events[]}`）。
   **骨格を出してくれる仕組みは無い**（題材から機械的に組めるものではない）。
   見本は `tests/video/fixtures/golden/*.json`
4. **要件を宣言する** —— `story_requirements` に、必ず触れるテーマ・必須シーン種別・
   シーン数の範囲・出典の期間を書く。**自分で宣言した要件を QA が執行する**
5. **検証ループ** —— `validate_video_script` → エラーの `fixHint` を読んで直す →
   `ok: true` になるまで繰り返す
6. **作る** —— `create_video_project`（`script` を渡す）
7. **絵コンテを自分で読む** —— `preview/storyboard-review.md` を開き、
   - 音を切ったまま全シーンの意味が通るか
   - 同じことを2回言っていないか
   - 出典が対象期間に収まっているか
8. **描く** —— `start_render_video` → `job_status` で待つ
9. **見る** —— `get_video_preview` でコンタクトシートを確認。直すシーンがあれば
    `update_video_script`（変わったシーンだけ `changedSceneIds` に出る）
10. **QA** —— `run_video_qa`。FAIL があれば直す
11. **人に渡す** —— `preview/MANUAL_PUBLISH.md` を案内する。
    **`approve_video` は押さない**（人間が観てから）

## よくあるエラーと直し方

| code | 直し方 |
| --- | --- |
| `INSUFFICIENT_SEMANTIC_CONTENT` | 表示文に記法断片・禁止見出しが残っている。`errors` の該当箇所を書き直す |
| `MONOTONOUS_RUN` | 同じ種別が続きすぎ。`preview` を見て別の見せ方に替える |
| `SLIDESHOW_RISK` | 文字を並べる面ばかり。図で見せられる内容を探す |
| `TOO_FEW_SCENE_KINDS` | 種別が偏っている。`preview` で選択肢を見る |
| `PORTRAIT_TOO_DENSE` | 縦型の図に要素を詰めすぎ。シーンを分ける |
| `MISSING_STORY_TOPIC` | 自分で宣言した必須テーマに触れていない。触れるか、要件から外す |
| `UNKNOWN_IMAGE_ASSET` | `image_assets` に登録してから `asset_id` で参照する |
| `INSUFFICIENT_CONTENT_FOR_DURATION` | 素材に対して尺が長い。尺を縮めるか題材を足す（**水増ししない**） |
| `NO_RESOLVABLE_INPUT` | `kb_paths` が `docs/` 配下にあるか確認する |

エラーには必ず `fixHint` が付く。まずそれを読む。
