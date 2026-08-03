# fixture 由来記録(M1)

このファイルは `tests/fixtures/**` に何が採取され、何が意図的に採取されなかったか、
なぜそう判断したかを記録する。作成した経緯を記したエージェント作業ログ
(`.superpowers/sdd/M1-fixture-capture/`)は gitignore 対象でマイルストーン完了後に
削除されるため、Python 移植(M2〜M9)の正しさの判定基準そのものであるこの fixture 群には、
恒久的な由来記録がこのファイル1つしか残らない。

読者は旧 Node システム(`C:\Temp\multi-source-knowledge-base`)や
`.superpowers/sdd/M1-fixture-capture/` の中身を見たことがない前提で書いている。

採取元(旧リポジトリ)の HEAD: `66dbfe371a7c5c8caa05821f3e9e9f0375bc9cea`(read-only、変更なし)。
機械可読な集計は `tests/fixtures/capture-manifest.json` にある。本ファイルはそれを
要約・解説するものであり、ファイル一覧やSHA-256を再掲しない。

## 1. fixture カテゴリと契約

Python 移植は「この fixture と一致すること」で正しさを判定する。ただしカテゴリによって
「一致」の意味が違う。ビット互換(diff ゼロを要求)と、advisory(差分があってよく、
レビューして許容判断する材料)を混同しないこと。

| カテゴリ | パス | 契約の種類 | 何を保証するか | 消費するマイルストーン |
|---|---|---|---|---|
| kernel | `tests/fixtures/kernel/*.json` | ビット互換 | frontmatter / chunker / line-range / e5入力 / sync-planner / metadata-schema / batch-config / b32doc-filter / sync-status-for の関数単位の入出力 | M2(カーネル移植の合否そのもの)・M3(sync-status-for は3ソースアダプター共通の受け入れ基準) |
| real-docs | `tests/fixtures/real-docs/samples.json` | ビット互換 | 実 `docs/` から層化抽出した40件に対する同カーネル出力(手書き境界ケースだけでは拾えない実データ分布の確認) | M2 |
| mcp | `tests/fixtures/mcp/**` | ビット互換(ただし `sdk_validation_error` の4ケースは prose 除く。§3参照) | 15ツールの `tools/list` スキーマと `tools/call` の生 JSON-RPC 応答 | M5(MCP 互換性検証) |
| eval | `tests/fixtures/eval/{queries.jsonl,baseline.json}` | ビット互換(ランキング・指標) | 22クエリ × `bm25_raw`/`hybrid` の2方式のランク付き結果・Recall@5/RR/nDCG@10 | M4(検索移植の受け入れゲート) |
| embedding | `tests/fixtures/embedding/gate-samples.json` | ビット互換(入力・保存済みベクトル) / 判定自体はadvisory | 100件の埋め込み入力・input_hash・保存済みベクトルBLOB | M8(埋め込み再利用ゲート判定の入力) |
| html | `tests/fixtures/html/turndown-goldens.json` | **advisory**(`comparison_policy: "advisory"`) | turndown(vanilla、gfmプラグイン無し)によるHTML→Markdown変換の15ケース | M3(差分は前提。完全一致は要求しない) |

### M4 受け入れゲートの基準値

`eval/baseline.json` は M4 の受け入れゲート(「Recall@5 が基準値の0.01以内」)が
測る対象そのものである。採取したマクロ平均(22クエリ、`data/kb-index.sqlite`、
埋め込みモデル `Xenova/multilingual-e5-small`):

| 方式 | Recall@5 | MRR | nDCG@10 | 0件クエリ数 |
|---|---|---|---|---|
| bm25_raw | 0.7955 | 0.7323 | 0.7627 | 1(q16、FTS5のtrigramが2文字語を索引できないため) |
| hybrid | 0.9545 | 0.8788 | 0.8779 | 0 |

`bm25_raw` の0件クエリ(q16「蛇腹の要件はどこまで充足しているか」)は、
「蛇腹」「要件」「充足」がいずれも2文字の日本語語であるため FTS5 の trigram
トークナイザで索引できないことに起因する。`hybrid` はこれを補う2文字語 LIKE
補助検索(`shortTermSearch`)を持つため0件を回避する。応答時間は非決定なので
fixture には含めていない(3回の独立採取でランキング・スコアの SHA-256 は完全一致、
応答時間のみ変動)。

### embedding ゲートの位置づけ

`gate-samples.json` は design/system-design.md §11.2 が定義する埋め込み再利用ゲート
(Python の sentence-transformers で同じ入力を埋め込み、全100件でコサイン類似度
≥0.999 なら既存ベクトル再利用可、そうでなければ全件再生成)の**測定入力**であり、
合否判定そのものはここでは出さない(判定は M8 が実施する)。旧版は q8量子化 ONNX
であるため、**不合格が既定想定であり、不合格自体は失敗ではない**。

### sync_status_for マッピング(`kernel/sync-status-for.json`、M2後の追加採取)

M2実装時、`documents` テーブルの `sync_status` 列(`synced`/`modified_local`/`conflict`/
`source_missing`/`error` の5値)を、`sync-planner.js` の11アクション
(`create`/`update`/`unchanged`/`local_modified`/`conflict`/`conflict_overwritten`/
`adopt`/`unknown_local`/`missing`/`orphan`/`error`)からどうマッピングするかが、
実行結果のゴールデンとして採取されていないまま `download-article.js` /
`tools/verify-integrity.js` を読んで導出されていた(誰も実行して確認していない)
というギャップが見つかり、`capture-sync-status.mjs` で追加採取した。

この関数(`download-article.js` の `syncStatusFor` / `download-web.js` の
`downloadPage` 内の無名インラインマッピング / `download-git.js` の
`recordMarkdownFiles`)はいずれも **export されていない**。手で転記する代わりに、
旧リポジトリの該当ファイルを `os.tmpdir()` 配下のサンドボックスへコピーし
(`node_modules` はジャンクションで読み取り専用参照。旧リポジトリ本体は一切変更しない)、
`savePost()` を直接実行(esa)、`download-web.js`/`download-git.js` を子プロセスとして
実行(web は127.0.0.1の使い捨てHTTPサーバーへ、gitはネットワーク不要のローカル使い捨て
リポジトリへ向けた)して、実際に sync-state DB へ書かれた `sync_status` を観測した。

**判明した事実(3ソースは同じマッピング関数を共有していない)**:

- **esa と web は(別ファイルに独立実装されているが)実質同じマッピングを持つ**:
  `create`/`update`/`conflict_overwritten` は書き込み後に `synced` を直書き、
  `unchanged`/`adopt`/`unknown_local` は `synced`、`local_modified` は
  `modified_local`、`conflict` は `conflict`。
- **git は `decideSyncAction`/`SYNC_ACTIONS` を一切使わない。** 記録するすべての
  Markdown に、そのファイルが add/update/unchanged のどれだったかに関わらず
  無条件で `synced` を書く(`recordMarkdownFiles` はアクション非依存)。したがって
  `local_modified`/`conflict`/`conflict_overwritten`/`adopt`/`unknown_local`/`orphan`
  の6アクションはgit-syncの実行結果として発生しない。
- **`missing`/`orphan`/一部の`error`は `sync_status` を一切書き換えない**(直前の値の
  まま、または行自体が作られない)。esa の `missing` は
  「全件同期成功+連続不在が閾値到達+個別取得も失敗」の3条件が揃ったときだけ
  `source_missing` を書き、それ以外(閾値未満・個別取得成功)は無変更。`orphan`
  (カテゴリ移動)は `sync_status` を書く呼び出し自体が無い。web には web 固有の
  経路(条件付きGETの HTTP 304)があり、これも `sync_status` に触れない。
- **`missing`(esaの`source_missing`確定分岐)と`orphan`(esa)の一部、および
  `error`(esaのネットワーク例外・gitの`updateGitCache`失敗)は、export されていない
  関数がさらに実 esa API 呼び出しや長いタイムアウト待ちを要求するため、旧リポジトリを
  変更せず安全に実行できる経路が無かった。** これらは fixture 内で
  `derivation: "read_only_no_safe_execution_path"` として明示し、コード読解に基づく
  記述であることを隠さず記録した(実行結果ではないことをはっきりさせるための、
  意図的な「導出不能」の記録)。

## 2. 転記カバレッジ・チェックリスト

旧システムには `test/*.test.js` が28ファイルある。**fixture ディレクトリは存在せず、
すべてインライン定数**だったため、Task 1 がその一部を新リポジトリへ転記した。

**M1 の範囲は最初から28ファイル全部ではない。** M1 が fixture として採るのは
(a) 純粋関数のビット互換ゴールデン(kernel)、(b) MCP wire契約、(c) 検索評価ベースライン、
(d) 埋め込みゲート入力、(e) HTML変換のadvisory資料、の5種だけである
(`design/plans/M1-fixture-capture.md` Task 1〜5 のスコープ)。旧テストの残りは、
**プロセス起動・実HTTPサーバー・一時Gitリポジトリ・実ファイル書き込みを伴う統合的な
振る舞い**を検証しており、これらは該当する後続マイルストーン(主にM3の収集・同期、
M7の監査・可視化)が**シナリオそのものを新実装に対して再実装したpytestとして
検証する対象であり、M1のfixture化(=旧コード実行結果をゴールデン値として保存する
手法)にはそもそも馴染まない**。以下はこの前提のもとでの28ファイル全件の状態。
「未確認」は、旧テスト本文を読んでもなお当該テストをどのマイルストーンが引き取るべきか
断定できないことを意味する(採取漏れがあると断定しているのではない)。

判断根拠: `design/plans/M0-foundation.md` の「Node→Pythonモジュール対応表」(各 `tools/*.js`
がどの `src/abist_kb/` モジュール・どのマイルストーンに対応するか)と、旧リポジトリの
該当テストファイル本文(`C:\Temp\multi-source-knowledge-base\test\*.test.js`、read-onlyで直接確認)。

### M1範囲・fixture化済み(12ファイル)

| 旧テストファイル | 状態 | 転記/採取先 | 理由 |
|---|---|---|---|
| `frontmatter.test.js` | 転記済み | `kernel/frontmatter.json`(36ケース) | ESA_MIXED/WEB_CRLF/B32DOC_BLOCK/NO_FRONTMATTER/EMPTY/BOM_DOC 等を行番号付きで転記し、`parseFrontmatter`/`hashBody`/`setFrontmatterValues`を実行 |
| `chunker.test.js` | 転記済み | `kernel/chunker.json`(構造18+単体6=24ケース) | 全 `test(...)` ブロックを転記・実行。ブリーフの「14ケース」目安との差は展開(1テストが複数呼び出しを含む場合の分割)による |
| `line-range.test.js` | 転記済み | `kernel/line-range.json`(24ケース) | 旧テスト8ケース全転記+BOM有無×CRLF/LF×境界の16ケースを追加実行 |
| `sync-planner.test.js` | 転記済み | `kernel/sync-planner.json`(19ケース) | `decideSyncAction`11アクション全部・`decideMissingCandidate`4シナリオ・`SYNC_ACTIONS`を転記・実行。`ACTION_BUCKET`は非公開定数のため`recordSyncResult`経由で間接導出 |
| `metadata-schema.test.js` | 転記済み | `kernel/metadata-schema.json`(68ケースの一部) | `classifyDocument`全シナリオ・列挙値9個等を転記・実行 |
| `downloaders.test.js` | 部分転記 | `kernel/metadata-schema.json`(sanitize系) | `sanitizeFileName`/`sanitizeCategoryPath`は転記・実行(Windows予約名・255バイト切詰・日本語ケースを追加)。ただし「受入条件6: 月別フォルダが再生成されない」テストと「直接実行でなければmainが走らない」テスト(108-161行)は**意図的にスキップ**——これらは旧JSソースの正規表現スキャンであり、Node実行結果のゴールデン値ではないため |
| `batch-config-store.test.js` | 部分転記 | `kernel/batch-config.json` | `formatConfig`/`loadBatchConfigsFresh`のロジックは転記・実行。旧テストの一時ディレクトリ利用テスト(save→load、一時ファイル残留無し、上書き保存)自体は**スキップ**(ファイルI/O挙動のテストでロジック自体の価値が薄いと判断)。ただしレビュー指摘を受け、serialize出力だけでなく save→load ラウンドトリップの合成ケース(空配列・ゼロ値・クォート付きキー)を別途追加している |
| `kb-download-mcp.test.js` | 転記せず・別方式で捕捉 | `mcp/kb-download/**` | インライン定数の転記ではなく、Task 3 が実サーバーへ `tools/call` を送信し生 JSON-RPC 応答を直接記録した(旧テストの assertion を読むより実行結果そのものを記録する方が正確という判断) |
| `kb-search-mcp.test.js` | 転記せず・別方式で捕捉 | `mcp/kb-search/**` | 同上 |
| `kb-visualize-mcp.test.js` | 転記せず・別方式で捕捉 | `mcp/kb-visualize/**` | 同上 |
| `search-engine.test.js` | 転記せず・別方式で捕捉 | `eval/baseline.json` | インライン定数は転記していないが、Task 4 が `search-engine.js` の `search`/`keywordSearch` を実クエリ(`eval/queries.jsonl`)で直接実行し、ランキング・指標を記録した |
| `b32doc-filter.test.js` | `kernel/b32doc-filter.json`(46ケース: ルール別ケース23+抽出系5+優先順位食い違い2+実文書8(reject中心)+accept実文書3+定数7) | 転記済み(当初のM1範囲の取りこぼしを別タスクで解消。§3参照) | `decideIndexable`/`extractedContent`/`extractSummaryKeys` を転記・実行。6ルール(path_excluded/toc_or_default/not_html_category/language_excluded/empty_content/too_short)すべてにaccept/rejectの両方を用意し、除外理由の文字列も記録した。加えて `docs/knowledge/B32doc` から path-sorted に実文書8件(64KB超過は0件)を採取したところ**たまたま全件 reject** だったため(制御ファイル・非html/非ja文書が辞書順で先に来たため)、accept経路(`decision.indexable===true`)を実データで確認する実文書3件(`real_doc_accept_00〜02`、path-sorted で accept のものだけを決定的に収集、64KB超は79件スキップ)を追加採取した。**JS/Python(filters.py)の優先順位食い違いを実測して記録**(§3参照) |

### M2範囲だが未fixture化(0ファイル) — 既知のギャップは解消済み

かつて `b32doc-filter.test.js` がここに記載されていたが、上表の通り `kernel/b32doc-filter.json` として採取済みになったため、このセクションは現在空である。

### M2範囲・DBスキーマ/CRUD統合テストのためfixture化不要(1ファイル)

| 旧テストファイル | 対応モジュール | 状態 | 理由 |
|---|---|---|---|
| `sync-state.test.js` | `tools/lib/sync-state.js` → `infrastructure/db/sync_state_repo.py` + migration(**M2**) | fixture化していない(意図した設計) | 内容を確認すると、`MACHINE_FIELDS`列挙とテーブル列の対応・upsert/取得といった**実SQLiteに対するスキーマ・CRUD統合テスト**であり、決定的な入出力値をNode側で固定してPython側と比較する性質のものではない。M2は同じ検証を新スキーマに対するpytest(`tests/kernel/` or 専用DBテスト)として再実装すればよく、Node実行結果のゴールデン値は不要。 |

### 後続マイルストーンの統合的振る舞い・fixture化ではなくシナリオ再実装で検証(14ファイル)

いずれも実プロセス起動・実HTTPサーバー・一時Gitリポジトリ・実ファイルI/Oを使う統合テストであることを
本文で確認済み。Python移植はこれらを**同じシナリオをpytestとして再実装**して検証する
(旧コードの実行結果をゴールデンとして固定する必要はない)。

| 旧テストファイル | 対応モジュール(M0-foundation.md 対応表) | 引き継ぐマイルストーン | 引き継ぎメモ(1行) |
|---|---|---|---|
| `sync-esa.test.js` | `download-article.js` → `infrastructure/sources/esa.py` | **M3** | `savePost`/`resolvePostPath`を実docsディレクトリ+一時sync-state.sqliteに対して実行する統合テスト。M3のesaアダプターは同じシナリオ(front matter生成・sync_policy判定・書込)をpytestで再実装して検証すること。 |
| `sync-web.test.js` | `download-web.js` → `infrastructure/sources/web.py` | **M3** | 実HTTPサーバーを起動してETag/If-None-Matchの条件付きGET・304時のリンク再抽出を検証する統合テスト。M3のWebアダプターは同じ条件付きGET契約をhttpxベースでpytest化すること。 |
| `sync-git.test.js` | `download-git.js` → `infrastructure/sources/git.py` | **M3** | 一時ディレクトリに実Gitリポジトリ(`git init`+commit)を作りshallow clone/fetch/差分コピーを検証する統合テスト。M3のGitアダプターは同じ「上流repoを都度作ってcloneさせる」統合テストをpytestで再実装すること。 |
| `orphan-detection.test.js` | `tools/verify-integrity.js`(`findOrphanCandidates`/`resolveOrphansWithSource`) → M7監査 | **M7** | 実際には純粋関数(reader/fetcherを注入する形)で、I/Oは無い。M7実装時はkernel同様パラメトリックテスト化できる可能性があるので、統合テストとして再実装する前にfixture化(旧コード実行結果をゴールデンにする)を検討する価値がある。 |
| `verify-integrity.test.js` | `tools/verify-integrity.js` → M7監査 `application/audit/integrity.py` | **M7** | 実一時ディレクトリにMarkdownを書き込み`runVerify`/`compareSourceUpdates`を実行する統合テスト。M7は同じ6状態判定シナリオをpytestで再実装すること。 |
| `find-duplicates.test.js` | `tools/find-duplicates.js` → M7監査 `application/audit/duplicates.py` | **M7** | `findSameArticle`/`findIdentical`/`findNearDuplicates`は純粋関数(Float32Arrayベクトルを直接渡す)。統合というよりkernel寄りだが、対応表上はM7監査モジュールの一部として扱われている。 |
| `check-contradictions.test.js` | `tools/check-contradictions.js` → M7監査 `application/audit/contradictions.py` | **M7** | `extractNumbers`/`detectConflict`等は純粋関数。M7が数値・否定・状態語の矛盾検出ロジックを移植する際の参照点。 |
| `backfill-metadata.test.js` | `tools/backfill-metadata.js` → M7監査 `application/audit/backfill.py` | **M7** | 実Markdownファイルを一時docsディレクトリへ書き込み`runBackfill`/`collectMarkdownFiles`を実行する統合テスト。**この`defaultExclude`の挙動自体はTask 2が実データ調査で独立に確認済み(§4参照)**であり、M7の再実装時にその除外リストとテストのフィクスチャ(esa/Archived/web/参照コーパス/git由来の各パターン)を突き合わせること。 |
| `scene-spec.test.js` | `tools/lib/scene-spec.js` → `domain/scene_spec.py` | **M7** | `validateSceneSpec`は純粋関数(SceneSpec 1.0のcross-field検証)。M7のSceneSpec検証実装時にkernel同様パラメトリックテスト化できる可能性がある。 |
| `source-verifier.test.js` | `tools/lib/source-verifier.js` → `application/visualization/source_verifier.py` | **M7** | 実一時ファイルに対し`rangeHash`と組み合わせて`verifySources`を検証する統合テスト。M7は出典ハッシュ照合・metric即中断・statement系beat除去のシナリオをpytestで再実装すること。 |
| `visualization-store.test.js` | `tools/lib/visualization-store.js` → `infrastructure/visualization/artifact_store.py` | **M7** | slug正規化・manifest構築・sha256計算を実一時ディレクトリに対して検証する統合テスト。M7のartifact_store実装時に同じシナリオを再実装すること。 |
| `visualize-render.integration.test.js` | `tools/lib/visualize-runner.js`+`visualize-python.js` → M7可視化 | **M7** | `KB_VISUALIZE_IT=1`かつ`checkVisualizeDeps().ready`の二重ゲート付きの**実Manimレンダリング**統合テスト。M7のT7.3が明示する「Manim実行テストは明示フラグ時のみ」の対象そのもの。 |
| `visualize-runner.test.js` | `tools/lib/visualize-runner.js` → `application/visualization/service.py` | **M7** | 偽Python(モック子プロセス)で`renderScene`のオーケストレーションを検証する統合テスト。M7は同じモック方式でジョブ制御・manifest生成をpytest化すること。 |
| `visualize-templates-guard.test.js` | `tools/visualize/templates/*.py`(既存Python、**同梱移設**) | **M7** | Manimテンプレート(Python、変更せず移設)のソースを正規表現でスキャンするlintテスト(LaTeX/Tex不使用・Static系がself.play/self.waitを使わない等)。旧コード実行結果のゴールデンではなく、移設後のテンプレートに対して**同じ静的スキャンをそのまま再実行**すればよい。 |

### 未確認(1ファイル)

| 旧テストファイル | 状態 | 不明点 |
|---|---|---|
| `deprecation-notice.test.js` | 未確認 | `tools/deprecation-notice.js`(`chat`/`ui`コマンドへ非推奨警告をstderrに出しMCPへの移行を案内するCLIシム)を検証するが、この`deprecation-notice.js`自体が `design/plans/M0-foundation.md` のNode→Pythonモジュール対応表に**存在しない**(移植する91行の表にも「移植しない」リストの4ファイルにも無い)。案内先の`chat`/`ui`コマンド自体は新設計でネイティブに実装される(M6/M7)ため、この非推奨シムを新システムが再現すべきかどうか対応表からは判断できない。M9のカットオーバー計画者が旧CLIの`chat`/`ui`をどう扱うか(単純に廃止/移行案内を残す)を決める際にこの点を確認する必要がある。 |

集計(28ファイル): M1でfixture化済み13(`b32doc-filter.test.js` を含む) +
M2範囲だが未fixture化(既知のギャップ)0 +
M2範囲でDB統合テストのためfixture化不要1 + 後続マイルストーンの統合シナリオとして
引き継ぐもの14(M3が3、M7が11) + 真に未確認1 = 28。

## 3. 既知のギャップ・逸脱・除外

| 項目 | 内容 | 影響 |
|---|---|---|
| 64KB超過ファイル除外 | 実データ母集団3,523件中46件が64KB超だが、層化選定の候補として実際に遭遇したのは0件(各層が目標件数を先に充足したため)。除外ロジック自体は実装・テスト済み | `real-docs/samples.json` に64KB超のファイルは含まれない。除外の仕組みは機能する状態で存在するが、今回のサンプルには適用結果として現れていない |
| 秘密情報パターンの偽陽性除外 | esa層候補の1ファイル(Pythonコード中の `script_key = f"..."` という変数代入)が `mask_secrets` の `*_KEY` サフィックスパターンに偶然一致し除外された。実際の秘密情報ではないが、「1件でも一致したら全体除外」の規定に忠実に従った | `real-docs/samples.json` から1ファイルが欠落。次点候補が繰り上がって採用されている |
| `add_web_batch` はスキーマのみ | 呼び出すと `batch-config.js` を書き換える副作用があるため、正常系を実行せず `tools/list` のスキーマのみ `skipped_reason` 付きで記録した | Python版のこのツールの入出力契約は `tools/list` のスキーマからのみ検証できる。実行結果のゴールデンは存在しない |
| `download_git` のエラーケースが空文字列 | ブリーフは「不正リポジトリURLエラー」を指定していたが、ハンドラにURL形式の事前チェックが無く、非空文字列を渡すと実際に `git clone` がspawnされてしまう。旧リポジトリ不変の制約上のリスクを避け、空文字列(zodの`min(1)`でspawn前に確実に拒否される)を採用した | Python版もこのケースでは「非空文字列を渡すと実クローンが走る」ことを認識し、テストで空文字列以外を渡す場合は別途サンドボックス隔離が必要 |
| `sdk_validation_error` の prose は比較対象外 | `download_esa_post`/`download_esa_category`/`download_esa_search`/`download_git` の4つのzodスキーマ検証エラーは `content[0].text` が `"MCP error -32602: ..."` という非JSONのプレーン文字列になる。この文言は旧リポジトリの zod + `@modelcontextprotocol/sdk` のバージョン固有の生成物 | M5の比較器は `response_kind`(`"sdk_validation_error"`)・`isError`・JSON-RPCエラーコード `-32602` の一致で判定し、`content[0].text` の文字列一致で比較してはならない |
| eval baseline は base64 でなくプレーンJSON | 他カテゴリ(kernel/real-docs/mcp)はBOM/CRLFを保持する必要がある生テキストを扱うため `*_b64` で格納するが、`eval/baseline.json` は検索結果の構造化データ(パス・スコア・行番号等)のみでBOM/CRLFを保持すべき生テキストを含まないため、素のJSON文字列で記録した | フォーマットの意図的な違いであり、欠陥ではない |
| embeddings テーブルの孤立20行 | `kb-index.sqlite` の `embeddings`(51,411件)が `chunks⋈embeddings`(51,391件)より20件多い。chunkが削除された後も残った孤立embeddings行と推測される | `gate-samples.json` の `db_counts` にそのまま記録(揃えていない)。M8での「全chunkの再利用判定」実装時、embeddings側にのみ存在する孤立行の扱いを設計判断する必要がある |
| `search_kb.diagnostics.elapsedMs` が唯一の非決定フィールド | MCP fixture 全28ケースを2回ずつ実行して構造的diffを取った結果、非決定だったのは `search_kb` の5ケース全ての `diagnostics.elapsedMs` のみ。他23ケースは完全に決定的だった | M5の比較器はこのフィールドだけを正規化(無視)すればよく、それ以外は全フィールドの一致を要求してよい |
| `b32doc-filter.js` は「食い違えば `filters.py` を正とする」と自称するが、実測すると `filters.py` 内部で2つの答えがある | `tools/lib/b32doc-filter.js` の冒頭コメントは「元の判定と食い違いが出たら `tools/knowledge-curator/filters.py` 側を正とすること」と明記する。しかし `filters.py` を実際に実行して確認すると、(a) `filters.py` が持つ `decide()` 関数(判定優先順位を1つにまとめた関数)は `not_html_category` を最初に判定する優先順位を持つが、`build_index.py` から一度も呼ばれない**死んだコード**である。(b) 実際に `procedures-index.jsonl` を生成する `build_index.py` 本体は `decide()` とは異なる優先順位(`is_excluded_path` を最初に呼ぶ)で個々の判定関数を直接呼んでいる。`tools/lib/b32doc-filter.js` の `decideIndexable()` は **(b) の実際に実行される優先順位と一致し、(a) の未使用の `decide()` とは一致しない**。加えて、目次/ランディングページ判定(`toc_or_default`)についても、`b32doc-filter.js` と `filters.decide()` は frontmatter の `source_name` を見るが、`build_index.py` 本体は実際にはディスク上のファイル名(`md_path.name`)だけを見ており、両者が食い違えば判定も食い違いうる(実データでは通常両者は一致するため実害は稀と推測されるが未検証)。両方の実測値は `kernel/b32doc-filter.json` の case id `js_vs_python_priority_order_divergence` / `js_vs_python_noise_filename_source_divergence` に、旧リポジトリの `filters.py` を実際に実行して得た値として記録してある(推測ではない) | **M2は `tools/lib/b32doc-filter.js`(=このfixtureの `decideIndexable` ケース群)の優先順位を移植すること。** これは `filters.py` の `decide()` 関数ではなく、実際にB32doc索引(`docs/knowledge/generated/b32doc`)を生成している `build_index.py` 本体の挙動と一致するため、M4のreference-index選定の実際の再現性にはこちらが正しい。`filters.py` のコメントが指す「正」は本文中では未使用の `decide()` であり、実行結果としての「正」ではない点に注意 |

## 4. 旧システムのデータ層に関する訂正事実(§9.2 を上書きする)

以下は `design/system-design.md` §9.2 の想定を覆す、実測で確認された事実である。
Task 2 の初稿はこれを「web由来コンテンツが参照コーパスに吸収される」という推測で
説明していたが、レビューが独立に検証した結果、以下の事実に基づく訂正が確定している。

- **`sync-state.sqlite` は履歴的な同期台帳ではない。** 全行の `created_at` は
  `2026-07-29T07:09:56Z`〜`2026-08-03T04:20:09Z` に収まり、`backfill-metadata.js` が
  直近に作った**1回限りのバックフィル・スナップショット**である。
- **`backfill-metadata.js` は既定で参照コーパスのパスを除外する。** 明示的な
  `include` 指定が無い場合の `defaultExclude` は
  `['knowledge/B32doc', 'knowledge/catiadoc', 'knowledge/generated', ...gitOutputDirs]`
  をハードコードしている。この1つのメカニズムだけで、`document_type='reference'`
  行が0件であることと、`web`由来の履歴コンテンツが1件も無いことの**両方**を説明できる
  (`source`列は`esa`/`git`/`manual`の3値のみ、`document_type`列に`reference`は無い)。
- **`reference-index.sqlite` は `knowledge/B32doc` だけをカバーする。**
  `b32doc-filter.js` のスコープにより7,563行全件が `knowledge/B32doc/` 始まりで、
  `knowledge/catiadoc` 配下は0件(直接クエリで確認済み)。
- **`docs/knowledge/catiadoc`(1,313ファイル、frontmatterに `source: web`・
  2026年1月クロール日時)と `docs/knowledge/generated`(4ファイル)は、
  どちらのDBにも登録されていないメタデータ孤児である。** 「参照コーパス側に
  吸収された」のではなく、`defaultExclude` によって最初からバックフィル対象外に
  なっているだけである。
- **現行の `batch-config.js` の下では web バッチは一度も実行されていない。**
  `type: 'web'` のバッチは `catiadoc`(`outputDir: docs/catiadoc`)と
  `C#ATIA`(`outputDir: C#ATIA`)の2件のみで、どちらの出力先ディレクトリも
  ディスク上に存在しない。つまり `docs/knowledge/catiadoc` は現行 `batch-config.js`
  とは別系統・別時期に取得された、管理DBに未登録のコンテンツである。

**M4/M8への帰結**(ledger 記載):
- M8 の移行は `docs/` を実ファイルベースで棚卸しする必要があり、`sync-state.sqlite`
  や `reference-index.sqlite` のどちらか一方が完全な文書一覧だと信頼してはならない。
- M4 の reference-index 選定は `knowledge/B32doc` のみのスコープを再現しなければならない。
- `design/system-design.md` §9.2 の documents テーブルは、上記の既定除外を明示するか、
  除外を外したフルツリーのバックフィルによって作られるべきである。

## 5. read-only 規律と教訓

- **旧リポジトリ(`C:\Temp\multi-source-knowledge-base`)は一切変更しない。**
  全採取スクリプトは読み取り専用。
- **`assertReadOnly()` は `docs/`・`data/` 配下の全ファイルを (size, mtime) で
  フィンガープリントする。** これらのパスは旧リポジトリの `.gitignore` 対象であり、
  `git status --porcelain` だけではほとんど何も検出できないため(実際、`docs/`
  配下にgit追跡ファイルは2つしかなく、`data/`配下は0)。フィンガープリント方式は
  decoyリポジトリでの検証(同バイト上書き・サイズ維持変更・追加・削除の全パターンで
  検知、誤検知ゼロ)で有効性を確認済み。
- **better-sqlite3 で旧インデックスDBを `readonly:true` 無しで開くと mtime が
  進む。** `data/kb-index.sqlite`・`data/reference-index.sqlite` を素朴に
  `new Database(path)` で開くだけで、ファイルサイズは不変のままmtimeだけが変化する
  ことが Task 3 で実測判明した(WALチェックポイントやSQLiteのファイル変更カウンタ
  更新が原因と推測されるが、事前ハッシュが無ければ遡って証明できない)。この
  ため kb-search の MCP 捕捉・eval baseline 捕捉・embedding ゲート捕捉のいずれも、
  **旧リポジトリ外の使い捨てサンドボックスへ対象DBを丸ごとコピーし、コピーだけを
  開く**方式を一貫して採用している(`docs/`・`node_modules/` が必要な場合は
  ディレクトリジャンクションで読み取り専用参照する)。
- **秘密情報の健全性検証は、資格情報の一部たりともエコーしてはならない。**
  Task 3 のレビュー中、トークンの長さと先頭4文字をトランスクリプトに出力してしまい、
  セキュリティ監視から「部分的な資格情報の顕在化」として指摘された事例がある
  (リポジトリには何も入らなかったが)。以後の秘密情報検証は、ハッシュ比較か
  メモリ内での完全一致判定のみを用い、資格情報の一部でも画面やログに出してはならない。

## 6. 再採取の手順

再採取の具体的なコマンド・実行順序・冪等性の確認方法は
[`tests/fixtures/capture/README.md`](capture/README.md) を参照(重複を避けるためここには
再掲しない)。

原則は1つだけ: **fixture(`tests/fixtures/**` 配下の JSON・JSONL)は手で編集しない。**
値を1つ直したいだけでも、対応する `capture-*.mjs` を直して再実行し、生成された差分を
レビューする。旧システムに変更があった場合も同様に、再採取して差分を見る。手編集を
許すと、「このfixtureは旧システムを実行して得た値である」という前提(このディレクトリ
全体の存在理由)が壊れる。
