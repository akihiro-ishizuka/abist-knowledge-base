#!/usr/bin/env node
// tests/fixtures/capture/capture-embeddings.mjs
//
// M1 Task 5 (Part 1): 埋め込みゲート素材の採取。
//
// design/system-design.md §11.2 の埋め込み再利用ゲート:
//   「Xenova/multilingual-e5-small と intfloat/multilingual-e5-small の互換性は
//    100件の層化サンプルで再計算し、同一入力のコサイン類似度が全件0.999以上、
//    かつ既存評価クエリのRecall@5低下が0.01以内の場合だけ合格とする。
//    不合格なら全件をPythonで再生成する。」
//
// このスクリプトはゲート判定そのものを行わない（判定は Python 側・M8 で行う）。
// ここでは判定に使う「測定用の入力」を、旧リポジトリの実データから決定的に
// 100件層化抽出して記録するだけである。旧版は q8 量子化 ONNX
// (tools/lib/embeddings.js の createEmbedder: `pipeline(..., { dtype: 'q8' })`)
// なので不合格が既定想定だが、それはこのフィクスチャが「測定して確認する」べき
// 結論であり、採取スクリプト側で仮定・先取りしてはいけない。
//
// 出力: tests/fixtures/embedding/gate-samples.json
//
// 【絶対条件】旧リポジトリ (multi-source-knowledge-base) には一切書き込まない。
//
// 【Task 3/4 からの教訓】better-sqlite3 で data/kb-index.sqlite・
// data/reference-index.sqlite を開くだけで mtime が変化することが実測で
// 判明している(内容・サイズは不変)。そのため、このスクリプトも Task 3/4 と
// 同じ方式を採る: 両 DB を旧リポジトリ外の使い捨てサンドボックスへ「コピー」し、
// サンドボックス内のコピーだけを開く。旧リポジトリの実ファイルには一切触れない
// (better-sqlite3 の `{readonly: true, fileMustExist: true}` オプションで mtime が
// 変化しないという報告もあるが、Task 3/4 で確立された「サンドボックスコピー」
// 方式のほうが実証済みで安全側に倒せるため、一貫してこちらを採用する)。
//
// tools/lib/embeddings.js(embeddingInput/embeddingInputHash/modelConfig)は
// crypto 以外の依存が無い純粋関数群なので、サンドボックスへコピーせず旧リポジトリから
// 直接 import する(capture-kernel.mjs と同じ方式。読み取りのみで副作用が無いことは
// Task 1 で確認済み)。

import { copyFileSync, existsSync, mkdirSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertReadOnly, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する

const ROOT = oldRepoRoot();
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "embedding");

const { embeddingInput, embeddingInputHash, modelConfig } = await import(
  pathToFileURL(join(ROOT, "tools", "lib", "embeddings.js")).href
);

// ===========================================================================
// サンドボックス: data/kb-index.sqlite・data/reference-index.sqlite をコピーする
// (Task 3 の「重大インシデント」注記、Task 4 の§1と同じ設計思想)
// ===========================================================================

function createSandbox(root) {
  const sandbox = mkdtempSync(join(tmpdir(), "kb-embed-sandbox-"));
  mkdirSync(join(sandbox, "data"), { recursive: true });

  // kb-index.sqlite は WAL モードのため、-wal/-shm が存在すればコミット済みだが
  // チェックポイントされていない変更を含みうる。整合性のため三点セットで揃える
  // (存在しなければコピーしない。旧リポジトリ側は一切書き込まないので、
  //  コピー元に無いファイルを新規作成する必要は無い)。
  for (const name of [
    "kb-index.sqlite",
    "kb-index.sqlite-wal",
    "kb-index.sqlite-shm",
    "reference-index.sqlite",
  ]) {
    const src = join(root, "data", name);
    if (!existsSync(src)) continue;
    const dst = join(sandbox, "data", name);
    copyFileSync(src, dst);
    const srcSize = statSync(src).size;
    const dstSize = statSync(dst).size;
    if (srcSize !== dstSize) {
      throw new Error(`サンドボックスへのコピーが不完全です: ${name} (src=${srcSize} dst=${dstSize})`);
    }
  }
  return sandbox;
}

function destroySandbox(sandbox) {
  if (!sandbox) return;
  rmSync(sandbox, { recursive: true, force: true });
}

const sandbox = createSandbox(ROOT);

const { default: Database } = await import(
  pathToFileURL(join(ROOT, "node_modules", "better-sqlite3", "lib", "index.js")).href
);

function openReadonly(path) {
  return new Database(path, { readonly: true, fileMustExist: true });
}

function dbSummary(db) {
  const counts = {};
  for (const table of ["documents", "chunks", "embeddings"]) {
    counts[table] = db.prepare(`SELECT COUNT(*) AS c FROM ${table}`).get().c;
  }
  // embeddings の件数と「chunks に実在する embeddings」の件数が食い違う場合、
  // 孤立した embeddings 行(削除済み chunk を指す再インデックス残骸)がある。
  // 黙って無視せず記録する。
  counts.chunks_with_embeddings = db
    .prepare("SELECT COUNT(*) AS c FROM chunks c JOIN embeddings e ON e.chunk_id = c.id")
    .get().c;
  const meta = {};
  for (const row of db.prepare("SELECT key, value FROM meta ORDER BY key").all()) {
    meta[row.key] = row.value;
  }
  return { counts, meta };
}

const kbIndexPath = join(sandbox, "data", "kb-index.sqlite");
const referenceIndexPath = join(sandbox, "data", "reference-index.sqlite");

const kbIndexDb = openReadonly(kbIndexPath);
const referenceIndexDb = openReadonly(referenceIndexPath);

const dbCounts = {
  "kb-index.sqlite": dbSummary(kbIndexDb),
  "reference-index.sqlite": dbSummary(referenceIndexDb),
};

// ===========================================================================
// 層化抽出: 文字数(短/中/長) × 文字種構成(日本語主体/英数主体/混在)
//
// 「文字数」は c.text の JS 文字列長(UTF-16コードユニット数)をそのまま使う
// (brief: token_estimate でも text length でもよいとされているうち、
//  chunker.js 自身の estimateTokens を経由しない生の text.length を採用。
//  再現性のため定義をここに固定する)。
//
// 「文字種構成」は capture-real-docs.mjs と同じ CJK 判定範囲
// (U+3000-30FF, U+3400-4DBF, U+4E00-9FFF, U+F900-FAFF, U+FF00-FFEF) を使い、
// 空白を除く全コードポイントに対する CJK 比率・ASCII(0x00-0x7F)比率で決める:
//   - CJK比率 >= 0.5                    → "japanese"
//   - CJK比率 < 0.5 かつ ASCII比率 >= 0.5 → "ascii"
//   - どちらも過半数に届かない            → "mixed"
// ===========================================================================

const CJK_RE = /[　-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;

function lengthBucket(len) {
  if (len < 200) return "short";
  if (len <= 600) return "medium";
  return "long";
}

function scriptBucket(text) {
  let cjk = 0;
  let ascii = 0;
  let total = 0;
  for (const ch of text) {
    if (/\s/.test(ch)) continue;
    total++;
    if (CJK_RE.test(ch)) cjk++;
    else if (ch.codePointAt(0) <= 0x7f) ascii++;
  }
  if (total === 0) return "mixed";
  const cjkRatio = cjk / total;
  const asciiRatio = ascii / total;
  if (cjkRatio >= 0.5) return "japanese";
  if (asciiRatio >= 0.5) return "ascii";
  return "mixed";
}

// 9層の並び順(このスクリプト内の唯一の優先順位)。100 = 12 + 11*8。
// 最初の層(short_japanese)だけ12件、残り8層は11件ずつ = 合計100件。
// この非対称は「9では割り切れない100」を決定的に配分するための都合であり、
// 特定の層を過大評価する意図はない(どの層が+1を受け取るかも固定順序で決める)。
const LENGTH_BUCKETS = ["short", "medium", "long"];
const SCRIPT_BUCKETS = ["japanese", "ascii", "mixed"];
const STRATA_ORDER = [];
for (const lb of LENGTH_BUCKETS) {
  for (const sb of SCRIPT_BUCKETS) {
    STRATA_ORDER.push({ length_bucket: lb, script_bucket: sb, name: `${lb}_${sb}` });
  }
}
const BASE_TARGET = Math.floor(100 / STRATA_ORDER.length); // 11
const REMAINDER = 100 % STRATA_ORDER.length; // 1
STRATA_ORDER.forEach((s, i) => {
  s.target = BASE_TARGET + (i < REMAINDER ? 1 : 0);
});

const E5_MODEL = "Xenova/multilingual-e5-small";

const joinedRows = kbIndexDb
  .prepare(
    `SELECT c.id AS chunk_id, c.path, c.chunk_index, c.title, c.heading_path, c.text,
            c.content_hash, c.token_estimate,
            e.vector, e.model, e.dimensions, e.input_hash AS stored_input_hash
       FROM chunks c
       JOIN embeddings e ON e.chunk_id = c.id
      ORDER BY c.id`
  )
  .all();

// 層ごとの候補プール(id昇順を維持したまま1パスで振り分ける)
const pools = new Map(STRATA_ORDER.map((s) => [s.name, []]));
for (const row of joinedRows) {
  const lb = lengthBucket(row.text.length);
  const sb = scriptBucket(row.text);
  pools.get(`${lb}_${sb}`).push(row);
}

const strataReport = [];
const selectedRows = [];
for (const stratum of STRATA_ORDER) {
  const pool = pools.get(stratum.name);
  const selected = pool.slice(0, stratum.target);
  strataReport.push({
    name: stratum.name,
    length_bucket: stratum.length_bucket,
    script_bucket: stratum.script_bucket,
    target_count: stratum.target,
    matching_total: pool.length,
    selected_count: selected.length,
    selected_chunk_ids: selected.map((r) => r.chunk_id),
  });
  selectedRows.push(...selected.map((r) => ({ ...r, stratum: stratum.name })));
}

const totalSelected = strataReport.reduce((sum, s) => sum + s.selected_count, 0);
if (totalSelected !== 100) {
  throw new Error(`層化抽出の合計が100件になりません(実際: ${totalSelected}件)。各層の母集団を確認してください。`);
}

// ===========================================================================
// 各サンプルについて e5 入力・ハッシュ・生ベクトルを記録する
// ===========================================================================

const cases = selectedRows.map((row) => {
  const chunkLike = { title: row.title, heading_path: row.heading_path, text: row.text };
  const input = embeddingInput(chunkLike, row.model);
  const recomputedHash = embeddingInputHash(chunkLike, row.model);

  // 埋め込み保存時に使われた input_hash と、いま同じ chunk 行から再計算した
  // input_hash が一致することを確認する(食い違えば chunk が embeddings 保存後に
  // 変更されたことを意味し、ゲート判定の前提が崩れるため、値を合わせず例外で止める)。
  if (recomputedHash !== row.stored_input_hash) {
    throw new Error(
      `chunk_id=${row.chunk_id}: 再計算した input_hash が保存済み embeddings.input_hash と食い違います。\n` +
        `保存済み: ${row.stored_input_hash}\n再計算: ${recomputedHash}\n` +
        "(chunk が embeddings 保存後に更新された可能性があります)"
    );
  }

  return {
    id: `${row.stratum}_${String(row.chunk_id).padStart(6, "0")}`,
    stratum: row.stratum,
    chunk_id: row.chunk_id,
    path: row.path,
    chunk_index: row.chunk_index,
    content_hash: row.content_hash,
    token_estimate: row.token_estimate,
    text_length: row.text.length,
    model: row.model,
    dimensions: row.dimensions,
    embeddingInput_b64: Buffer.from(input, "utf8").toString("base64"),
    input_hash: recomputedHash,
    // 保存済みベクトル BLOB を Float32 LE のまま base64 化する(数値配列に変換すると
    // 浮動小数の精度が落ちるため、生バイト列のまま保持する。brief必須要件)。
    vector_b64: Buffer.from(row.vector.buffer, row.vector.byteOffset, row.vector.byteLength).toString("base64"),
  };
});

writeDeterministicJson(join(OUT_DIR, "gate-samples.json"), {
  schema: 1,
  source: "data/kb-index.sqlite chunks JOIN embeddings (sandboxed copy; see this file's header comment)",
  design_reference:
    "design/system-design.md §11.2 (埋め込み再利用ゲート: 100件層化サンプルでコサイン類似度>=0.999 かつ Recall@5低下<=0.01)",
  gate_note:
    "このフィクスチャはゲート判定の入力(旧ベクトル・再現可能な e5 入力・保存済みinput_hash)を" +
    "記録するのみで、判定結果(合格/不合格)はここでは出さない。旧版は q8量子化ONNXの" +
    "Xenova/multilingual-e5-smallで、Python側はintfloat/multilingual-e5-small(非量子化)を" +
    "比較対象とする想定のため不合格が既定シナリオだが、それはM8で実測して確認すべき結論であり、" +
    "このスクリプトは測定に先回りして仮定しない。",
  embedding_model: E5_MODEL,
  model_config: modelConfig(E5_MODEL),
  classification: {
    length_bucket_note: "chunks.text の JS 文字列長(UTF-16コードユニット数)。short<200, 200<=medium<=600, long>600",
    script_bucket_note:
      "空白を除く全コードポイントに対するCJK比率・ASCII比率(定義はスクリプト本体コメント参照)。" +
      "cjk_ratio>=0.5→japanese、次にascii_ratio>=0.5→ascii、どちらでもなければmixed。",
  },
  db_counts: dbCounts,
  strata: strataReport,
  cases,
});

kbIndexDb.close();
referenceIndexDb.close();
destroySandbox(sandbox);

assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

console.log(
  `capture-embeddings.mjs: OK (tests/fixtures/embedding/gate-samples.json, ${cases.length} cases, ${strataReport.length} strata)`
);
