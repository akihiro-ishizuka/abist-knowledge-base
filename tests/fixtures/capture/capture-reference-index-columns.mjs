#!/usr/bin/env node
// tests/fixtures/capture/capture-reference-index-columns.mjs
//
// M4 Task 1b: `data/reference-index.sqlite` の `documents` テーブルが reference
// コーパス(B32doc)の各行に実際にどの列値を書き込んでいたかを実測する。
//
// 経緯: M4 Task1 の `select_reference_targets`(`src/abist_kb/infrastructure/search/corpus.py`)
// は `source = "b32doc"` を決め打ちしていたが、brief にこの値の指定が無く、M1 でも
// fixture 化されていなかったため実装者の推測だった。PROVENANCE.md §4 は旧
// `sync-state.sqlite` の `source` 列が `esa`/`git`/`manual` の3値のみと記録しており、
// `reference-index.sqlite` 側の実際の値は未確認のまま残っていた。本スクリプトは
// その未確認部分を実測で埋める。
//
// 【絶対条件】旧リポジトリ (multi-source-knowledge-base) には一切書き込まない。
// `data/reference-index.sqlite` を better-sqlite3 で素朴に開くだけで mtime が変化する
// ことが Task 3 (M1) で実測判明しているため(capture/README.md「読み取り専用SQLiteの
// 教訓」参照)、旧リポジトリ外の使い捨てサンドボックスへコピーし、コピーだけを開く。
// 旧リポジトリの実 DB ファイルは一度も直接開かない。
//
// タイトルの取得元検証は、docs/knowledge/B32doc の実ファイルを読み(読み取りのみ)、
// tools/lib/b32doc-filter.js の extractSummaryKeys() を実行して DB の title 列と
// 突き合わせる(ファイル名由来ではないことを実測で示す)。
//
// 出力: tests/fixtures/reference-index/columns.json

import { execFileSync } from "node:child_process";
import { copyFileSync, mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertReadOnly, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する

const ROOT = oldRepoRoot();
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "reference-index");

// ===========================================================================
// 1. サンドボックスコピー(旧リポジトリ外。実 SQLite ファイルへは一切触れない)
// ===========================================================================

const sandbox = mkdtempSync(join(tmpdir(), "kb-reference-index-capture-"));
const srcDb = join(ROOT, "data", "reference-index.sqlite");
const dstDb = join(sandbox, "reference-index.sqlite");
copyFileSync(srcDb, dstDb);
const srcSize = statSync(srcDb).size;
const dstSize = statSync(dstDb).size;
if (srcSize !== dstSize) {
  throw new Error(`reference-index.sqlite のサンドボックスへのコピーが不完全です(src=${srcSize} dst=${dstSize})`);
}

// better-sqlite3 は旧リポジトリの node_modules から解決する(ネイティブモジュールの
// ため、サンドボックスに node_modules を作らず require だけ旧リポジトリ側から行う。
// DB ファイル自体はサンドボックスコピーを開くので旧リポジトリへの書き込みは発生しない)。
const require = createRequire(pathToFileURL(join(ROOT, "package.json")).href);
const Database = require("better-sqlite3");

const db = new Database(dstDb, { readonly: true, fileMustExist: true });

// ===========================================================================
// 2. documents テーブルの列ごとの分布を実測する
// ===========================================================================

const totalCount = db.prepare("SELECT COUNT(*) AS n FROM documents").get().n;

const COLUMNS = ["source", "document_type", "status", "post_number", "url", "category"];
const distinctValues = {};
for (const col of COLUMNS) {
  distinctValues[col] = db
    .prepare(`SELECT ${col} AS value, COUNT(*) AS count FROM documents GROUP BY ${col} ORDER BY count DESC, value ASC`)
    .all();
}

const embeddingsRowCount = db.prepare("SELECT COUNT(*) AS n FROM embeddings").get().n;
const metaRows = db.prepare("SELECT key, value FROM meta ORDER BY key ASC").all();

// ===========================================================================
// 3. title の取得元検証: docs/knowledge/B32doc の実ファイルを読み、
//    tools/lib/b32doc-filter.js の extractSummaryKeys() を実行して DB の title と
//    突き合わせる。path-sorted で先頭5件を決定的に選ぶ(実データそのもの)。
// ===========================================================================

const { extractSummaryKeys } = await import(pathToFileURL(join(ROOT, "tools/lib/b32doc-filter.js")).href);

const titleSampleRows = db
  .prepare("SELECT path, title FROM documents ORDER BY path ASC LIMIT 5")
  .all();

const titleVerification = titleSampleRows.map((row) => {
  const fullPath = join(ROOT, "docs", ...row.path.split("/"));
  const raw = readFileSync(fullPath, "utf8");
  // frontmatter を素朴に取り除く(先頭の --- ... --- ブロック)だけで本文を得る。
  // extractSummaryKeys は本文(frontmatter 無し)を引数に取る。
  const body = raw.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "");
  const extracted = extractSummaryKeys(body);
  const filenameStem = row.path.split("/").pop();
  return {
    path: row.path,
    db_title: row.title,
    extract_summary_keys_title: extracted.title,
    matches_extract_summary_keys: row.title === extracted.title,
    matches_filename: row.title === filenameStem,
  };
});

db.close();

// ===========================================================================
// 4. サンドボックス破棄 + 書き出し
// ===========================================================================

rmSync(sandbox, { recursive: true, force: true });

writeDeterministicJson(join(OUT_DIR, "columns.json"), {
  schema: 1,
  source: "data/reference-index.sqlite (documents/embeddings/meta テーブル、サンドボックスコピー経由)",
  note:
    "M4 Task1 の select_reference_targets が推測していた reference コーパスの列値" +
    "(source='b32doc')を実測で置き換えるための fixture。実測値は source='manual' " +
    "(sync-state.sqlite と同じ3値のうちの1つ)であり、b32doc という値はどの列にも" +
    "存在しない。詳細は tests/fixtures/PROVENANCE.md 参照。",
  total_documents: totalCount,
  distinct_values: distinctValues,
  embeddings_row_count: embeddingsRowCount,
  meta: metaRows,
  title_source_verification: titleVerification,
});

assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

console.log(
  `capture-reference-index-columns.mjs: OK (tests/fixtures/reference-index/columns.json, ` +
    `${totalCount} documents, embeddings=${embeddingsRowCount})`
);
