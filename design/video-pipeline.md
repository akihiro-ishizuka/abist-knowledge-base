# 社内動画生成パイプライン — 契約

> KB の Markdown から社内配布用の動画を生成する仕組みの契約。
> ロードマップは `C:\Users\a1199118\.claude\plans\youtube-video-automation.md`。
> 本書は**実装が従う契約**（入力・出力・エラー・責務分担）だけを定める。

## 位置づけ

既存の単一 `SceneSpec` → Manim 1本レンダー（`reports/visualizations/`）は**変更しない**。
その上位に `VideoProjectSpec` を置き、複数シーンを結合して `reports/videos/<id>/` へ出す。

**自動投稿はしない。** 会社の YouTube へ人間が手動登録できる成果物を作るところまで。

## 入力契約

主要入力は **`docs/` 配下の Markdown 文書**。esa 記事はその由来の一つ。

### 4つの指定経路

| 入力 | 扱い | `selection` | 使用要求 |
|---|---|---|---|
| `kb_paths[]` | 明示主入力 | `explicit_primary` | 各文書の使用が必須 |
| `esa_posts[]` | 明示主入力（任意の補助経路） | `explicit_primary` | 解決できた各文書の使用が必須 |
| `kb_directories[]` | 主入力コレクション | `collection_candidate` | 各ディレクトリから1件以上 |
| `kb_queries[]` | 補完候補 | `supplemental` | 使用必須ではない |

**ディレクトリ配下の全件を使用必須にしてはならない。** `docs/` には約 85,000 件の
Markdown があり、ディレクトリによっては数百〜数万件を含む。ディレクトリは
「そこから選抜する候補集合」として扱う。

### `ResolvedInput`（解決後の統一形）

```json
{
  "path": "knowledge/manuals/a.md",
  "content_hash": "<64hex>",
  "selection": "explicit_primary",
  "require_usage": true,
  "origin": {
    "type": "kb_path",
    "selector": null,
    "esa_url": null,
    "esa_post_id": null
  }
}
```

| フィールド | 意味 |
|---|---|
| `path` | `docs/` 相対の POSIX パス（正規化後。唯一の識別子） |
| `content_hash` | `get_document` の `range_hash`（`domain/line_range.py` の契約） |
| `selection` | `explicit_primary` / `collection_candidate` / `supplemental` |
| `require_usage` | その1件の使用が必須か。`explicit_primary` のみ `true` |
| `origin.type` | `kb_path` / `kb_directory` / `kb_query` / `esa_post` |
| `origin.selector` | コレクション由来のとき、どの指定から来たか |

同一 `path` は1件に畳む。強い方を採用:
`explicit_primary` > `collection_candidate` > `supplemental`。

### パスの正規化と封じ込め

`docs/` 配下へ正規化し、外へ出るものは拒否する。
`domain/scene_spec._is_safe_relative_path` と
`application/visualization/source_verifier._resolve_inside_docs` と同じ多層防御を使う。

| 入力 | 扱い |
|---|---|
| `knowledge/manuals/a.md` | そのまま |
| `docs/knowledge/manuals/a.md` | 先頭 `docs/` を剥がす（利便性のため受け付ける） |
| `..` を含む / 絶対パス / ドライブ指定 | 拒否 |
| `docs/` 外を指すシンボリックリンク | 拒否（`resolve()` 後に再確認） |
| `.md` / `.markdown` 以外 | 対象外 |

リポジトリルート直下の `design/` は KB コーパス外なので拒否する。
題材にしたい場合は先に `docs/` 配下へ取り込む。

### コレクションの選抜（決定的）

1. `selector` を再帰走査し `.md` / `.markdown` を候補化
2. 既存索引で絞り込み（**全件の本文を LLM へ渡さない**）
3. `(score 降順, path 昇順)` で並べる（同点は path 辞書順で一意 = 決定的）
4. `max_docs_per_directory`（既定 20）/ `max_total_candidates`（既定 60）で切る
5. 選抜後の文書だけ本文と `content_hash` を読む

候補総数・選抜件数・採用理由・除外理由を `inputs-manifest.json` に記録する。

## エラー契約

エラー形は SceneSpec と揃える。

```python
{"ok": False, "code": "<CODE>", "errors": [{"path": str, "code": str, "message": str}]}
```

| code | 分類 | 挙動 |
|---|---|---|
| `NO_RESOLVABLE_INPUT` | 共通・失敗 | 1件も解決できない。台本生成へ進まない |
| `INVALID_INPUT_PATH` | 失敗（個別） | `docs/` 外・`..`・絶対パス・拡張子違い。全滅なら `NO_RESOLVABLE_INPUT` |
| `INVALID_VIDEO_SPEC` | 失敗 | `VideoProjectSpec` の検証エラー |
| `EMPTY_DIRECTORY` | warning | ディレクトリ配下に Markdown が無い |
| `INPUT_COLLECTION_TRUNCATED` | warning | 選抜が上限で切られた |
| `ESA_POST_NOT_IN_KB` | warning | 未取り込みの esa URL/ID |
| `PRIMARY_INPUT_UNUSED` | warning（QA） | `kb_paths` / 解決済み `esa_posts` が本編未使用 |
| `PRIMARY_COLLECTION_UNUSED` | warning（QA） | ディレクトリから1件も採用されない |
| `RENDER_*` | 失敗 | purring の既存コードをそのまま使う |

`SUPPLEMENTAL_INPUT_UNUSED` は**定義しない**（補完は使われなくてよい）。

## 同期 / 非同期の責務

| 処理 | 経路 | 理由 |
|---|---|---|
| 入力解決・spec 検証・プロジェクト作成 | **同期**（MCP / CLI） | 短時間。即座にフィードバックしたい |
| カタログ照会（一覧・詳細） | **同期** | 読み取りのみ |
| 台本生成（LLM） | 同期可（短時間）だが長引くならジョブ | provider 次第 |
| **多シーンレンダリング・音声合成・結合** | **ジョブのみ** | 長時間。`render` リースを占有する |

**動画レンダリングで同期 MCP `render_scene` を使わない。**
`design/notes/visualize-recovery-known-issues.md` #1 のとおり、同期パスが `render` リースを
長時間保持すると worker のキュー消費全体が止まる。動画は多シーンを連続描画するため
影響が拡大する。ジョブ経路（`ResourceKind.RENDER`）を1本取り、内部で直列に描画する。

## 成果物ツリー

```
reports/videos/<UTC-ts>-<slug>-<4hex>/
  project-spec.json         # VideoProjectSpec（正本）
  inputs-manifest.json      # 入力解決と選抜の記録
  state.json                # 進行状態（再開用）
  scenes/<scene_id>/        # シーンごとの中間成果物
  output.mp4                # 最終成果物
  manifest.json             # 成果物の sha256 / 生成環境
  citations.json            # 出典
```

purring の `reports/visualizations/` と**同じ命名規則**（`<UTC>-<slug>-<4hex>`、`:` を含まない）
を使い、`artifact_store` の関数を再利用する。

## 後方互換の約束

1. 既存 `render_scene` / SceneSpec 1.0 / `reports/visualizations/` を変更しない
2. `tests/fixtures/**` を手編集しない。新ツールは allowlist で純増分として宣言する
3. 動画機能を使わない利用者に影響を与えない（新テーブル・新ディレクトリのみ）

## 尺の逆算（Phase 7 以降）

`format.target_duration_sec` から構成を**逆算**する（`application/video/duration_planner.py`）。

```
目標尺 → シーン数 → 章数 → 役割ごとのシーン数
      → カード固定尺 / 本編配分尺 → ナレーション目標文字数
```

- 日本語の読み上げ速度は `tts_provider.CHARS_PER_SECOND`（6.5 文字/秒）が唯一の正本
- **表紙・章扉・エンドカードは固定尺**（既定 8 秒）。伸ばしても情報が増えないため、
  余った尺は本編へ配分する。一律に割ると完成尺が目標を割る
- 関連情報だけで目標尺に届かない場合は `INSUFFICIENT_CONTENT_FOR_DURATION` を返して
  **止まる**。入力文書を機械的に増やしたり、説明を言い換えて水増ししたりはしない

映像の尺は `min_duration_sec`（SceneSpec の任意フィールド）で下限を与える。
**描画前に決める必要がある** —— 既に描き終わった映像は伸ばせないため、
ナレーションが映像より長いと音声の末尾が切れる。

## 成果物ツリー（Phase 7〜12 追加分）

```
reports/videos/<id>/
  project-spec.json      # VideoProjectSpec（正本）
  inputs-manifest.json   # 入力解決と選抜の記録
  state.json             # 進行状態（再開用）
  scenes/<scene_id>/     # シーン成果物 + resume-digest.txt（再開判定）
  captures/              # 画面キャプチャ（任意）+ manifest.json
  workspace/             # キャプチャ対象の clone 先（読み取り専用）
  audio/                 # ナレーション + sound-cues.json
  subtitles/             # narration.srt / narration.vtt
  preview/
    candidate-thumbs/    # 章ごとの候補フレーム
    MANUAL_PUBLISH.md    # 手動アップロード手順
  output.mp4
  output-with-audio.mp4
  thumbnail.png
  video-metadata.json    # 手動投稿パック（チャプター・説明文・タグ）
  citations.json         # 出典 + 効果音の帰属 + キャプチャの commit SHA
  qa-report.json         # 自動検査 + human_required
  distribution-report.json
  approval.json          # 成果物ハッシュにバインドした承認
  manifest.json
```

## 画面キャプチャの安全性（Phase 7）

- **既定で無効。** `ABIST_KB_VIDEO_CAPTURE_ENABLED=1` を明示したときだけ動く
- **起動コマンドは MCP / API / CLI / LLM から渡せない。** 受け付けるのは
  `config/capture-profiles.json` に運用者が登録した**プロファイル名**だけ
- git は allowlist で読み取り専用。`ext::` / `file://` / `http://` / `git://` は拒否
- `resolved_commit_sha`・成果物 `sha256`・マスク件数を manifest へ記録する
- マスク対象が見つからない shot は**落とす**（未マスクのまま出さない）
- 撮影プロセスは木ごと終了させる（`terminate` より先に `taskkill /F /T`）

## 実装しないもの（将来バックログ）

YouTube API / OAuth / 自動アップロード / 公開予約 / AI 音楽生成。
`request_public_review` はステータスを記録するだけで、外部へは何も送らない。
