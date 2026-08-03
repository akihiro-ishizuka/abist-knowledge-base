# M3 収集・バッチ・永続ジョブ・CLI 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development でタスク単位に実行する。ステップは `- [ ]` で進捗管理。

**Goal:** esa / Web / Git からの収集と差分同期、バッチ管理、永続ジョブ基盤(Worker とリース)、それらを操作する Rich CLI を実装する。同一入力に対して旧 Node 版とバイト同一の `docs/` を生成することをもって完了とする。

**Architecture:** M2 のカーネル(`sync_policy`・`frontmatter`・`metadata_schema`)を判断の中核に据え、ソース種別ごとのアダプタが I/O を担う。すべての長時間処理は SQLite 上の永続ジョブとして表現し、複数プロセスが同時起動してもリーダー1つ・リソース排他が守られる。CLI は既定でインライン同期実行し、`--detach` のときだけキューへ投入する。

**Tech Stack:** Python 3.12 / httpx(非同期)/ markdownify or html2text(HTML→MD)/ SQLite WAL / AnyIO / Typer + Rich

## Context

親計画: `C:\Users\a1199118\.claude\plans\design-system-design-md-push-glowing-tulip.md`。設計の正は [../system-design.md](../system-design.md) §9(データ設計)、**§10(ジョブと進捗)**、§12(セキュリティ)、§13.1/§13.3(テスト)。

**M2 で確立済み、そのまま使う:**
- `domain/sync_policy.py` — 11アクションの判定、`decide_missing_candidate` の3条件、`ACTION_BUCKET`、`sync_status_for`
- `domain/frontmatter.py` — バイト保存パース、`hash_body`、`set_frontmatter_values`
- `domain/metadata_schema.py` — 列挙、`classify_document`、`safe_batch_name` / `sanitize_file_name`(**別物。統合しない**)、`extract_repo_name`
- `migration/batch_config_parser.py` — 旧 `batch-config.js` の読み取り(一方向インポート専用。**書き戻さない**)
- `infrastructure/search/chunker.py`, `e5_input.py` — M4 で使う。M3 では触らない。

**M0 で確立済み:**
- `AppError`/`ErrorCode`/`ExitCode`、`Presenter`(`--output` 各モード)、`AppTyper`、SQLite 接続(`connect(immutable=)` あり)・マイグレーション基盤、秘密マスク付きロギング。
- **サブコマンド群は `AppTyper()` で作ること。** `typer.Typer()` だと `AppError` が終了コードへ変換されず、実プロセスとテストで挙動が食い違う(`design/plans/M0-foundation.md` の申し送り参照)。
- ロック競合は `AppError(CONFLICT, retryable=True)`、恒久エラーは `FAILURE`/非 retryable。リトライループはこの区別に従う。

**M1 の記録(着手前に `tests/fixtures/PROVENANCE.md` を読むこと):**
- 旧 `test/sync-{esa,web,git}.test.js` と `orphan-detection.test.js` のシナリオは **fixture ではなく pytest として再実装する対象**。M1 では意図的に採取していない。
- `tests/fixtures/kernel/sync-status-for.json` — 実行で採取済み。**esa と web は同じマッピング、git は常に `synced`**(`decideSyncAction` を呼ばない)。この差を潰さないこと。
- `tests/fixtures/html/turndown-goldens.json` — HTML→MD の15ケース。**advisory**(完全一致は要求しない)。差分は設計判断として記録する。
- 旧システムの実態(設計書 §9.2 の前提を覆す): `sync-state.sqlite` はバックフィル結果でしかなく、`backfill-metadata.js` が参照コーパスを既定除外している。`docs/knowledge/catiadoc`(1,313ファイル、front matter は `source: web`)はどの DB にも登録されていない。**M3 の同期は DB を完全な台帳と仮定してはならない。**

## Global Constraints(全タスク共通)

- **旧リポジトリ `C:\Temp\multi-source-knowledge-base` には書き込まない。** 比較検証で読むのは可。SQLite を開くときは**サンドボックスコピー方式のみ**(`readonly:true` でも `-shm` の mtime が動くことが実測済み)。
- 出力パスは解決後に `docs/` または `reports/` 配下であることを検証し、`..`・絶対パス・Windows 予約名を拒否する(§12)。**旧 web アダプタの「`docs/` を付けない」不整合は矯正する**(付ける)。矯正した事実は記録する(**`tests/fixtures/PROVENANCE.md` §4「旧システムのデータ層に関する訂正事実」に恒久記録済み** — `C#ATIA` バッチが実例で、`docs/knowledge/catiadoc` 配下の約1,300ファイルがこの穴でどちらの旧DBにも未登録のまま孤児化している。M8 の移行棚卸しはこれを踏まえ、収集済みコンテンツが全て `docs/` 配下にあるとは仮定しないこと)。
- Web 収集は同一ホスト既定。最大深さ・最大ページ数・サイズ・タイムアウトを**必須上限**にする。
- Git は任意フックを実行しない。取得内容はデータとして扱う。
- 文書削除・バッチ削除・強制同期は監査イベントへ記録する。破壊的操作は対象件数とパスを先に提示し、非対話 CLI では `--yes` を必須にする。
- 秘密情報(esa トークン、Git 資格情報)をログ・DB・レポートへ書かない。URL 埋め込み資格情報もマスクする。
- SQLite: WAL、`foreign_keys=ON`、`busy_timeout=5000`。lease 取得とジョブ claim は `BEGIN IMMEDIATE`。プロセス内 `asyncio.Lock` だけに依存しない。
- TDD。外部 I/O はモック(esa モックサーバー、ローカル HTTP サイト、一時 Git リポジトリ)で閉じる。実 esa API を叩くテストは書かない。
- 各タスクのコミット条件: `uv run pytest -q` 全通過、`ruff check .`、`ruff format --check .` クリーン。

## ファイル構成

```
src/abist_kb/
  domain/
    job.py                    # T1: ジョブ状態・ProgressEvent・リース種別
  infrastructure/
    db/migrations/
      0002_jobs.sql           # T1: jobs/job_events/worker_leases/resource_leases
      0003_sources_batches.sql# T2: sources/batches/batch_items/documents
    jobs/
      repository.py           # T1: ジョブ CRUD・claim
      leases.py               # T1: worker/resource リース
      supervisor.py           # T1: WorkerSupervisor
      events.py               # T1: ProgressEvent バス
    sources/
      base.py                 # T3: SourceAdapter プロトコル・共通同期ループ
      esa.py                  # T3
      web.py                  # T4
      git.py                  # T5
      html_to_md.py           # T4: HTML→Markdown 変換
  application/
    document_service.py       # T2
    source_service.py         # T2
    batch_service.py          # T2
    sync_service.py           # T3-T5
    job_service.py            # T1
  presentation/cli/
    jobs_cmd.py worker_cmd.py # T1
    source_cmd.py batch_cmd.py document_cmd.py  # T2
    sync_cmd.py               # T3-T5
tests/
  jobs/                       # T1: 多プロセス統合テスト含む
  sources/                    # T3-T5: モックサーバー・一時 git リポジトリ
  application/                # T2
```

---

## Task 1: 永続ジョブ基盤とリース

**Files:** `src/abist_kb/domain/job.py`, `src/abist_kb/infrastructure/db/migrations/0002_jobs.sql`, `src/abist_kb/infrastructure/jobs/{repository,leases,supervisor,events}.py`, `src/abist_kb/application/job_service.py`, `src/abist_kb/presentation/cli/{jobs_cmd,worker_cmd}.py`, `tests/jobs/`

**Interfaces:**
- Produces: `JobState`(`QUEUED/RUNNING/SUCCEEDED/PARTIAL/FAILED/CANCELLED/INTERRUPTED`)、`ResourceKind`(`DOCS_WRITE`, `CORPUS_WRITE`, `RENDER`)
- Produces: `ProgressEvent(job_id, phase, current, total, message, severity, item, timestamp)`
- Produces: `JobRepository`(submit/claim/update/cancel/retry/list/get、`BEGIN IMMEDIATE` で claim)
- Produces: `LeaseManager.acquire(kind, key=None, owner, ttl) -> ContextManager`、期限切れ検出
- Produces: `WorkerSupervisor(owner_id, heartbeat=5s, lease_ttl=15s)`。`start()`/`stop()`、リーダーのみキュー消費
- Produces: `JobService.submit/progress/cancel/retry/history`

- [ ] **Step 1: マイグレーション 0002 とテスト**

`jobs`(id, kind, state, params JSON, result JSON, error JSON, progress JSON, cancel_requested, retry_of, created_at, started_at, finished_at)、`job_events`(job_id, seq, phase, current, total, message, severity, item, created_at)、`worker_leases`(owner_id, heartbeat_at, expires_at)、`resource_leases`(resource_key PK, owner_id, job_id, expires_at)。

- [ ] **Step 2: リースの多プロセステストを先に書く(赤)**

これが M3 で最も壊れやすい箇所なので最初に固める。`subprocess` で2〜3プロセス起動し:
- `worker_leases` のリーダーが常に1つだけであること
- リーダー停止後、lease 期限切れを待って別プロセスが引き継ぐこと
- `docs-write` を同時取得しようとしたとき、2つ目が待つか `CONFLICT` になること(仕様として決めて記録)
- `corpus-write:<corpus>` はコーパスが違えば同時取得できること

Run → FAIL。

- [ ] **Step 3: リースと Supervisor を実装**

所有者はプロセス UUID。heartbeat 5秒、lease 15秒。取得・更新は `BEGIN IMMEDIATE`。期限切れ lease は次のリーダーが回収する。

- [ ] **Step 4: 緑を確認 + ジョブ claim の競合テスト**

同一ジョブを複数プロセスが claim しようとして1つだけ成功することを確認する。

- [ ] **Step 5: 異常終了からの復旧**

heartbeat と resource lease が期限切れの `running` ジョブを、次のリーダーが `interrupted` へ遷移させる。安全に再試行できるものだけを利用者確認後に再投入する(自動再投入はしない)。

- [ ] **Step 6: ProgressEvent バスと CLI**

インプロセス購読 + `job_events` への追記。`jobs list|show|cancel|retry`、`worker run`。Rich 進捗は全体・現在項目・完了数・失敗数・経過時間・残り時間を出す。

- [ ] **Step 7: `--detach` の契約**

CLI 既定はインライン同期実行。`--detach` 指定時のみキュー投入し、**有効な worker heartbeat が無ければ `WORKER_UNAVAILABLE` で失敗する**(実行されないジョブを放置しない)。

- [ ] **Step 8: コミット**

---

## Task 2: DB スキーマと Application Service・文書/ソース/バッチ CLI

**Files:** `0003_sources_batches.sql`, `application/{document_service,source_service,batch_service}.py`, `presentation/cli/{source_cmd,batch_cmd,document_cmd}.py`, `tests/application/`

- [ ] **Step 1: マイグレーション 0003**

`sources`(id, type, display_name, connection JSON, output_dir, enabled, created_at, updated_at)、`batches`/`batch_items`、`documents`(**旧27列を全保持** + `uuid` + `source_id`)。索引は旧同等(source/sync_status/status/source_key)。

`documents` の列は `tests/fixtures/kernel/` と旧 `sync-state.js` の定義に一致させる。

- [ ] **Step 2: DocumentRepository のテストを書く(赤)**

部分 upsert(欠落列は保持・未知キーは無視)、`list_documents` のフィルタ(source/sync_status/status/path_prefix/category_prefix/managed_only)、`count_by_source`、`mark_missing`/`clear_missing`、Windows→POSIX パス正規化。

- [ ] **Step 3: 実装し緑を確認**

- [ ] **Step 4: Service 層と CLI**

`SourceService`(CRUD・接続テスト)、`BatchService`(CRUD・旧 `batch-config.js` からの一方向インポート)、`DocumentService`(一覧・取得・メタデータ更新・安全削除)。CLI: `source list|add|edit|remove|test`、`batch list|show|add|edit|remove|run`、`document show <path>`。

**バッチは `app.sqlite` が正。** 旧ファイルへ書き戻さない。インポートは `migration/batch_config_parser.py` を使う。

- [ ] **Step 5: 破壊的操作の確認と監査**

削除系は対象件数とパスを提示 → 確認(非対話は `--yes`)→ 監査イベント記録。

- [ ] **Step 6: コミット**

---

## Task 3: esa アダプタと同期ループ

**Files:** `infrastructure/sources/{base,esa}.py`, `application/sync_service.py`, `presentation/cli/sync_cmd.py`, `tests/sources/test_esa.py`

- [ ] **Step 1: 旧 sync-esa シナリオを pytest 化(赤)**

`test/sync-esa.test.js` と `test/orphan-detection.test.js` のシナリオを移植する。esa モックサーバー(ローカル HTTP)で閉じる。網羅する挙動:
- `unchanged` のとき**ファイルの mtime が変わらない**こと
- conflict が繰り返し実行しても安定すること
- `--force` で `conflict_overwritten` になること
- DB レコードが無いとき `adopt`(記録のみ、上書きしない)
- `--dry-run` が何も書かないこと
- 3回連続実行で結果が安定すること
- 人間が設定した `status` / `document_type` が再同期で保持されること
- カテゴリ改名時の orphan 解決、`--prune-orphans`
- 重複 post_number の扱い

- [ ] **Step 2: esa アダプタを実装**

httpx 非同期。post / category / search 取得。出力パスは `<output_dir>/<sanitize_category_path(category)>/<sanitize_file_name(name)>.md`。

front matter の生成キー順は固定: `title, date, updated_at, author, updated_by, category, tags, post_number, url, source, managed_by, document_type, status`。空値は省略。文字列は二重引用符+エスケープ、ただし末尾4つのメタキーは非引用。

`save_post` の流れ: 既存ファイル読み取り → `decide_sync_action` → 書かない判定なら `last_checked_at` と `sync_status` のみ更新 → 書く判定でも**レンダリング結果が既存とバイト同一なら `unchanged` へ格下げ**(front matter の `updated_at` が日付までしか無いため、これが無いと毎回書き換わる)。

missing 判定は `decide_missing_candidate` に委ねる。閾値は `ABIST_KB_MISSING_THRESHOLD`(既定3)。

- [ ] **Step 3: 緑を確認**

- [ ] **Step 4: SyncService と CLI**

`sync source|batch|all`。`docs-write` リースの下で実行。進捗は `ProgressEvent`。結果は `reports/sync/sync-esa-<label>-<ts>.json` へ(旧形式互換)。

- [ ] **Step 5: DB 書き込み失敗を致命傷にしない**

旧 `doc-record.js` の契約を維持する: DB エラーは警告に降格し、ダウンロード自体は失敗させない。

- [ ] **Step 6: コミット**

---

## Task 4: Web アダプタ

**Files:** `infrastructure/sources/{web,html_to_md}.py`, `tests/sources/test_web.py`

- [ ] **Step 1: 旧 sync-web シナリオを pytest 化(赤)**

ローカル HTTP サーバーを立てて検証する。網羅する挙動:
- 条件付き GET(`If-None-Match` / `If-Modified-Since`)
- 304 のとき本文は来ないので、**保存済み Markdown からリンクを再抽出して巡回を継続**する
- ローカル編集の保護(勝手に上書きしない)
- HTTP 失敗でローカルファイルを削除しない
- `etag` / `last_modified` / `source_content_hash` は**実際に書き込んだときだけ**永続化する(スキップで競合が解決済みに見えないように)

- [ ] **Step 2: HTML→Markdown 変換**

`tests/fixtures/html/turndown-goldens.json` の15ケースと比較する。**完全一致は要求しない(advisory)**が、差分は1件ずつ見て許容可否を判断し、判断理由を記録する。旧実装は素の turndown(GFM プラグイン無し)なので**表と定義リストは平文へ潰れる**。前処理も再現する: 相対リンク・画像 URL の絶対化、`script/style/nav/header/footer/aside/.sidebar/.navigation` の除去。

- [ ] **Step 3: クローラを実装**

キュー + 並行数制御 + アイドルタイムアウト。同一ホスト既定。最大深さ・最大ページ数・サイズ・タイムアウトを必須上限として強制する。

front matter 7キー(`title, url, date, source, managed_by, document_type, status`)。**`date` は初回取得日を維持**する。

出力先には `docs/` を強制付与する(旧実装の不整合を矯正)。

- [ ] **Step 4: 緑を確認しコミット**

---

## Task 5: Git アダプタ

**Files:** `infrastructure/sources/git.py`, `tests/sources/test_git.py`

- [ ] **Step 1: 旧 sync-git シナリオを pytest 化(赤)**

一時 Git リポジトリを作って検証する:
- shallow clone / fetch + reset
- 変更の無いファイルに触らない(mtime 不変)
- 上流で消えたファイルだけを削除する
- **fetch 失敗で出力ディレクトリを消さない**
- 破損キャッシュからの再作成
- `diff_files` の判定

- [ ] **Step 2: 実装**

`data/cache/git/<repo>[@<branch>]/` に shallow ミラー。フックは実行しない。バイト比較の増分コピー(追加・変更のみ書き、上流消滅分のみ削除、空ディレクトリ剪定)。

**git 由来ファイルには front matter を書かない**(次回 clone で消えるため)。メタデータは DB のみ。`source_key = git:<repo>#<relfile>`、`source_updated_at = <commit sha>`。

**`sync_status` は常に `synced`**(`tests/fixtures/kernel/sync-status-for.json` が実行で確認済み。esa/web と違い `decide_sync_action` を通さない)。

出力先は `--output-dir` が `docs` で始まらなければ `docs/` を付与する。

- [ ] **Step 3: 緑を確認しコミット**

---

## Task 6: E2E とバイト同一検証

**Files:** `tests/sources/test_e2e_byte_identity.py`

- [ ] **Step 1: 3ソースのモック E2E**

esa モック・ローカル HTTP サイト・一時 Git リポジトリを用意し、同一入力に対する Python 版の `docs/` 出力を記録する。

- [ ] **Step 2: 旧 Node 版との比較**

**旧リポジトリを変更しない方法で**同じ入力を旧実装へ流す。サンドボックスコピー方式(旧スクリプトと `tools/lib/` を OS 一時領域へ複製、`node_modules` はジャンクション)で実行し、出力先も一時領域にする。両者の `docs/` をバイト比較する。

esa と git は完全一致を要求する。**web は HTML→MD 変換器が別物なので一致しない**。差分を記録し、許容判断を明文化する(一度きりの変換器変更イベントとして扱う)。

- [ ] **Step 3: 実データ dry-run の判定一致**

実 `docs/` のスナップショットに対する `sync --dry-run` の判定が、旧版の plan と一致することを確認する。**`docs/` へは書き込まない。**

- [ ] **Step 4: `--output json` 純度**

同期コマンドの JSON 出力が stdout に単一 JSON のみ、ANSI 無しであることを確認する。

- [ ] **Step 5: コミット**

---

## Verification(M3 ゲート)

1. モック E2E で esa・git の出力が旧 Node 版とバイト同一。web は差分を記録し許容判断済み。
2. 多プロセス統合テスト: リーダー1つ、`docs-write` 直列化、`corpus-write` はコーパス別に並行可、ジョブ claim の排他。
3. 旧 `sync-{esa,web,git}` / `orphan-detection` の全シナリオが pytest として通る。
4. `--detach` が worker 不在時に `WORKER_UNAVAILABLE` で失敗する。
5. 異常終了後、`running` ジョブが `interrupted` へ遷移する。
6. `--output json` 純度、破壊的操作の確認と監査記録。
7. 秘密情報がログ・DB・レポートに出ない。
8. Windows と Linux の CI 両方で緑。

## 次のマイルストーン

M3 完了後は **M4(索引・埋め込み・ハイブリッド検索)**。M4 は `tests/fixtures/eval/baseline.json`(hybrid Recall@5 = 0.9545)との比較が受入ゲートになる。埋め込みモデルの識別子は**ハッシュ用が `Xenova/...`、ローダ用が `intfloat/...`** で使い分ける(`tests/fixtures/PROVENANCE.md` 参照)。
