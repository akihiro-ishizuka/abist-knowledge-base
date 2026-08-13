---
name: visualizing-kb
description: Use when turning knowledge-base content into a Manim 図解・アニメーション — e.g. 「蛇腹の同期フローを図解して」「この仕組みをアニメーションで見せて」「#4952 の手順を動画にして」. Covers the kb-visualize MCP tools (list_scene_kinds / render_scene / check_visualize_deps) and the rule that every beat must carry source_refs from kb-search. NOT for numeric charts/グラフ — use dataviz for that. NOT for searching itself — use searching-kb.
---

# ナレッジベースの内容を図解・アニメーション化する

## 原則

- **出典のない図を作らない。** SceneSpec の各 beat に source_refs（sources[].id）を付ける。
  sources には kb-search で得た path / 行番号 / content_hash を入れる
- **内容を創作しない。** KB に無いステップ・数値・閾値は描かない。
  出典のない数値（metric）はツール側でも拒否される

## 役割分担

| やりたいこと | 使うもの |
| --- | --- |
| 素材を探す・原文を読む | `searching-kb`（search_kb / get_document） |
| docs/ を最新化する | `downloading-kb-docs` |
| 図解・アニメ生成（**1シーン**） | **このスキル（kb-visualize MCP）** |
| 章立ての**動画**（複数シーン・テロップ・効果音・BGM） | `creating-kb-videos` スキル |
| KB の数値を動画の中でグラフにする | `chart` scene_kind（このスキル） |
| KB と関係ない数値の可視化 | `dataviz` スキル |

## ツール一覧

| ツール | 用途 | 応答 |
| --- | --- | --- |
| `list_scene_kinds` | シーン種別・テンプレ・必須フィールドの確認 | `{ok, count, scene_kinds}` |
| `render_scene` | SceneSpec を検証してレンダリング（最長10分ブロック） | 成功 `{ok, visualizationId, outputDir, outputs, manifestPath, warnings}` / 失敗 `{ok:false, code, errors}` |
| `check_visualize_deps` | Python / Manim / ffmpeg / フォントの診断 | `{ok, ready, python, manim, ffmpeg, fonts, messages}` |
| `list_visualizations` | 過去の成果物を新しい順に一覧（自己修復つき） | `{ok, count, total, visualizations}` |
| `get_visualization` | 成果物1件の詳細・出典検証結果・manifest | `{ok, visualization, sources, artifacts, manifest_drift}` |

出力は `mp4`（アニメ）か `png`（静止画）。予約済み・未実装の kind は無い。
**正確な一覧と制約は必ず `list_scene_kinds` で確認すること**（このドキュメントは要約）。

図解の kind（5種）に加え、動画の構成要素として使うカード系の kind がある:
`title` / `chapter` / `key_points` / `quote` / `summary` / `cta` / `ending` /
`code` / `formula` / `image` / `chart` / `thumbnail`。
単体でも描けるが、主用途は `creating-kb-videos` で組む章立て動画の1枚。

| scene_kind | 使いどころ | 主な beat |
| --- | --- | --- |
| `explain` | 箇条書きの解説 | `statement` / `metric` / `transition` |
| `flow` | 処理・作業フロー。分岐は `decision` + ラベル付き `transition` | `flow_step` / `decision` / `transition` |
| `timeline` | 議事録の経緯・決定事項の推移。`at` は原文表記のままでよい | `timeline_point` |
| `comparison` | 新旧・案の比較。観点 x 対象のマトリクス表 | `comparison_item` |
| `domain` | システム構成・用語の関係図。**静的な構造**（矢印に関係名が付き順序に意味がない）。時間的な流れは `flow` | `domain_entity` / `domain_relation` |

任意フィールド: `transition.label`（矢印ラベル。**出典必須**）/ `emphasis`（`normal`/`key`/`warn`）/
`quality`（`draft`/`standard`/`high`。既定 standard = 1920x1080）/ `metric.unit`（8文字以内）。
`flow` の配置は transition のグラフ構造から自動で決まる（分岐先は同じ列に縦並び）。
直線フローは従来と同じ配置だが、**分岐・合流・循環・孤立ノードを含む図は配置が変わる**。

上限: `timeline_point` は 2〜10 件、`domain` は entity 2〜10・relation 12 以内・group 4 種以内、`comparison` は side 2〜3 種・aspect 6 種以内・`text` 60 文字、
`decision.label` は 16 文字、`transition.label` は 40 文字。
`comparison` で該当のない組み合わせは書かなくてよい（表では「—」になる）。
長文は自動で折り返されるので `text` に手で改行を入れる必要はない。

## 手順

1. 初回のみ `check_visualize_deps`。`ready: false` なら `messages` のセットアップ手順を案内して中断
2. `search_kb` → `get_document` で素材を集める。
   **`index_stale: true` のときは行番号を信用せず、`downloading-kb-docs` で再ダウンロードするか
   `node tools/build-index.js` で再索引してから進める。**
   引用箇所は `get_document(path, start_line, end_line)` で読み、応答の **`range_hash` を控える**
   （これが SceneSpec `sources[].content_hash` になる）
3. `list_scene_kinds` でテンプレートと必須フィールドを確認する
4. SceneSpec を組み立てる。beat ごとに `source_refs` を付け、装飾テキストのみ `decorative: true`
5. `render_scene`。`ok: false` のときは `code` で分岐する:

| code | 対処 |
| --- | --- |
| `INVALID_SCENE_SPEC` | `errors` を読んで Spec を直す（再検索ではない） |
| `SOURCE_NOT_FOUND` / `SOURCE_HASH_MISMATCH` | 文書が変わっている。kb-search で取り直して content_hash を更新 |
| `CONCURRENT_RENDER` | 実行中のレンダリング完了を待って再試行 |
| `RENDER_TIMEOUT` / `RENDER_FAILED` | `stderrTail` を確認。beats を減らして再試行 |
| `PYTHON_NOT_FOUND` / `MANIM_NOT_FOUND` / `FFMPEG_NOT_FOUND` | `check_visualize_deps` で診断して導入を案内 |

6. 成果物の絶対パス（`outputs` / `manifestPath`）をユーザーに提示し、Cursor / IDE で開けるようにする

## 注意

- `warnings` は「出典不正で除外した beat」の一覧。空でないときは必ずユーザーに伝える
- クライアント側タイムアウトで応答が切れてもレンダリングは完走しうる。
  再レンダリングの前に **`list_visualizations`** で直近の成果物を確認する
  （ディスクと DB の差分は呼び出し時に自動整合される。詳細は `get_visualization`）
- `render_from_script`（任意 Python 実行）は無効。SceneSpec + 固定テンプレート経由のみ
- レンダリング環境: `.venv-visualize/`（`requirements-visualize.txt`、manim==0.19.0、Python 3.11）。
  セットアップは **`scripts\bootstrap-visualize.bat` 一発**。
  リポジトリ直下に作れば `KB_VISUALIZE_PYTHON` の指定は不要
  （手動なら `py -3.11 -m venv .venv-visualize` → `.venv-visualize\Scripts\python.exe -m pip install -r requirements-visualize.txt`。
  `python` は Store スタブなので必ず `py`）
- 日本語フォントは既定 Yu Gothic UI（`spec.font` か env `KB_VISUALIZE_FONT` で変更可）
