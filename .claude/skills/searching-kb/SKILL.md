---
name: searching-kb
description: Use when answering a question from this repository's knowledge base (docs/ 配下の esa 記事・議事録・仕様・メモ) — e.g. 「蛇腹の要件はどうなっている？」「#398 の対応状況は？」「CATIAの起動を速くする方法は？」. Covers the kb-search MCP tools (search_kb / get_document / get_chunk / index_status) and the rule that every answer must carry its source. NOT for downloading or updating documents — use downloading-kb-docs or managing-esa-posts for that.
---

# ナレッジベースを検索して出典付きで答える

## 原則

**根拠のない回答を出さない。** 検索結果を読んだうえで、必ず出典（ファイルパスと行番号）を添える。
見つからなかった場合は「見つからなかった」と言う。推測で埋めない。

## 手順

### 1. `search_kb` で探す

```
search_kb(query: "蛇腹の要件はどこまで充足しているか")
```

- **質問文のまま渡してよい。** 日本語の語分割はツール側で行う
- 識別子（`#398` / `shrink_clamp_bellow_overlap_mm` / `PySide6`）はそのまま渡すと完全一致で強くヒットする
- 絞りたいときだけオプションを使う

| 目的 | 指定 |
| --- | --- |
| 議事録だけ | `document_type: "meeting"` |
| 仕様・要件だけ | `document_type: "specification"` |
| 現行のものだけ | `status: "active"` |
| 特定案件だけ | `path_prefix: "チーム内定例/設計効率化/三桜工業様"` |

### 2. 結果を読む

各結果には次が入っている。

```
path / heading_path / start_line / end_line / snippet / url
status / source / document_type / post_number / indexed_at
index_stale / other_paths / chunk_id
```

**`index_stale: true` のときは行番号を信用しない。** 索引を作ったあとにファイルが変わっている。
その場合は `get_document` で原文を読み直してから引用する。

`other_paths` は同じ記事が別パスにも存在することを示す（バッチ間でカテゴリが重なっているため）。
同じ内容なので、引用は1つのパスで足りる。

### 3. 必要なら原文を確認する

スニペットだけで答えられないとき、前後の文脈が要るときは原文を読む。

```
get_document(path: "...", start_line: 138, end_line: 152, context: 10)
```

行番号付きで返るので、そのまま引用箇所を特定できる。
`get_chunk(chunk_id: 12345)` でチャンク単位に読むこともできる。

### 4. 出典を添えて答える

```markdown
提案ルールの探索条件は `shrink_clamp_bellow_overlap_mm` で制御しています。

> （引用）

出典: [提案ルールと探索条件ダイアログの仕様](docs/チーム内定例/設計効率化/三桜工業様/蛇腹形状の自動設計/検討/機能/提案ルールと探索条件ダイアログの仕様.md#L138-L152)（esa #4952 / 更新 2026-07-14）
```

- ファイルパスと行番号を必ず書く
- esa 記事なら記事番号と更新日も添える（鮮度が判断できる）
- `status: deprecated` / `archived` の文書を引くときは**その旨を明記する**

## 見つからないとき

1. 語を変えて再検索する（正式名称 ↔ 略称、日本語 ↔ 英語）
2. `path_prefix` の絞りを外す
3. `index_status` で索引が最新か確認する

```
index_status()
```

`lastIndexedAt` が古い、または対象文書が最近同期されたなら、索引が追いついていない。
`node tools/build-index.js` で更新できる（数十秒）。

それでも無ければ「ナレッジベースには見つからなかった」と答える。**無い情報を作らない。**

## 現在の制約

- **ベクトル検索は未稼働。** 埋め込みが未生成のため全文検索のみで動く。
  `index_status` の `vectorSearchAvailable: false` で確認できる。
  言い換え（「起動が遅い」→「起動時間の短縮」）に弱いので、語を変えた再検索が有効
- **B32doc（CATIA原本コーパス 約8万件）は索引に含まれていない。** 実務資料のみが対象。
  B32doc は `docs/knowledge/` 配下を直接読むか、`tools/knowledge-curator` の索引を使う

## 他の手段との使い分け

| やりたいこと | 使うもの |
| --- | --- |
| ナレッジベースを検索する | **このスキル（kb-search）** |
| 検索結果を図・アニメにする | `visualizing-kb` スキル（kb-visualize MCP） |
| 文書を取得・更新する | `downloading-kb-docs` スキル |
| esa の記事を読む・書く | `managing-esa-posts` スキル |
| 特定のファイルを直接読む | `Read` ツール（パスが分かっている場合） |

パスが分かっているなら検索を挟まず `Read` で読む方が速い。
