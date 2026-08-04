# M2 ビット互換カーネル移植 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development でタスク単位に実行する。ステップは `- [ ]` で進捗管理。

**Goal:** M1 で採取したゴールデン fixture と**ビット一致**する Python 実装を作る。front matter 解析、本文ハッシュ、行範囲ハッシュ、Markdown チャンク分割、e5 埋め込み入力構築、同期判定、メタデータ分類、batch-config パーサ。

**Architecture:** 各モジュールは M1 の `tests/fixtures/kernel/*.json` と `tests/fixtures/real-docs/samples.json` を読むパラメトリックテストを先に書き、全ケース一致するまで実装する。**期待値は fixture が正**であり、実装に合わせて fixture を書き換えることは禁止。

**Tech Stack:** Python 3.12 / pytest(パラメトリック)/ Hypothesis(性質テスト)/ 標準ライブラリのみ(`hashlib`, `re`, `unicodedata`)

## Context

親計画: `C:\Users\a1199118\.claude\plans\design-system-design-md-push-glowing-tulip.md`。設計の正は [../system-design.md](../system-design.md) §5.1(パッケージ構成)、§9(データ設計)、§11.2(埋め込み再利用ゲート)、§13.1(単体・契約テスト)。

**なぜこれが最重要ゲートなのか。** M3 以降のすべて(収集の差分判定、索引のチャンク境界、埋め込みの再利用可否、検索の出典行、移行の検証)がこの層の出力に依存する。ここが1バイトでもずれると、下流の不一致が「どこで生じたか」を追えなくなる。設計書 §14 の手順2が独立した工程として置かれているのはこのためである。

**M1 で確立済みの前提**(そのまま使う):
- `tests/fixtures/kernel/*.json` — 190ケースのゴールデン。旧 Node コードを実行して採取済み。
- `tests/fixtures/real-docs/samples.json` — 実 `docs/` からの層化40件。BOM 2件、CRLF 5件、日本語パス5件を含む。
- `tests/fixtures/PROVENANCE.md` — 何が採取され、何が意図的に採取されなかったかの恒久記録。**着手前に必読。**
- fixture の入力は base64。`tests/fixtures/.gitattributes` が改行変換を禁止している。
- `tests/fixtures_check/` の 242 テストが fixture 自体の完全性を守っている。M2 はこれを壊してはならない。

**M0 で確立済みの前提**:
- `src/abist_kb/` レイアウト、`identity.py`、`Settings`、`AppError`/`ErrorCode`/`ExitCode`、`domain/redaction.py::mask_secrets`、`infrastructure/db/`(接続・マイグレーション)、`presentation/`(Console・CLI・AppTyper)。
- **`AppTyper` の落とし穴**: サブコマンド群は `typer.Typer()` ではなく `AppTyper()` で作ること(`design/plans/M0-foundation.md` の申し送り参照)。M2 で CLI を足す場合に該当。
- 例外は `AppError` に正規化。ロック競合は `CONFLICT`/retryable、恒久エラーは `FAILURE`/非 retryable。

## Global Constraints(全タスク共通)

- **fixture は正解であり、変更してはならない。** テストが落ちたら実装を直す。fixture の期待値が間違っていると考える根拠がある場合は、実装を進めずに報告する(旧 Node コードで再検証が必要になる)。
- **旧リポジトリ `C:\Temp\multi-source-knowledge-base` には触れない。** M2 は fixture だけで完結する。旧コードを読む必要が生じたら読むのは可(read-only)だが、実行や書き込みはしない。
- Python 3.12、標準ライブラリ中心。正規表現は `re`。YAML ライブラリは**使わない**(front matter はバイト保存パースであり、YAML 解釈は不一致の原因になる)。
- 文字列処理はコードポイント単位を意識する。CJK 判定・255バイト切詰・512文字切詰は、それぞれ「コードポイント」「UTF-8 バイト」「コードポイント」で数える基準が異なるので、fixture で確認しながら実装する。
- 各モジュールは純粋関数中心。I/O は呼び出し側が担う(テスト容易性のため)。
- ruff + format クリーン、`uv run pytest -q` 全通過を各タスクのコミット条件とする。
- TDD: パラメトリックテストを先に書き、**赤を確認してから**実装する。「テストを書いた」と「赤を見た」は別物。

## ファイル構成

```
src/abist_kb/
  domain/
    frontmatter.py        # T1: バイト保存パース、hash_body
    line_range.py         # T2: range_hash
    sync_policy.py        # T3: 11アクションの判定
    metadata_schema.py    # T4: 分類・sanitize 一本化
    b32doc_filter.py      # T4: 参照コーパス選別
  infrastructure/search/
    chunker.py            # T5: 見出しベース分割
    e5_input.py           # T6: 埋め込み入力構築
  migration/
    batch_config_parser.py # T7: JS リテラル限定パーサ
tests/kernel/
  conftest.py             # fixture ローダ(base64 デコード等の共通処理)
  test_frontmatter.py  test_line_range.py  test_sync_policy.py
  test_metadata_schema.py  test_chunker.py  test_e5_input.py
  test_batch_config_parser.py
  test_real_docs.py       # 実データ40件を全モジュールへ通す横断テスト
```

---

## Task 1: fixture ローダと frontmatter

**Files:**
- Create: `src/abist_kb/domain/frontmatter.py`, `tests/kernel/conftest.py`, `tests/kernel/test_frontmatter.py`

**Interfaces:**
- Produces: `conftest.py` に `load_kernel_fixture(name) -> dict`(`tests/fixtures/kernel/<name>.json` を読む)、`b64d(s) -> str`(base64 → UTF-8 文字列)、`b64d_bytes(s) -> bytes`
- Produces: `parse_frontmatter(text: str) -> FrontmatterResult`。`FrontmatterResult` は frozen dataclass で `has_frontmatter: bool`, `bom: str`, `eol: str`, `data: dict[str, str]`, `keys: tuple[str, ...]`, `block_keys: tuple[str, ...]`, `body: str`, `raw: str`
- Produces: `body_of(text) -> str`, `hash_body(text) -> str`, `sha256_hex(text) -> str`, `set_frontmatter_values(text, values) -> str`, `serialize_scalar(value) -> str`

- [ ] **Step 1: fixture ローダを書く**

`tests/kernel/conftest.py`。`tests/fixtures/kernel/` を `pathlib` で解決し、`json.loads` で読む。base64 デコードは `base64.b64decode(s).decode("utf-8")`。**BOM を除去しないこと**(`utf-8` であって `utf-8-sig` ではない)。fixture が見つからない場合は明示的に失敗させる(スキップしない)。

- [ ] **Step 2: frontmatter のパラメトリックテストを書く**

`tests/fixtures/kernel/frontmatter.json` の全ケースに対し `pytest.mark.parametrize` で回す。各ケースの `expected` にある全フィールドを検証: `hasFrontmatter`, `bomPresent`, `bom_b64`, `eol`, `data`, `keys`, `blockKeys`, `body`(base64 デコードして比較), `raw`, `hashBody`, `setFrontmatterValues` の結果。

**キー名の対応に注意**: fixture は Node 由来なので camelCase(`hasFrontmatter`)、Python 側は snake_case(`has_frontmatter`)。テスト内で明示的にマッピングする。

- [ ] **Step 3: 赤を確認**

Run: `uv run pytest tests/kernel/test_frontmatter.py -q`
Expected: 全件 FAIL(`ModuleNotFoundError: abist_kb.domain.frontmatter`)

- [ ] **Step 4: frontmatter を実装**

仕様(fixture が正、以下は理解の助け):
- 先頭の BOM(`\ufeff`)を1個だけ剥がし、`bom` に保持する。
- 行分割は `\n`。各行の末尾 `\r` は保持したまま扱い、`eol` は最初の行区切りから判定する(`\r\n` か `\n`)。
- 1行目が `---`(前後空白許容)なら front matter。次の `---` 行までを走査する。閉じられていなければ front matter 無しとして全体を本文にする。
- `key: value` 形式を素直にパースする。値がその行で完結しないもの(ブロックスカラー、次行がインデントされた入れ子)は `block_keys` に記録し、`data` には入れず、書き換え対象からも外す。
- `body` は front matter ブロックの直後から。**BOM は除外、元の改行は維持**。
- `hash_body(text) = sha256(body_of(text).encode("utf-8")).hexdigest()`。
- `set_frontmatter_values` は既存キーを置換、無いキーは末尾に追加。`block_keys` のキーは触らない。

- [ ] **Step 5: 緑を確認**

Run: `uv run pytest tests/kernel/test_frontmatter.py -q`
Expected: 全件 PASS

- [ ] **Step 6: Hypothesis 性質テストを追加**

任意の(BOM 有無 × LF/CRLF 混在 × 日本語 × front matter 有無)テキストに対し:
- `parse_frontmatter(t).raw == t`(往復でバイト同一)
- front matter だけを書き換えても `hash_body` が変わらない
- 本文の改行を LF↔CRLF に変えると `hash_body` が変わる

- [ ] **Step 7: コミット**

```bash
git add src/abist_kb/domain/frontmatter.py tests/kernel/
git commit -m "$(cat <<'EOF'
feat(m2): front matter のバイト保存パースを M1 ゴールデンとビット一致で実装

YAML ライブラリを使わず行単位で解析し、BOM・行別 EOL・ブロック値を保持する。
hash_body は front matter と BOM を除外し元の改行を維持した本文の SHA-256。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: line_range

**Files:**
- Create: `src/abist_kb/domain/line_range.py`, `tests/kernel/test_line_range.py`

**Interfaces:**
- Consumes: T1 の fixture ローダ
- Produces: `range_hash(text: str, start_line: int, end_line: int) -> str`(1始まり閉区間)

- [ ] **Step 1: パラメトリックテストを書く**

`tests/fixtures/kernel/line-range.json` の全ケース。BOM 有無 × CRLF/LF × 境界(先頭行・末尾行・単一行)を網羅している。

- [ ] **Step 2: 赤を確認**

Run: `uv run pytest tests/kernel/test_line_range.py -q` → FAIL

- [ ] **Step 3: 実装**

手順は厳密に: 先頭 BOM を1個除去 → `\n` で分割 → 各行の末尾 `\r` を除去 → `lines[start-1:end]` を取り出す → `\n` で join(末尾改行は付けない、trim しない)→ UTF-8 で SHA-256。

**front matter は除去しない。** 行番号は物理行であり、これが `get_document` の出典行・SceneSpec の `content_hash`・source-verifier の三者で共有される契約になる。

- [ ] **Step 4: 緑を確認** → PASS

- [ ] **Step 5: コミット**

---

## Task 3: sync_policy

**Files:**
- Create: `src/abist_kb/domain/sync_policy.py`, `tests/kernel/test_sync_policy.py`

**Interfaces:**
- Produces: `SyncAction` 列挙(`CREATE, UPDATE, UNCHANGED, LOCAL_MODIFIED, CONFLICT, CONFLICT_OVERWRITTEN, ADOPT, UNKNOWN_LOCAL, MISSING, ORPHAN, ERROR`)
- Produces: `SyncStatus` 列挙(`SYNCED, MODIFIED_LOCAL, CONFLICT, SOURCE_MISSING, ERROR`)
- Produces: `decide_sync_action(*, remote, record, local, force=False) -> SyncDecision`(`SyncDecision` は `action`, `write: bool`, 付随情報を持つ frozen dataclass)
- Produces: `decide_missing_candidate(*, full_sync_succeeded, missing_count, threshold, individual_fetch_failed) -> MissingDecision`
- Produces: `ACTION_BUCKET: Mapping[SyncAction, str]`(added/updated/skipped/conflict/missing/error の6分類)
- Produces: `sync_status_for(action) -> SyncStatus`

- [ ] **Step 1: パラメトリックテストを書く**

`tests/fixtures/kernel/sync-planner.json` の19ケース。11アクション全部、force、hash 優先 vs updated_at 比較、`decide_missing_candidate` の3条件、`ACTION_BUCKET` 全体を含む。

- [ ] **Step 2: 赤を確認** → FAIL

- [ ] **Step 3: 実装**

判定表(fixture が正):

| | ローカル未変更 | ローカル変更あり |
|---|---|---|
| リモート未変更 | `UNCHANGED` | `LOCAL_MODIFIED`(書かない) |
| リモート変更あり | `UPDATE` | `CONFLICT`(書かない。force なら `CONFLICT_OVERWRITTEN`) |

加えて: ローカルファイル無し → `CREATE`。DB レコード無し → `ADOPT`(記録のみ、上書きしない)。`local_content_hash` が None → `UNKNOWN_LOCAL`。

`is_remote_changed` の優先順位: `remote.content_hash != record.source_content_hash` を優先。どちらか欠けていれば `updated_at` の比較。どちらも無ければ「変更あり」とみなす。

`decide_missing_candidate` は**3条件すべて**が成立したときだけ `source_missing` を提案する。`change_status` は常に False(業務ステータスは人間の判断)。

- [ ] **Step 4: 緑を確認** → PASS

- [ ] **Step 5: コミット**

---

## Task 4: metadata_schema と b32doc_filter

**Files:**
- Create: `src/abist_kb/domain/metadata_schema.py`, `src/abist_kb/domain/b32doc_filter.py`, `tests/kernel/test_metadata_schema.py`

**Interfaces:**
- Produces: 列挙 `Source`(esa/web/git/manual)、`DocumentType`(meeting/specification/knowledge/memo/reference)、`Status`(active/deprecated/archived)、`ManagedBy`
- Produces: `FRONTMATTER_KEY_ORDER: tuple[str, ...]`、`BACKFILL_KEYS`、`REFERENCE_CORPUS_PREFIXES`
- Produces: `classify_document(path, frontmatter_data) -> DocumentClassification`
- Produces: `is_reference_corpus(path) -> bool`
- Produces: `safe_batch_name(name) -> str`、`sanitize_file_name(name) -> str`、`sanitize_category_path(path) -> str`、`extract_repo_name(url) -> str`
- Produces: `select_b32doc_targets(...)`(参照コーパス選別。除外理由を返す)

- [ ] **Step 1: パラメトリックテストを書く**

`tests/fixtures/kernel/metadata-schema.json` の68ケース。

**重要な区別**(PROVENANCE.md にも記録済み): `safe_batch_name` と `sanitize_file_name` は**別物**である。前者は4つの置換のみ。後者はそれに加えて Windows 予約名の `file-` 接頭辞と 255 **バイト**(UTF-8)での切詰を行う。旧実装で3箇所に重複していたものを1箇所へ集約するが、**振る舞いの違いは維持する**。統合して片方に寄せてはならない。

- [ ] **Step 2: 赤を確認** → FAIL

- [ ] **Step 3: 実装**

`sanitize_file_name` の切詰は UTF-8 バイト数で数え、マルチバイト文字の途中で切らないこと。切り詰めた場合は `...` を付ける(fixture で確認)。

`b32doc_filter` は旧 `b32doc-filter.js` の移植だが、その元は `tools/knowledge-curator/filters.py` である。除外規則(パス断片、`toc.htm`/`default.htm`、front matter の `category != 'html'`、`language` が ja/mixed 以外、`## Extracted Content` が空または 200 文字未満)を実装し、**除外理由を返す**(黙って落とさない)。

- [ ] **Step 4: 緑を確認** → PASS

- [ ] **Step 5: コミット**

---

## Task 5: chunker

**Files:**
- Create: `src/abist_kb/infrastructure/search/chunker.py`, `tests/kernel/test_chunker.py`

**Interfaces:**
- Produces: `DEFAULT_CHUNK_OPTIONS`(`max_tokens=800`, `hard_max_tokens=4000`, `min_tokens=40`)
- Produces: `estimate_tokens(text: str) -> int`
- Produces: `chunk_markdown(text: str, options=None) -> list[Chunk]`。`Chunk` は `index`, `heading_path`, `text`, `start_line`, `end_line`, `token_estimate`, `content_hash`

- [ ] **Step 1: パラメトリックテストを書く**

`tests/fixtures/kernel/chunker.json` の24ケース(18シナリオ+`default_chunk_options`等)。加えて `tests/fixtures/real-docs/samples.json` の40件でもチャンク出力を検証する。

- [ ] **Step 2: 赤を確認** → FAIL

- [ ] **Step 3: 実装**

順に:
1. `\n` で分割し各行末の `\r` を除去。チャンクの `text` は LF 結合。**行番号は元ファイルの物理行**。
2. 1行目が `---`(前後空白許容)なら次の `---` を探し、`body_start_line = i + 2`。閉じなければ1行目から本文扱い。**行番号は front matter を含む**。
3. コードフェンス(` ``` ` または `~~~`、3個以上、同じ文字でのみ閉じる)の**外側**にある `#{1,6}\s+` を見出しとしてセクション化。見出しスタックは `top.level >= level` の間 pop。`heading_path` は ` > ` 結合。
4. **見出しのみのセクションは次の内容ありセクションへ前置**する。`start_line` は最初の保留セクションのものを使う。連続する見出しのみセクションはすべて保持する(行範囲を連続させるため)。`min_tokens` は**意図的に使わない**(短いセクションを併合すると `heading_path` が壊れるため)。
5. `estimate_tokens(section) > max_tokens` のときだけ分割。分割候補は「前行が空行、当行が非空、フェンス外、表の行(`^\s*\|`)でない」位置。貪欲に、累積が `max_tokens` 以上になる最初の候補で切る。**表とコードブロックは分割されない**。候補が無ければサイズに関係なく1チャンク。
6. 候補が無い巨大ブロックは `hard_max_tokens` で強制分割。
7. 末尾の空行を除去。`end_line = start_line + len(kept) - 1`。空チャンクは捨てる。
8. **チャンク間のオーバーラップは無い。**

`estimate_tokens`: 対象 Unicode 範囲(`U+3000-30FF`, `U+3400-4DBF`, `U+4E00-9FFF`, `U+F900-FAFF`, `U+FF00-FFEF`)を CJK として数え、それ以外を other として、`ceil(cjk + other / 4)`。**コードポイント単位**で走査する。

`content_hash`: 先頭行が `#{1,6}\s` にマッチすれば除去 → 各行の末尾空白除去 → 3連以上の改行を2つへ圧縮 → `strip()` → SHA-256。

- [ ] **Step 4: 緑を確認** → PASS

- [ ] **Step 5: 実データで検証**

Run: `uv run pytest tests/kernel/test_real_docs.py -q -k chunk`
実データ40件のチャンク出力が fixture と一致すること。ここで落ちる場合、合成ケースでは現れない実データ固有の構造(深い見出し、混在フェンス、巨大表)が原因なので、fixture を疑う前に実装を疑う。

- [ ] **Step 6: コミット**

---

## Task 6: e5_input

**Files:**
- Create: `src/abist_kb/infrastructure/search/e5_input.py`, `tests/kernel/test_e5_input.py`

**Interfaces:**
- Produces: `EMBEDDING_MODELS: Mapping[str, EmbeddingModelSpec]`(`intfloat/multilingual-e5-small` 384次元・512字・`passage: `/`query: `、e5-base、OpenAI 2種)
- Produces: `embedding_input(chunk, model) -> str`、`query_input(query, model) -> str`、`input_hash(model, text) -> str`

- [ ] **Step 1: パラメトリックテストを書く**

`tests/fixtures/kernel/embeddings.json` の15ケース。**512文字境界をまたぐケースが含まれている**ことを確認し、それを明示的にアサートするテストも書く(順序を逆にすると壊れることを保証する)。

- [ ] **Step 2: 赤を確認** → FAIL

- [ ] **Step 3: 実装**

順序が命: `[title, heading_path, text]` のうち偽値でないものを `\n` で結合 → `[:max_input_chars]` で切詰 → **その後**に接頭辞を付ける。接頭辞は 512 に**数えない**。

`input_hash = sha256((model + "\n" + input).encode("utf-8")).hexdigest()`。

モデル名が未知の場合のフォールバック(`text-embedding-` 接頭辞でプロバイダ推定、`dimensions=None`、接頭辞なし、`max_input_chars=2000`)も fixture にあれば再現する。

- [ ] **Step 4: 緑を確認** → PASS

- [ ] **Step 5: 埋め込みゲート素材との突合**

`tests/fixtures/embedding/gate-samples.json` の100件について、記録された e5 入力文字列と `input_hash` を Python 実装が再現することを検証する。**これが M8 の埋め込み再利用ゲートの前提条件**(§11.2 が要求する「同一入力から content_hash と input_hash が完全一致する」の Python 側)。ここが通らなければ埋め込み再利用の判定へ進めない。

- [ ] **Step 6: コミット**

---

## Task 7: batch_config_parser

**Files:**
- Create: `src/abist_kb/migration/batch_config_parser.py`, `tests/kernel/test_batch_config_parser.py`

**Interfaces:**
- Produces: `parse_batch_config(text: str) -> dict`
- Produces: `UNSUPPORTED_BATCH_CONFIG` は `ErrorCode` の値として使う(既存になければ追加)

- [ ] **Step 1: パラメトリックテストを書く**

`tests/fixtures/kernel/batch-config.json` の6ケース。特に:
- `format_config_synthetic_roundtrip` — **空配列 `[]`、値 `0`(maxDepth/delay)、引用符入りキー `quote'in'name` と値 `path/with'quote`** を含む。M1 のレビューで、これらを取りこぼすパーサが実ファイルのケースだけなら通ってしまうことが実証されている。
- `real_batch_config_roundtrip` — 実物の `batch-config.js` のパース結果。

加えて、パースを**拒否すべき**入力のテストを書く: 式(`1 + 1`)、関数呼び出し、テンプレート文字列、変数参照。いずれも `AppError(UNSUPPORTED_BATCH_CONFIG)` を投げること。

- [ ] **Step 2: 赤を確認** → FAIL

- [ ] **Step 3: 実装**

`export const batchConfigs = ` から末尾の `;` までを抽出し、**リテラルのみ**を受け付ける再帰下降パーサを書く。受け付けるのはオブジェクト、配列、文字列(単引用符・二重引用符、`\'` `\"` `\\` `\n` 等のエスケープ)、数値、真偽値、`null`。それ以外のトークンに出会ったら即座に `UNSUPPORTED_BATCH_CONFIG` で停止する。

**JavaScript を実行しない。** `eval` 相当、`json5` ライブラリでの緩いパース、正規表現での場当たり的な抽出はいずれも不可。設計書 §11.2 が明示的に禁じている。

エラーメッセージには問題のあった位置(行・列)と、利用者が修正済み JSON5/JSON を指定して再実行できる旨の `hint` を含める。

- [ ] **Step 4: 緑を確認** → PASS

- [ ] **Step 5: コミット**

---

## Task 8: 実データ横断テストと M2 ゲート

**Files:**
- Create: `tests/kernel/test_real_docs.py`(既に部分的に作られていれば統合)

- [ ] **Step 1: 実データ40件を全モジュールへ通す**

`tests/fixtures/real-docs/samples.json` の各サンプルについて、記録されている全カーネル出力(`parse_frontmatter` 結果、`hash_body`、チャンク列、`range_hash` 複数、e5 入力と `input_hash`)を Python 実装が再現することを検証する。

これは合成ケースでは出ない実データ固有の構造を突く。BOM 2件・CRLF 5件・日本語パス5件・最長パス3件が含まれている。

- [ ] **Step 2: M2 ゲートを確認**

以下すべてが通ること:
```bash
uv run pytest tests/kernel -q          # M1 ゴールデン全件一致
uv run pytest tests/fixtures_check -q  # fixture 完全性(壊していない)
uv run pytest -q                       # 全体
uv run ruff check .
uv run ruff format --check .
```

- [ ] **Step 3: 埋め込みゲート前提条件の記録**

`tests/fixtures/embedding/gate-samples.json` の100件で e5 入力と `input_hash` が一致することを確認し、その事実を `design/plans/M2-bit-compatible-kernel.md` の末尾か `tests/fixtures/PROVENANCE.md` に追記する。M8 がこの前提の上で cosine 判定に進む。

- [ ] **Step 4: コミット**

---

## Verification(M2 ゲート)

1. `tests/fixtures/kernel/*.json` の全190ケースが Python 実装とビット一致。
2. `tests/fixtures/real-docs/samples.json` の40件が全カーネル出力で一致。
3. `tests/fixtures/embedding/gate-samples.json` の100件で e5 入力と `input_hash` が一致。
4. Hypothesis 性質テスト(front matter 往復、行範囲、CRLF/BOM/日本語)が通る。
5. `batch_config_parser` が実 `batch-config.js` を解析でき、JS 式を含む入力を `UNSUPPORTED_BATCH_CONFIG` で拒否する。
6. `tests/fixtures_check/` の242テストが引き続き全通過(fixture を壊していない)。
7. ruff・format クリーン、Windows と Linux の CI 両方で緑。

**このゲートを満たさないうちは M3 に着手しない。** 下流のすべてがこの層の出力に依存しており、ここを未検証のまま進めると不一致の原因追跡が不可能になる。

## 次のマイルストーン

M2 完了後は **M3(収集・バッチ・永続ジョブ・CLI)**。M3 の計画を書く前に `tests/fixtures/PROVENANCE.md` の「後続マイルストーンが再現すべきもの」節を読むこと — 旧 `test/sync-{esa,web,git}.test.js` と `orphan-detection.test.js` のシナリオは fixture ではなく pytest として再実装する対象であり、M1 では意図的に採取していない。
