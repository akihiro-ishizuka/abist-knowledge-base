# M6〜M10 残マイルストーン 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。各マイルストーン着手時に該当節を `design/plans/M<n>-*.md` へ展開してから実装する。

**Goal:** UI 3種(Web/デスクトップ/TUI)、チャット・監査・可視化、旧データ移行、並行稼働と切替、旧システム廃止までを完了する。

**Context:** 親計画 `C:\Users\a1199118\.claude\plans\design-system-design-md-push-glowing-tulip.md`。設計の正は [../system-design.md](../system-design.md)。M0〜M5 で確立した前提は各マイルストーンの計画書と `tests/fixtures/PROVENANCE.md` にある。

## 全マイルストーン共通の申し送り(M0〜M5 で確定した事実)

着手前に必ず確認すること。いずれも実測または実行で確かめた事実であり、設計書の記述より優先する。

| 事項 | 内容 |
|---|---|
| `AppTyper` | サブコマンド群は `typer.Typer()` ではなく `AppTyper()` で作る。`add_typer()` は親の設定を子へ遡及適用しないため、素の Typer で作ったグループは `AppError` が終了コードへ変換されず、実プロセスとテストで挙動が食い違う |
| リース | 側効果のあるループでは `run.check_lease()` を呼ぶ。リースを失った後も書き続ける事故が実証済み(20回中16回が奪取後) |
| ジョブ状態 | 一部失敗は `PARTIAL`。CLI 終了コードだけでなくジョブ状態を読む消費者(MCP・UI)がいる |
| 埋め込みモデル識別子 | ハッシュ用は `Xenova/multilingual-e5-small`、ローダ用は `intfloat/multilingual-e5-small`。**別物として共存させる** |
| 埋め込み再利用 | §11.2 のゲートは**不合格**(100件全て 0.999 未満、最小 0.99253)。M8 は全件再生成が既定経路。実測 8.84件/秒 |
| 旧DBは完全な台帳でない | `sync-state.sqlite` はバックフィル結果。`docs/knowledge/catiadoc` の約1,300ファイルはどちらの旧DBにも未登録。**移行棚卸しはファイルシステムを正とする** |
| 参照コーパス | `reference-index.sqlite` は `knowledge/B32doc` のみ・埋め込み0件。列値は `source="manual"`, `document_type="reference"`, `status="active"`, `category="html"`(実測) |
| 旧リポジトリ | **絶対に変更しない。** SQLite はサンドボックスコピー方式のみ(`readonly:true` でも `-shm` の mtime が動く) |
| `range_hash` | `domain/line_range.py` の1実装のみ。get_document・SceneSpec・source-verifier の共通契約 |
| MCP stdout | JSON-RPC 専用。`mcp serve` は自前 Presenter を stderr へ向ける |

---

# M6 NiceGUI Web/デスクトップ・Textual TUI・FastAPI/SSE

**Goal:** 設計書 §7.1 の9画面を Web・デスクトップ・TUI で提供し、CLI と同じ Application Service 上で同じ状態・件数・エラーを表示する。

**依存:** M3(ジョブ・収集)、M4(検索)。チャットと可視化の画面は M7 の Service が要るので、**サービス境界を先に切ってスタブで進める**。

## Task 6.1 FastAPI `/api/v1` と SSE
- NiceGUI 内蔵アプリへ統合。ジョブ進捗の SSE、ヘルスチェック。
- 既定バインドは `127.0.0.1`。`0.0.0.0` 公開時はアクセストークンまたはリバースプロキシ認証を必須にする(§7.1)。
- SSE は M3 の `ProgressEvent` バスを購読する。イベント形状は表示層に依存しない。

## Task 6.2 NiceGUI 9画面
ダッシュボード / ソース・バッチ / ジョブ / 文書 / 検索 / チャット(M7 までスタブ)/ 可視化(同)/ 品質 / 設定・診断。

- §6.1 のセマンティックトークン7種を Web の hex 値で実装。**色だけで状態を伝えない**(記号かラベルを必ず併記)。
- ライト/ダーク両テーマ。Markdown 内 HTML は表示時にサニタイズする(§12)。
- デスクトップは同じ画面を native mode で起動。
- 長時間稼働エントリポイントは `WorkerSupervisor` を開始する(§10.1)。

## Task 6.3 Textual TUI
- 同9領域を左ナビで。`Ctrl+K` パレット / `/` 検索 / `r` 更新 / `Esc` 戻る / `?` ヘルプ / `q` 終了。
- 最低 100桁×30行。未満は1ペイン表示へ切替。破壊的操作はモーダル確認。

## Task 6.4 UI テストと横断契約
- Textual Pilot(ナビ・検索・モーダル・キャンセル・狭幅)、NiceGUI の Python fixture、Playwright(主要フロー+アクセシビリティ)。
- **UI 横断契約テスト**: 同一ジョブが Web・TUI・CLI で同じ状態・件数・エラーコードを表示すること。これが §15 の受入条件。

**ゲート:** §13.2 の UI テスト群が緑 / 横断契約テストが緑 / 30秒超の処理に進捗・キャンセルがある。

---

# M7 チャット・監査・可視化・知識昇格

**Goal:** チャット、品質監査4種、Manim 可視化、`kb-visualize` MCP(残り3ツール)、知識昇格を実装し、**15ツール契約を完成させる**。

## Task 7.1 チャット
- migration で `conversations` / `messages` / `citations`。
- `ChatService`: SearchService で根拠取得 → コンテキスト構築 → OpenAI SDK ストリーミング(プロバイダ境界で交換可能)→ **引用検証(`line_range` で実際に存在する行か確認)** → 履歴永続化。
- 旧 `chat-server.js` は移植しない。会話履歴はサーバー側 DB に持つ(旧版はクライアント保持だった)。

## Task 7.2 監査4種
`verify-integrity`(6状態・欠落≠削除の原則)、`find-duplicates`(same_article / identical / near、閾値互換)、`check-contradictions`(候補限定 + 数値・否定・状態語)、`backfill-metadata`(dry-run 既定・4キーのみ・書込後ハッシュ検証・ロールバック)。`audit_runs` / `audit_findings` へ記録。

**旧テストのシナリオを pytest 化する**(M1 で fixture 化していない。`tests/fixtures/PROVENANCE.md` のカバレッジ表で M7 scope とされている11ファイル)。

## Task 7.3 可視化
- `domain/scene_spec.py`: SceneSpec 1.0 検証。2 kind(explain / flow)、予約 kind エラー、beats ≤ 30、日本語エラーメッセージ互換。
- `source_verifier`: `range_hash` 照合。**metric の出典不正は即中断、statement 系は beat 除去+警告+カスケード**、`decorative: true` はバイパス。
- `manim_runner`: 子プロセス、`PYTHONUTF8`、既定10分、最終行 JSON パース、出力パス封じ込め。
- `artifact_store`: `reports/visualizations/<UTC-ts>-<slug>-<4hex>/`、manifest + sha256。
- `render` リースで全プロセス横断1件(`CONCURRENT_RENDER` 互換)。
- 既存の `tools/visualize/render_scene.py` と Manim テンプレートを同梱移設(Manim 0.19 互換維持)。

## Task 7.4 kb-visualize MCP(3ツール)
`list_scene_kinds` / `check_visualize_deps` / `render_scene`。M1 の契約 fixture をリプレイ。**これで15ツール互換が完成する。**

## Task 7.5 知識昇格
`tools/knowledge-curator/promote.py` を `curate promote` へ移植。procedures-index.jsonl → curated テンプレート、`status: draft`、原本不変更。

**ゲート:** kb-visualize 契約 fixture 緑(15/15 完成)/ 監査出力が旧版と一致 / 実 Manim レンダリング(明示フラグ時)で manifest sha256 検証成功。

---

# M8 移行ツール

**Goal:** 旧システムのデータを新システムへ移行し、検証する。設計書 §11。

## Task 8.1 inspect(M2 直後から前倒し可)
移行元 read-only 走査 → 候補・容量・件数・スキーマ版・文字化け疑い・壊れ front matter を報告。**ファイルシステムを正とする**(旧DBは不完全)。

## Task 8.2 plan
ファイル単位で コピー/変換/再生成/除外 を確定し `plan.json` 出力。移行元と移行先が同一・親子関係なら拒否。既定 `--from C:\Temp\multi-source-knowledge-base --to C:\Temp\abist-knowledge-base`。

## Task 8.3 run
一時ディレクトリへ構築 → 検証成功後に正式切替。
- `docs/` バイト保持コピー(パス・文字コード・front matter 検証)。**`docs/` 外にある収集済みコンテンツも拾う**(M3 で記録した web の `docs/` 未付与問題)。
- sync-state 列単位インポート(+`uuid`/`source_id` 付与)。
- batch-config.js → `batches`(M2 のパーサー。`UNSUPPORTED_BATCH_CONFIG` で停止、修正版 JSON5/JSON 指定で再実行可)。
- 可視化成果物(manifest + sha256 検証コピー)、git-cache(リモートURL+HEAD 検証分のみ)、レポート類。
- `.env` はキー名診断のみ。値は自動コピーしない。
- 工程ごとに件数・SHA-256・警告を `migration-manifest.json` へ。**再開可能**(完了済みハッシュ一致工程スキップ)。失敗時は移行先破棄のみ。

## Task 8.4 索引・埋め込み
索引は Markdown から再構築。**埋め込みは全件再生成**(§11.2 ゲート不合格が M4 で確定済み。約98分、resumable、`corpus-write` リース下)。M4 の全件実行が事前のリハーサルになっている。

## Task 8.5 verify
§11.4 の全条件を manifest と突合: Markdown 件数・パス集合・SHA-256 / source×sync_status 件数 / バッチ定義 / 可視化 manifest+sha256 / 索引対象集合 / Recall@5 低下 ≤0.01・出典行一致率 ≥95%。

**ゲート:** 縮小 fixture からの全移行が冪等 / scratch 試行で verify 全項目合格 / manifest に未説明の欠落なし。

---

# M9 並行稼働・カットオーバー

## Task 9.1 本移行
実データ移行(埋め込み再生成込み)。manifest レビューで未説明の欠落ゼロを確認。

## Task 9.2 並行比較
同一入力に対する同期判定・検索品質(22クエリ)・MCP 応答を新旧で比較記録。**この期間バッチ定義の変更は凍結**(旧 `batch-config.js` への書き戻しは移植しない方針のため)。

## Task 9.3 切替
`.mcp.json` の3キーすべてを Python 版へ。既存スキル(`searching-kb` 等)の手順文書はコマンド差替のみ。統合サーバー `all` は別キー並行登録、契約テスト通過後に案内。**旧3キーの削除は別リリースの破壊的変更**とし、移行ガイド・設定差分・ロールバック手順を提示する。

## Task 9.4 §15 受入チェックリスト
全項目を記録付きで確認。ロールバック手順を文書化。

## Task 9.5(任意)Office 変換
`office` 任意依存グループで `pptx_to_md` 等を補助コマンド化(未導入でもコア起動可)。

**ゲート:** §15 全項目サインオフ → Python 版を正本宣言。

---

# M10 旧システム廃止

旧リポジトリを**読み取り専用で1リリース保持** → 復旧不要を確認後、アーカイブ化を利用者に提案する(**削除は利用者判断**。こちらから消さない)。

**ゲート:** 保持期間中にロールバックが発生しなかったこと。
