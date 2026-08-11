# 可視化復旧で見つかった範囲外の問題・判断記録

> `feat/visualize-recovery-and-expansion` の Phase 0〜4 実施中に確認した記録。
> 元は `.superpowers/sdd/` 配下に置いたが、そこは gitignore 対象で消えうるため、
> Phase 5〜7 の実施に必要な判断根拠として追跡対象へ移した。

`feat/visualize-recovery-and-expansion` の作業中に確認した、本ブランチの
スコープ外だが記録しておくべき問題。いずれも本ブランチでは修正していない。

## 1. 同期 render が worker のキュー消費全体を止めうる（重大）

`src/abist_kb/infrastructure/jobs/execution.py:279-286` は
`leases.acquire_resource_lease` を **`wait` 引数なし**で呼ぶ。
`src/abist_kb/infrastructure/jobs/leases.py:188` の既定は `wait: bool = True`
（`timeout` 既定は `None` = 無限待ち）。

そして `supervisor.py` の `tick()` はハンドラを同期呼び出しする。したがって:

> 同期 MCP `render_scene` が `render` リースを最大10分保持している間、
> `worker run` が render ジョブを claim すると `tick()` が最大10分ブロックし、
> **docs-write / corpus-write を含む全ジョブ種別のキュー消費が停止する。**

`repository.py` の `claim` は「必要リソースが空いているか」を見ずに先頭の
queued を取るため、ジョブ側では回避できない。

最小の緩和策: `run_job` に `lease_timeout` を渡し、`CONFLICT` になったら
`finish(FAILED)` ではなく queued へ差し戻す（`JobRepository.requeue`）。
恒久策は `claim` に「保持中のリソースを要求する kind をスキップする」条件を足すこと。

## 2. `test_handler_writes_stop_after_resource_lease_is_stolen_mid_run` が
   タイミング依存なのにマークされていない

`tests/jobs/test_multiprocess_leases.py:727`。実プロセス2つを跨ぎ、
「0.1秒間隔で20回書き込む間に t=0.35秒でリースを奪う」という壁時計前提で動く。

本ブランチの作業中、`pytest tests/mcp tests/cli tests/api tests/jobs` の並行実行で
1度だけ失敗した。以下より **本ブランチの変更が原因ではない**と判断した:

- 単体実行では成功
- 変更前（`git stash`）の tree でも同スイートは 12 passed ×3 で成功
- 同じスコープを変更後に2回再実行して 346 passed ×2（再現せず）
- 本ブランチの jobs 系変更は presentation 層の直列化のみで、
  lease / execution の経路に触れていない

同ファイル `:424` の `test_corpus_write_different_corpora_do_not_contend` は
同種のリスクを認めて `@pytest.mark.timing_sensitive` を付けている。
このテストにも同じマークを付けるのが一貫するが、マークすると本物の回帰を
見逃す可能性もあるため、判断は保留して記録に留める。

## 3. `scripts/bootstrap.bat` の `echo ==>` がリダイレクトだった（本ブランチで修正済み）

batch では `echo ==> text` は「`echo ==` を `text` という名前のファイルへ書き出す」と
解釈される。実行するとリポジトリルートに `uv` / `abist-kb` / `document` / `index` /
`skipped` といった空ファイルが散らかる。

本ブランチで `echo ==^> ` へエスケープして修正した（`bootstrap.bat` 8箇所、
新規の `bootstrap-visualize.bat` 7箇所）。既存の `^(dry-run^)` と同じ流儀。

## 4. Phase 6-b（flow への自動レイヤリング適用）は必要性が実証された

Phase 3 で `decision` を実装したあと、分岐フローを実レンダリングして確認した
（`reports/visualizations/_phase3b/`）。結果は計画の予測どおり:

> `許容差内?` の分岐先（`自動確定` / `手動レビュー`）が **beat 順に左から右へ**
> 並び、分岐に見えない。`いいえ` の矢印は箱の上を大きく迂回する弧になる。

`decision` は分岐を書くための beat type なので、4個折り返しレイアウトのままだと
`decision` を使うほど図が読みにくくなる。**Phase 6-b（`assign_ranks` による
自動レイヤリングを flow にも適用）は実施すべき**と結論する。

ただし後方互換は直線フローに限られる（分岐・合流・循環・孤立ノードを含む
既存 spec は配置が変わる）。計画の Phase 6-b にある画像確認表に沿って
5ケースを目視してから採否を決めること。

## 5. `tests/visualize/test_schema_compat.py` の件数はローカル成果物に依存する

`reports/visualizations/` を走査する parametrize なので、レンダリングするほど
テスト件数が増える。CI（クリーンチェックアウト）では成果物が無いので skip される。
「全体テスト件数」を回帰の指標に使うときはこの揺れを考慮すること。

## 6. リポジトリの行末が混在している（`.gitattributes` が無い）

大半のファイルは LF だが、以下は CRLF で管理されている:

- `src/abist_kb/presentation/mcp/kb_admin.py`
- `tests/visualize/test_real_render.py`
- `tests/visualize/test_scene_spec.py`
- `tests/visualize/test_source_verifier.py`

`.gitattributes` が無く `core.autocrlf=false` なので、編集ツールによっては
ファイル全体が書き換わったように見える巨大な差分が生まれる（本ブランチでも
一度発生させ、各ファイルの元の行末へ戻して解消した）。

`.gitattributes` に `* text=auto eol=lf` 等を入れて一度正規化するのが望ましいが、
全ファイルに触れる変更なので本ブランチのスコープ外とした。
