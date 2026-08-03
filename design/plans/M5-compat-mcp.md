# M5 互換 MCP サーバー(kb-download / kb-search)実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development でタスク単位に実行する。ステップは `- [ ]` で進捗管理。

**Goal:** 旧 Node 版と同じ MCP ツールを Python で提供し、M1 で採取した契約 fixture のリプレイで一致を証明する。本マイルストーンでは `kb-download`(8ツール)と `kb-search`(4ツール)を対象とし、`kb-visualize`(3ツール)は M7 で完成させる。

**Architecture:** MCP Python SDK の上に薄い互換層を置く。ビジネスロジックは M3/M4 の Application Service をそのまま呼ぶ。stdout は JSON-RPC 専用にし、Rich を初期化しない。既存ツールのスキーマ・応答形状・同期ブロッキング挙動・タイムアウトを変えず、非同期版は**別名の新規ツール**として追加する。

**Tech Stack:** Python 3.12 / MCP Python SDK / Pydantic v2

## Context

親計画: `C:\Users\a1199118\.claude\plans\design-system-design-md-push-glowing-tulip.md`。設計の正は [../system-design.md](../system-design.md) **§7.4(MCP 互換性の3層定義)**、§6.3(出力モードと stdio 純度)、§10.2(同期互換実行と排他)。

**M1 の契約 fixture(着手前に `tests/fixtures/PROVENANCE.md` を読むこと):**
- `tests/fixtures/mcp/tools-list.json` — 3サーバー15ツールのスキーマスナップショット。所属は kb-download 8 / kb-search 4 / kb-visualize 3。
- `tests/fixtures/mcp/<server>/<tool>/<case>.json` — 生の JSON-RPC 応答28ケース。**正規化せずそのまま保存されている**ので、比較側で時刻・所要時間・一時パスを吸収する。
- 各ケースは `response`(パース済み)・`response_raw_line`(生の1行)・`response_kind`・`is_error_present`/`is_error_value`・`stderr_tail`・`run2`・`nondeterministic_fields` を持つ。
- **非決定なのは `search_kb` の `diagnostics.elapsedMs` だけ**(全5ケース)。残り23ケースは2回実行してバイト同一だった。

**取り扱いに注意が要る発見(PROVENANCE.md に記録済み):**
- kb-download の**エラー4ケース**(`download_esa_post` / `download_esa_category` / `download_esa_search` / `download_git`)は、ツール自身の JSON ではなく **SDK が生成した英語の散文**を返す(`"MCP error -32602: Input validation error: ..."`)。これは zod と `@modelcontextprotocol/sdk` のバージョン固有の文言であり、Python 側(pydantic + MCP Python SDK)は必ず別の文言になる。**比較は `response_kind` + `isError` + `-32602` コードで行い、全文一致を要求しない。** 各ケースに `response_kind: "sdk_validation_error"` と `"tool_result_json"` のタグが付いている。
- `add_web_batch` は呼ぶと `batch-config.js` を書き換えるため、**スキーマのみ採取**(`skipped_reason` 付き)。
- `download_git` のエラーケースは、空文字列によるスキーマ検証失敗を使っている(非空文字列だと実際に `git clone` が走るため)。`deviation_from_brief` に記録済み。

**M3/M4 で確立済み、そのまま使う:**
- `application/{batch_service,sync_service,index_service,search_service}.py`。**`SearchService` の応答形状は M4 で `search_kb` fixture に合わせてある**ので、変換は薄いはず。
- 永続ジョブ基盤とリース。`docs-write` は M3 で「2つ目は待つ」仕様に決定済み。`wait=False` 経路も残してある(MCP の busy 意味論用)。
- `AppTyper`、`Presenter`(ただし MCP では使わない — 下記)。

## Global Constraints(全タスク共通)

- **stdout は JSON-RPC 専用。** Rich を初期化しない。`Presenter` を stdout に向けて構築しない。ログは stderr かファイルへ。これは設計書 §6.3 の要求であり、混入すると MCP クライアントがプロトコルを解釈できなくなる。
  - M0 の申し送り: `Presenter` は `stdout=` を明示的に受け取れるので、`mcp serve` は自前の Presenter を stderr に向けて構築し、共通コールバックが作ったものを使わない。**コールバックはすべてのサブコマンドで stdout 束縛の Console を構築するため、`mcp serve` は自前のものに差し替えるまで stdout へ書くメソッドを一切呼んではならない。**
- **既存15ツールのスキーマと応答へフィールドを追加しない。** 新機能は別名の新規ツールとして足す(§7.4.2)。
- 同期ツール(`run_batch` / `download_*` / `render_scene`)は**ジョブレコードを作っても呼び出し元にはジョブIDを返さない**。処理完了まで待ち、旧版と同じ結果 JSON を返す。タイムアウトは batch 60分 / web 30分 / git 15分 / esa 10分。
- 同期ツールは `docs-write` リースの下でインライン実行する。キューワーカーを必須にしない(§10.2)。busy 時の意味論を維持する。
- 秘密情報を応答・ログへ出さない。
- 旧リポジトリ `C:\Temp\multi-source-knowledge-base` には書き込まない。SQLite はサンドボックスコピー方式のみ。
- ruff・format クリーン、`uv run pytest -q` 全通過を各タスクのコミット条件とする。

## ファイル構成

```
src/abist_kb/presentation/mcp/
  server_core.py      # T1: SDK 初期化・stdout 純度・応答整形
  payloads.py         # T1: content[0].text へ入れる JSON の整形
  kb_search.py        # T2: 4ツール
  kb_download.py      # T3: 8ツール
  jobs_tools.py       # T4: start_* / job_status / cancel_job / 新規参照
  all_server.py       # T4: 統合サーバー(追加機能。既存3つを置き換えない)
src/abist_kb/presentation/cli/mcp_cmd.py   # T1: mcp serve
tests/mcp/
  replay.py           # T1: fixture リプレイ基盤(正規化ルール込み)
  test_contract_search.py test_contract_download.py
```

---

## Task 1: サーバー基盤とリプレイ基盤

**Files:** `presentation/mcp/{server_core,payloads}.py`, `presentation/cli/mcp_cmd.py`, `tests/mcp/replay.py`

- [ ] **Step 1: stdout 純度のテストを先に書く(赤)**

サーバーモジュールを import しただけで stdout に1バイトも出ないこと。実プロセスで起動して `initialize` を送り、stdout に JSON-RPC 以外が混ざらないこと。ログが stderr に出ること。

- [ ] **Step 2: server_core**

MCP Python SDK で stdio サーバーを立てる。応答は `{content: [{type: "text", text: <JSON文字列>}], isError: <bool>}`。**`text` に入れる JSON の整形(インデント・`ensure_ascii`)は fixture の実測に合わせる**——`tests/fixtures/mcp/**` の `response_raw_line` をデコードして確認すること。

Streamable HTTP は明示起動のみ。既定は stdio。

- [ ] **Step 3: リプレイ基盤**

fixture の `request` を送り、返った応答を `response` と比較する。**正規化は比較側で行う:**
- `nondeterministic_fields` に挙がっているパス(現状 `diagnostics.elapsedMs` のみ)は値を無視する。
- 時刻・所要時間・一時パスは正規化する。
- `response_kind == "sdk_validation_error"` のケースは**全文一致を要求せず**、`isError` が真であること・`-32602` を含むこと・JSON としてパースできないことだけを検証する。
- `response_kind == "tool_result_json"` のケースは、`text` を JSON としてパースし、**キー集合・型・必須性・エラーコード・0件時の意味・パス表記**を比較する。

- [ ] **Step 4: `mcp serve` CLI**

`mcp serve kb-download|kb-search|kb-visualize|all [--transport stdio|http]`。**自前の Presenter を stderr に向けて構築する。**

- [ ] **Step 5: コミット**

---

## Task 2: kb-search(4ツール)

**Files:** `presentation/mcp/kb_search.py`, `tests/mcp/test_contract_search.py`

- [ ] **Step 1: リプレイテストを書く(赤)**

`search_kb`(5ケース: 日本語自然文・識別子・0件・フィルタ・corpus=reference)、`get_document`(正常・行範囲付き・存在しないパス・パストラバーサル試行)、`get_chunk`(正常・存在しない ID)、`index_status`。

- [ ] **Step 2: 実装**

M4 の `SearchService` / `IndexService` を呼ぶ。work / reference の2コーパスを遅延オープンしてキャッシュする。

`get_document` は行範囲指定時に `%5d | ` のガター付きで返し、`range_hash` を含める。`context` は 0〜50。**`range_hash` は M2 の `line_range` を使う**(get_document / SceneSpec / source-verifier の共通契約)。

**パス封じ込めは prefix 比較でなく `Path.resolve()` + `relative_to` で行う。** 旧版は `startswith(DOCS_DIR)` だったので `docs-backup` のような兄弟ディレクトリを通してしまう。**応答形状は変えずに実装だけ堅くする。**

- [ ] **Step 3: 緑を確認しコミット**

---

## Task 3: kb-download(8ツール)

**Files:** `presentation/mcp/kb_download.py`, `tests/mcp/test_contract_download.py`

- [ ] **Step 1: リプレイテストを書く(赤)**

`list_batches`(正常)、`run_batch`(不明バッチ名)、`add_web_batch`(スキーマのみ)、`download_esa_post` / `download_esa_category` / `download_esa_search` / `download_web` / `download_git`(いずれもエラー系)。

- [ ] **Step 2: 実装**

M3 の `BatchService` / `SyncService` を呼ぶ。**同期ブロッキング**: 完了まで待ち、旧版の JSON 形状(`ok, exitCode, command, outputDir, batchType, sync{totals, conflicts≤20, missingCandidates≤20, errors≤20, reports}, stdoutTail, stderrTail`)を返す。

タイムアウトは batch 60分 / web 30分 / git 15分 / esa 10分。環境変数で上書き可。

単一実行は `docs-write` リースで実現し、busy 応答の文言を維持する。

`add_web_batch` の検証(http/https のみ・絶対パスと `..` を拒否・`docs/` 接頭辞・非 web バッチの上書き拒否)を再現する。**ただし書き込み先は新しい `batches` テーブルであり、旧 `batch-config.js` には書き戻さない。**

`download_git` は非空文字列で実際に clone が走る点に注意。テストはローカルの使い捨てリポジトリへ向ける。

- [ ] **Step 3: 緑を確認しコミット**

---

## Task 4: 新規ジョブ型ツールと統合サーバー

**Files:** `presentation/mcp/{jobs_tools,all_server}.py`

- [ ] **Step 1: 新規ツール(既存スキーマは不変更)**

`start_run_batch` / `start_download_esa_post` / `start_download_esa_category` / `start_download_esa_search` / `start_download_web` / `start_download_git` / `start_render_scene`(M7 で有効化)→ `{ok: true, job_id, state: "queued"}` を返す。

`job_status` / `cancel_job`。`get_batch` / `list_corpora` / `system_status`。

**`start_*` は worker が居なければ `WORKER_UNAVAILABLE` で失敗する**(M3 の `--detach` と同じ契約。実行されないジョブを放置しない)。

**`job_status` は `PARTIAL` を正しく返すこと。** M3 の修正で、一部失敗した同期はジョブ状態が `partial` になる。CLI の終了コードだけでなくジョブ状態を読む消費者がここにいる。

- [ ] **Step 2: 統合サーバー `all`**

15ツール + 新規ツールを1サーバーで提供する。**既存3サーバーを置き換えない**(§7.4.3)。別キーで並行登録できるようにする。

- [ ] **Step 3: 契約リプレイ全件と stdout 純度**

12ツール(kb-download 8 + kb-search 4)の全ケースが通ること。新規ツールが既存ツールのスキーマへ影響していないことを `tools/list` の差分で確認する。

- [ ] **Step 4: 段階切替の手順を記録**

旧リポジトリの `.mcp.json` の `kb-download` / `kb-search` の2キーだけを Python 版へ差し替える手順と、戻す手順を `design/plans/` に記録する。**実際の切替は M9 で行う**(M5 では手順の記録と検証のみ。旧リポジトリの `.mcp.json` を書き換えない)。

- [ ] **Step 5: コミット**

---

## Verification(M5 ゲート)

1. kb-download 8ツール + kb-search 4ツールの全契約ケースがリプレイで一致(時刻・所要時間・一時パスのみ正規化)。
2. `sdk_validation_error` の4ケースは `response_kind` + `isError` + `-32602` で一致(全文一致は要求しない)。
3. `tools/list` のツール名・所属・入力スキーマが fixture と一致。
4. stdout 純度: import 時・実行時とも JSON-RPC 以外が出ない。ログは stderr。
5. 同期ツールが完了まで待ち、旧版の JSON 形状で返す。タイムアウト値が一致。
6. `start_*` が worker 不在時に `WORKER_UNAVAILABLE`。`job_status` が `partial` を返せる。
7. パス封じ込めが `resolve()` + `relative_to` で堅い(応答形状は不変)。
8. 新規ツールが既存スキーマを変えていない。
9. MCP Inspector で手動確認。
10. Windows・Linux 両 CI で緑。

## 次のマイルストーン

M5 完了後は **M6(NiceGUI Web/デスクトップ・Textual TUI・FastAPI/SSE)**。M6 のチャットと可視化画面は M7 の Service が要るので、サービス境界を先に切ってスタブで進める。
