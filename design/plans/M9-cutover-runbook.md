# M9 カットオーバー・ランブック

**この文書は実行手順そのもの。実行判断(いつ切り替えるか)は利用者が行う。**
本ランブックの作成タスク自体は `.mcp.json` の書き換え・実データ移行・
本番切替のいずれも実行していない。`design/plans/M9-mcp-cutover.md`(M5
task-4 で作成した `.mcp.json` 差替えの記録)を土台に、前提条件・検証・
監視・切り戻しまでを1本の手順書にまとめたもの。

対象読者: 実際に切り替えボタンを押す人。手元でこの通りに実行できることを
目標にしている。

## 0. 大原則

- 旧リポジトリ(`C:/Temp/multi-source-knowledge-base`)には切り替え作業以外で
  書き込まない。`.mcp.json` の書き換えだけが例外。
- 旧システムは切り替え後も**1リリース分は読み取り専用で保持**する(§10 廃止
  ゲート、設計書 §14 step 9)。削除は利用者判断であり、こちらから提案しない。
- 各手順の後に検証ステップがある。検証に失敗したら次のステップに進まない。

## 1. 事前準備(切り替えの何日か前)

### 1.1 source_id プレースホルダーの解消(必須、切り替え直前作業ではない)

移行時、旧 `sync-state.sqlite` の `source` 列にある値が新 `sources` テーブルに
まだ無いカテゴリの場合、`migration/runner.py::_import_sync_state_step` が
`type` と `output_dir`(`docs/<source_value>`)だけを持つ最小限のプレース
ホルダー行を自動生成する(接続情報・認証情報は一切引き継がない設計 —
§11.1 のとおり、旧システムから値を持ち越さない)。

これらのプレースホルダーは **同期を実行できない**(esa のチーム名/トークン、
Git のリポジトリ URL/トークンなど実際の接続設定が無いため)。カットオーバー
後に Python 版が同期を担う以上、これは移行の後片付けではなく**カットオーバー
前提条件**である:

1. `abist-kb source list --output json` でプレースホルダー行を洗い出す
   (`display_name` が旧 `source` 値のまま、`output_dir` が `docs/<値>` の
   単純な形になっている行)。
2. 各プレースホルダーに対応する実際の接続設定(esa: `ESA_TEAM_NAME`/
   `ESA_ACCESS_TOKEN` 相当、Git: リポジトリ URL・認証、Web: 巡回対象 URL)を
   `abist-kb source update`(または該当コマンド)で入力する。
3. 入力後、`abist-kb source list` で全ソースが「接続設定あり」の状態になって
   いることを確認する。ここが埋まらないうちは同期系 MCP ツール
   (`run_batch` 等)をカットオーバーしても実際には動かない。

### 1.2 移行 manifest のレビュー

`abist-kb migrate verify` の manifest に未説明の欠落がないことを確認する
(§15 受入条件の1項目、実データ移行後にのみ実測できる)。

### 1.3 バッチ定義凍結の周知

並行稼働期間中、旧 `batch-config.js` を変更しないことをチームに周知する。
Python 版は `batch-config.js` への書き戻しを実装しない方針(移植しない設計
判断)のため、並行稼働中に旧側でバッチ定義を変更すると Python 側の
`batches` テーブルと乖離し、並行比較(§3)の意味が失われる。凍結解除は
カットオーバー完了後。

## 2. 並行比較ハーネスの実行

`abist-kb audit parallel-compare` が M3 のバイト同一検証・M5 の MCP契約diff・
M4 の検索品質評価(22クエリ)を1コマンドでまとめて実行する。

```
abist-kb audit parallel-compare --output json
```

- `sync_and_collection`: `tests/sources/test_e2e_byte_identity.py` を実行し、
  esa/git のバイト同一、web の不変条件(パス集合・front matter・date)を検証
  する。旧リポジトリが無い環境では `skipped` になる。
- `mcp_contract`: `tests/mcp/test_all_server_tools_list_diff.py` を実行し、
  既存15ツールの名前・スキーマ・`taskSupport` が1つも変わっていないことを
  検証する。
- `search_quality`: 索引がある場合、22クエリの Recall@5/MRR/nDCG@10 を
  `--corpus`(既定 work)に対して測定し、直前レポートとの悪化を警告する。
  索引が無い/評価しない場合は `--skip-search-quality` で明示的に省略する。

全項目 `passed`/`ok`/`skipped`(旧リポジトリ不在起因のみ)であることを確認
してから次に進む。`failed` があれば原因を解消するまでカットオーバーしない。

## 3. `.mcp.json` の切り替え

対象は旧リポジトリの `.mcp.json`(`C:/Temp/multi-source-knowledge-base/.mcp.json`)。
既存の3キー: `kb-download` / `kb-search` / `kb-visualize`。

### 3.1 切り替え後の値

```json
{
  "mcpServers": {
    "kb-download": {
      "command": "abist-kb",
      "args": ["--root", "C:/Temp/multi-source-knowledge-base", "mcp", "serve", "kb-download"]
    },
    "kb-search": {
      "command": "abist-kb",
      "args": ["--root", "C:/Temp/multi-source-knowledge-base", "mcp", "serve", "kb-search"]
    },
    "kb-visualize": {
      "command": "abist-kb",
      "args": ["--root", "C:/Temp/multi-source-knowledge-base", "mcp", "serve", "kb-visualize"]
    }
  }
}
```

`command` が `PATH` 上で解決できない環境では、`abist-kb` を絶対パス
(`uv run --project <python repo> abist-kb` 形式、または `pip install -e` 済み
インタプリタの絶対パス)に置き換える。

**推奨: 1キーずつ切り替える。** 3キー同時ではなく `kb-search`(読み取りのみ、
最も安全)→ `kb-download`(書き込み系ツールを含む)→ `kb-visualize` の順で
1つずつ切り替え、各段で§3.2の検証を行ってから次のキーへ進む。

### 3.2 切り替え直後の検証(キーごと)

1. MCP クライアントを再起動し、対象サーバーが起動することを確認する
   (`tools/list` が返る。`tool_count` が `tests/fixtures/mcp/tools-list.json`
   と一致: kb-download 8、kb-search 4、kb-visualize 3)。
2. 非破壊な読み取り系ツールを1回ずつ呼び `ok: true` を確認する
   (`kb-search.search_kb`、`kb-download.list_batches`、
   `kb-visualize.list_scene_kinds`/`check_visualize_deps`)。
3. 書き込み系(`run_batch`/`download_web`/`render_scene` 等)は影響範囲が
   大きいため、この時点では実行せず、別途の受け入れテストとして計画する。
4. まだ切り替えていない残りのキーが元の値(`node`, 対応する `.js`)のままで
   あることを確認する(誤って一緒に切り替えていないことの確認)。

## 4. カットオーバー後の監視

切り替え後、最低1リリース分は以下を継続的に確認する:

- 同期実行のたびに `sync.totals`(added/updated/skipped/conflict/missing/
  error)を旧システム実行時の傾向と比較する。急増する `error`/`conflict` は
  source_id 接続設定(§1.1)の見落としを疑う。
- `abist-kb audit search-quality` を定期実行し、直前レポートとの悪化
  (`compare_with_previous` の警告)が出ていないか確認する。
- ログ・DB・移行成果物・テストスナップショットに秘密情報が出ていないか
  (§15 受入条件の1項目)を定期サンプリングする。
- 旧リポジトリの `git status --porcelain` 行数・`data/*.sqlite` のサイズと
  更新時刻が、Python 版の書き込みで意図せず変化していないか確認する
  (`docs/` は両実装が触れる互換フォーマットの前提だが、想定外の差分が
  出ていないかは運用中も見ておく)。

## 5. 切り戻し手順

対象キーの `.mcp.json` エントリを、§3.1 冒頭(または元の値)の
`node` + 対応する `.js` に戻し、MCP クライアントを再起動するだけでよい。

Python 側は旧リポジトリのファイルを一切書き換えない設計
(`app.sqlite`/索引DB は Python 側管理下の別ファイル)。`docs/` への書き込みは
両実装が触れる箇所のため、切り戻し前に「Python 版稼働中に書き込まれた
`docs/` の差分を旧 Node 版がどう扱うか」を確認すること。想定外の差分が
あれば、切り戻し後に手動で整合を取る。

## 6. 変更してはいけないもの

- 並行稼働期間中の旧 `batch-config.js`(§1.3 の凍結)。
- カットオーバー作業そのもの以外での旧リポジトリの書き換え
  (`docs/`, `data/`, `tools/*.js` など)。
- `npm install` の実行(旧リポジトリの `node_modules`/ロックファイルに触れない)。

## 7. 旧システムの扱い(参考、M10)

カットオーバー完了後も旧リポジトリは1リリース分**読み取り専用で保持**する。
保持期間中にロールバックが発生しなかったことを確認してから、アーカイブ化を
利用者に提案する(削除は利用者判断)。本ランブックの範囲はカットオーバーの
実行と直後の監視までであり、廃止判断そのものは対象外。
