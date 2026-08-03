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
| kernel | `tests/fixtures/kernel/*.json` | ビット互換 | frontmatter / chunker / line-range / e5入力 / sync-planner / metadata-schema / batch-config の関数単位の入出力 | M2(カーネル移植の合否そのもの) |
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

## 2. 転記カバレッジ・チェックリスト

旧システムには `test/*.test.js` が28ファイルある。**fixture ディレクトリは存在せず、
すべてインライン定数**だったため、Task 1 がその一部を新リポジトリへ転記した。
以下は28ファイル全件の状態。「未確認」は、いずれのタスク報告にも記載が無く、
本ファイル作成時点で判断材料が無いことを意味する(採取しなかったと断定しているのではない)。

| 旧テストファイル | 状態 | 転記/採取先 | 理由 |
|---|---|---|---|
| `frontmatter.test.js` | 転記済み | `kernel/frontmatter.json`(36ケース) | ESA_MIXED/WEB_CRLF/B32DOC_BLOCK/NO_FRONTMATTER/EMPTY/BOM_DOC 等を行番号付きで転記し、`parseFrontmatter`/`hashBody`/`setFrontmatterValues`を実行 |
| `chunker.test.js` | 転記済み | `kernel/chunker.json`(構造18+単体6=24ケース) | 全 `test(...)` ブロックを転記・実行。ブリーフの「14ケース」目安との差は展開(1テストが複数呼び出しを含む場合の分割)による |
| `line-range.test.js` | 転記済み | `kernel/line-range.json`(24ケース) | 旧テスト8ケース全転記+BOM有無×CRLF/LF×境界の16ケースを追加実行 |
| `sync-planner.test.js` | 転記済み | `kernel/sync-planner.json`(19ケース) | `decideSyncAction`11アクション全部・`decideMissingCandidate`4シナリオ・`SYNC_ACTIONS`を転記・実行。`ACTION_BUCKET`は非公開定数のため`recordSyncResult`経由で間接導出 |
| `metadata-schema.test.js` | 転記済み | `kernel/metadata-schema.json`(68ケースの一部) | `classifyDocument`全シナリオ・列挙値9個等を転記・実行 |
| `downloaders.test.js` | 部分転記 | `kernel/metadata-schema.json`(sanitize系) | `sanitizeFileName`/`sanitizeCategoryPath`は転記・実行(Windows予約名・255バイト切詰・日本語ケースを追加)。ただし「受入条件6: 月別フォルダが再生成されない」テストと「直接実行でなければmainが走らない」テスト(108-161行)は**意図的にスキップ**——これらは旧JSソースの正規表現スキャンであり、Node実行結果のゴールデン値ではないため |
| `batch-config-store.test.js` | 部分転記 | `kernel/batch-config.json` | `formatConfig`/`loadBatchConfigsFresh`のロジックは転記・実行。旧テストの一時ディレクトリ利用テスト(save→load、一時ファイル残留無し、上書き保存)自体は**スキップ**(ファイルI/O挙動のテストでロジック自体の価値が薄いと判断)。ただしレビュー指摘を受け、serialize出力だけでなく save→load ラウンドトリップの合成ケース(空配列・ゼロ値・クォート付きキー)を別途追加している |
| `b32doc-filter.test.js` | 対象外(意図的) | なし | Task 1 のブリーフ(Step 2/3)のリストに無いため転記対象外と判断された。ただし `b32doc-filter.js` のスコープ(`knowledge/B32doc`のみ)自体は Task 2 が `reference-index.sqlite` の実データ調査で独立に確認している(§4参照) |
| `kb-download-mcp.test.js` | 転記せず・別方式で捕捉 | `mcp/kb-download/**` | インライン定数の転記ではなく、Task 3 が実サーバーへ `tools/call` を送信し生 JSON-RPC 応答を直接記録した(旧テストの assertion を読むより実行結果そのものを記録する方が正確という判断) |
| `kb-search-mcp.test.js` | 転記せず・別方式で捕捉 | `mcp/kb-search/**` | 同上 |
| `kb-visualize-mcp.test.js` | 転記せず・別方式で捕捉 | `mcp/kb-visualize/**` | 同上 |
| `search-engine.test.js` | 転記せず・別方式で捕捉 | `eval/baseline.json` | インライン定数は転記していないが、Task 4 が `search-engine.js` の `search`/`keywordSearch` を実クエリ(`eval/queries.jsonl`)で直接実行し、ランキング・指標を記録した |
| `find-duplicates.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `backfill-metadata.test.js` | 未確認 | — | いずれの報告にも記載なし(ただし `backfill-metadata.js` 自体の `defaultExclude` 挙動は Task 2 がデータ調査で独立に確認。§4参照) |
| `check-contradictions.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `deprecation-notice.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `orphan-detection.test.js` | 未確認 | — | いずれの報告にも記載なし(ただし `embeddings` の孤立20行は Task 5 が独立に発見。§3参照) |
| `scene-spec.test.js` | 未確認 | — | いずれの報告にも記載なし(`render_scene`のINVALID_SCENE_SPEC/SOURCE_HASH_MISMATCHはTask3が実行時エラーとして捕捉したが、このテストファイルからの転記ではない) |
| `source-verifier.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `sync-esa.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `sync-git.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `sync-state.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `sync-web.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `verify-integrity.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `visualization-store.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `visualize-render.integration.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `visualize-runner.test.js` | 未確認 | — | いずれの報告にも記載なし |
| `visualize-templates-guard.test.js` | 未確認 | — | いずれの報告にも記載なし |

集計: 転記済み5 + 部分転記2 + 意図的対象外1 + 別方式で捕捉4 = **12ファイルの状態を報告から判定できた**。
残り**16ファイルは未確認**(28ファイル中)。M2以降でこれらのテストが参照する関数を移植する際は、
未確認のファイルを個別に読み、必要なら追加の capture スクリプトで採取すること。

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
