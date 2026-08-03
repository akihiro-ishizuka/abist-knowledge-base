#!/usr/bin/env node
// tests/fixtures/capture/capture-eval.mjs
//
// M1 Task 4: 検索評価ベースライン採取。
//
// eval/queries.jsonl(22クエリ)を旧リポジトリからバイト保持でコピーし、
// tools/eval-search.js の関数(loadQueries/recallAtK/reciprocalRank/ndcgAtK)と
// tools/lib/search-engine.js の関数(search/keywordSearch)を旧リポジトリの実装
// そのままで実行して、hybrid(統合検索)/bm25_raw(FTS5のみ)の2方式で
// Recall@5・MRR・nDCG@10 のベースラインを採取する。
//
// M4(Python版検索の移植)の受け入れ基準「Recall@5 が本ベースラインと0.01以内、
// 引用行番号の一致率95%以上」はこの fixture が根拠になる。
//
// 【絶対条件】旧リポジトリ (multi-source-knowledge-base) には一切書き込まない。
//
// 【Task 3 からの教訓】better-sqlite3 で実 SQLite ファイル(data/kb-index.sqlite)を
// 開くだけで mtime が変化することが実測で判明している(内容は不変)。そのため
// 索引 DB は旧リポジトリ外の使い捨てサンドボックスへコピーし、そこで開く。
// tools/lib/*.js は依存関係ごとコピーし、docs/・node_modules/ は読み取り専用の
// ディレクトリジャンクションとして参照する(Task 3 で安全性を実証済みの方式)。
//
// 【埋め込みモデルについて】search_kb と同じ本番経路(ベクトル検索込みの
// hybrid)を再現するため、既定の埋め込みモデル(Xenova/multilingual-e5-small)で
// クエリを埋め込む。モデルは旧リポジトリの node_modules 配下に既にキャッシュ
// 済み(config.json・tokenizer.json・tokenizer_config.json・onnx 2種)であることを
// 事前に確認済みで、@huggingface/transformers の FileCache はキャッシュヒット時に
// 一切書き込みを行わない(ソースを読んで確認済み: match() は fs.exists 相当の
// 判定のみ、put() はキャッシュミス後のリモート取得成功時にしか呼ばれない)。
// それでも「万一キャッシュが欠けていたらネットワーク取得で書き込みが起きる」
// 可能性を断つため、サンドボックス内の driver.mjs で
// `env.allowRemoteModels = false` を明示的に設定し、欠けていれば
// ダウンロードではなく例外で即座に止まるようにしている。
//
// 出力:
//   tests/fixtures/eval/queries.jsonl (旧リポジトリからのバイト保持コピー)
//   tests/fixtures/eval/baseline.json

import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertReadOnly, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する

const ROOT = oldRepoRoot();
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "eval");

const METHODS = ["bm25_raw", "hybrid"];

// ===========================================================================
// 0. 追加の安全網: HF モデルキャッシュ(node_modules 配下)の変化を検出する
//
// assertReadOnly() は docs/・data/・batch-config.js しか見ないため、
// node_modules/@huggingface/transformers/.cache 配下の変化は検出できない。
// このスクリプトはそこへは書き込まない設計(上記コメント参照)だが、設計が
// 破れていないことを実測でも確認するため、専用のフィンガープリントを取る。
// ===========================================================================

const HF_CACHE_DIR = join(ROOT, "node_modules", "@huggingface", "transformers", ".cache");

function fingerprintDir(dir) {
  const results = new Map();
  function walk(d) {
    let entries;
    try {
      entries = readdirSync(d, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = join(d, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile()) {
        const stat = statSync(full);
        results.set(full.slice(dir.length).replace(/\\/g, "/"), `${stat.size}:${Math.round(stat.mtimeMs)}`);
      }
    }
  }
  walk(dir);
  return results;
}

function diffFingerprint(before, after) {
  const changed = [];
  for (const [key, value] of after) {
    if (!before.has(key) || before.get(key) !== value) changed.push(key);
  }
  for (const key of before.keys()) {
    if (!after.has(key)) changed.push(key);
  }
  return changed;
}

const hfCacheBefore = fingerprintDir(HF_CACHE_DIR);

// ===========================================================================
// 1. eval/queries.jsonl をバイト保持でコピーし、SHA-256 を記録する
// ===========================================================================

const srcQueriesPath = join(ROOT, "eval", "queries.jsonl");
const dstQueriesPath = join(OUT_DIR, "queries.jsonl");

const rawQueriesBytes = readFileSync(srcQueriesPath); // Buffer(生バイト、utf8変換をしない)
mkdirSync(OUT_DIR, { recursive: true });
copyFileSync(srcQueriesPath, dstQueriesPath);

const copiedBytes = readFileSync(dstQueriesPath);
if (Buffer.compare(rawQueriesBytes, copiedBytes) !== 0) {
  throw new Error("queries.jsonl のコピーがバイト一致しません(コピー処理自体の不具合)");
}
const queriesSha256 = crypto.createHash("sha256").update(rawQueriesBytes).digest("hex");

// ===========================================================================
// 2. サンドボックス作成(旧リポジトリ外。実 SQLite ファイルへは一切触れない)
// ===========================================================================

function createSandbox(root) {
  const sandbox = mkdtempSync(join(tmpdir(), "kb-eval-sandbox-"));
  mkdirSync(join(sandbox, "tools", "lib"), { recursive: true });
  cpSync(join(root, "tools", "lib"), join(sandbox, "tools", "lib"), { recursive: true });

  // tools/eval-search.js をそのままコピーし、末尾に foldToDocuments の
  // export だけを追記する(foldToDocuments は非公開関数だが、brief が要求する
  // 「文書単位に畳んだリスト」を旧コードの実装そのもので作るために必要)。
  // 追記前の内容が原本と完全一致することを自己検証してから追記する。
  const evalSearchSrcPath = join(root, "tools", "eval-search.js");
  const evalSearchDstPath = join(sandbox, "tools", "eval-search.js");
  const originalEvalSearch = readFileSync(evalSearchSrcPath, "utf8");
  copyFileSync(evalSearchSrcPath, evalSearchDstPath);
  const copiedEvalSearch = readFileSync(evalSearchDstPath, "utf8");
  if (originalEvalSearch !== copiedEvalSearch) {
    throw new Error("tools/eval-search.js のコピーが原本と一致しません");
  }
  writeFileSync(evalSearchDstPath, copiedEvalSearch + "\nexport { foldToDocuments };\n");

  writeFileSync(join(sandbox, "package.json"), JSON.stringify({ type: "module" }) + "\n");

  // driver.mjs: 埋め込みモデルのリモート取得を明示的に禁止してから
  // 必要なモジュールを再エクスポートする(このファイル冒頭のコメント参照)。
  writeFileSync(
    join(sandbox, "tools", "driver.mjs"),
    [
      "import { env } from '@huggingface/transformers';",
      "// キャッシュ済みモデルが万一欠けていた場合にダウンロード(=書き込み)へ",
      "// フォールバックさせず、例外で止める(旧リポジトリ絶対不変の制約を守るため)。",
      "env.allowRemoteModels = false;",
      "",
      "export * as indexerMod from './lib/indexer.js';",
      "export * as searchEngineMod from './lib/search-engine.js';",
      "export * as embeddingsMod from './lib/embeddings.js';",
      "export * as evalSearchMod from './eval-search.js';",
      "",
    ].join("\n")
  );

  mkdirSync(join(sandbox, "data"), { recursive: true });
  const srcDb = join(root, "data", "kb-index.sqlite");
  const dstDb = join(sandbox, "data", "kb-index.sqlite");
  copyFileSync(srcDb, dstDb);
  const srcSize = statSync(srcDb).size;
  const dstSize = statSync(dstDb).size;
  if (srcSize !== dstSize) {
    throw new Error(`索引DBのサンドボックスへのコピーが不完全です(src=${srcSize} dst=${dstSize})`);
  }

  // 読み取り専用参照(検索は docs/・node_modules/ のどちらにも書き込まないことを
  // Task 3 でソース読解・probe 実行済み。このタスクでも同じ経路(search-engine.js)
  // しか使わないため同じ保証が成り立つ)。
  execFileSync("cmd", ["/c", "mklink", "/J", join(sandbox, "node_modules"), join(root, "node_modules")], {
    windowsHide: true,
  });
  execFileSync("cmd", ["/c", "mklink", "/J", join(sandbox, "docs"), join(root, "docs")], { windowsHide: true });

  return sandbox;
}

function destroySandbox(sandbox) {
  if (!sandbox) return;
  // rmSync(recursive) はジャンクションを isSymbolicLink() として検出し、
  // リンク先(旧リポジトリの docs/・node_modules/)へは再帰しない
  // (Task 3 で隔離環境にて事前実証済み)。
  rmSync(sandbox, { recursive: true, force: true });
}

const sandbox = createSandbox(ROOT);

// ===========================================================================
// 3. 採取本体
// ===========================================================================

function round6(n) {
  return Number(Number(n).toFixed(6));
}

/** 統合検索(hybrid)の結果1件を fixture 用の形へ落とす */
function trimHybridRow(row) {
  return {
    path: row.path,
    post_number: row.post_number ?? null,
    chunk_id: row.chunk_id,
    score: round6(row.score),
    start_line: row.start_line,
    end_line: row.end_line,
    matched_by: [...row.matched_by].sort(),
  };
}

/** BM25のみ(bm25_raw)の結果1件を fixture 用の形へ落とす */
function trimBm25Row(row) {
  return {
    path: row.path,
    post_number: row.post_number ?? null,
    chunk_id: row.chunk_id,
    score: round6(row.bm25),
    start_line: row.start_line,
    end_line: row.end_line,
    matched_by: ["keyword"],
  };
}

async function main() {
  const driver = await import(pathToFileURL(join(sandbox, "tools", "driver.mjs")).href);
  const { indexerMod, searchEngineMod, embeddingsMod, evalSearchMod } = driver;

  const sandboxIndexPath = join(sandbox, "data", "kb-index.sqlite");
  const sandboxDocsDir = join(sandbox, "docs");

  const db = indexerMod.openIndex(sandboxIndexPath);
  const status = indexerMod.indexStatus(db);

  const queries = evalSearchMod.loadQueries(dstQueriesPath);
  if (queries.length !== 22) {
    throw new Error(`queries.jsonl の件数が想定と異なります: 期待=22 実際=${queries.length}`);
  }

  const embeddingModel = status.embeddingModel || embeddingsMod.DEFAULT_EMBEDDING_MODEL;
  const embedStarted = process.hrtime.bigint();
  const embed = await embeddingsMod.createEmbedder(embeddingModel);
  const embedLoadMs = Number(process.hrtime.bigint() - embedStarted) / 1e6;

  const perQuery = [];
  const totals = {};
  for (const method of METHODS) totals[method] = { recall5: 0, mrr: 0, ndcg10: 0, zero: 0 };
  const elapsedByMethod = { bm25_raw: 0, hybrid: 0 };
  let queryEmbedMsTotal = 0;

  for (const q of queries) {
    const embedStart = process.hrtime.bigint();
    const [rawVector] = await embed([embeddingsMod.queryInput(q.query, embeddingModel)]);
    const queryVector = embeddingsMod.normalize(rawVector);
    queryEmbedMsTotal += Number(process.hrtime.bigint() - embedStart) / 1e6;

    const methodResults = {};

    // --- bm25_raw ---
    {
      const started = process.hrtime.bigint();
      const raw = searchEngineMod.keywordSearch(db, q.query, { candidateLimit: 60 });
      const folded = evalSearchMod.foldToDocuments(raw, 10);
      elapsedByMethod.bm25_raw += Number(process.hrtime.bigint() - started) / 1e6;

      const recall5 = evalSearchMod.recallAtK(folded, q.relevant, 5);
      const rr = evalSearchMod.reciprocalRank(folded, q.relevant);
      const ndcg10 = evalSearchMod.ndcgAtK(folded, q.relevant, 10);

      totals.bm25_raw.recall5 += recall5;
      totals.bm25_raw.mrr += rr;
      totals.bm25_raw.ndcg10 += ndcg10;
      if (folded.length === 0) totals.bm25_raw.zero++;

      methodResults.bm25_raw = {
        raw: raw.map(trimBm25Row),
        folded: folded.map(trimBm25Row),
        recall5,
        rr,
        ndcg10,
      };
    }

    // --- hybrid ---
    {
      const started = process.hrtime.bigint();
      const raw = searchEngineMod.search({
        db,
        query: q.query,
        queryVector,
        docsDir: sandboxDocsDir,
        options: { limit: 20 },
      }).results;
      const folded = evalSearchMod.foldToDocuments(raw, 10);
      elapsedByMethod.hybrid += Number(process.hrtime.bigint() - started) / 1e6;

      const recall5 = evalSearchMod.recallAtK(folded, q.relevant, 5);
      const rr = evalSearchMod.reciprocalRank(folded, q.relevant);
      const ndcg10 = evalSearchMod.ndcgAtK(folded, q.relevant, 10);

      totals.hybrid.recall5 += recall5;
      totals.hybrid.mrr += rr;
      totals.hybrid.ndcg10 += ndcg10;
      if (folded.length === 0) totals.hybrid.zero++;

      methodResults.hybrid = {
        raw: raw.map(trimHybridRow),
        folded: folded.map(trimHybridRow),
        recall5,
        rr,
        ndcg10,
      };
    }

    perQuery.push({
      id: q.id,
      query: q.query,
      type: q.type,
      intent: q.intent,
      relevant: q.relevant,
      results: methodResults,
    });
  }

  const n = queries.length;
  const macro = {};
  for (const method of METHODS) {
    macro[method] = {
      recall5: totals[method].recall5 / n,
      mrr: totals[method].mrr / n,
      ndcg10: totals[method].ndcg10 / n,
      zero_hit_queries: totals[method].zero,
    };
  }

  writeDeterministicJson(join(OUT_DIR, "baseline.json"), {
    schema: 1,
    source:
      "tools/eval-search.js (loadQueries/recallAtK/reciprocalRank/ndcgAtK/foldToDocuments) " +
      "+ tools/lib/search-engine.js (search/keywordSearch) " +
      "+ tools/lib/indexer.js (openIndex/indexStatus), " +
      "sandboxed copy of data/kb-index.sqlite(旧リポジトリの実ファイルは一切開いていない)",
    queries_file: "tests/fixtures/eval/queries.jsonl",
    queries_sha256: queriesSha256,
    query_count: n,
    embedding_model: embeddingModel,
    index_status: {
      documents: status.documents,
      chunks: status.chunks,
      embeddedChunks: status.embeddedChunks,
      tokenizers: status.tokenizers,
      embeddingModel: status.embeddingModel,
    },
    methods: METHODS,
    per_query: perQuery,
    macro,
  });

  db.close();

  console.error(
    `[capture-eval] embed model load: ${embedLoadMs.toFixed(1)}ms / query embed total: ${queryEmbedMsTotal.toFixed(1)}ms / ` +
      `bm25_raw search total: ${elapsedByMethod.bm25_raw.toFixed(1)}ms / hybrid search total: ${elapsedByMethod.hybrid.toFixed(1)}ms`
  );
  for (const method of METHODS) {
    console.error(
      `[capture-eval] macro ${method}: recall5=${macro[method].recall5.toFixed(4)} mrr=${macro[method].mrr.toFixed(4)} ` +
        `ndcg10=${macro[method].ndcg10.toFixed(4)} zero_hit_queries=${macro[method].zero_hit_queries}`
    );
  }
}

await main();

destroySandbox(sandbox);

// ===========================================================================
// 4. 追加の安全網の検証 + 通常の assertReadOnly()
// ===========================================================================

const hfCacheAfter = fingerprintDir(HF_CACHE_DIR);
const hfCacheChanged = diffFingerprint(hfCacheBefore, hfCacheAfter);
if (hfCacheChanged.length > 0) {
  throw new Error(
    `HuggingFace モデルキャッシュ(node_modules 配下)が変化しました(書き込みが発生した可能性): ` +
      hfCacheChanged.slice(0, 20).join(", ")
  );
}
console.error(`hf cache fingerprint check: OK (${hfCacheBefore.size} files, 変化なし)`);

if (existsSync(sandbox)) {
  throw new Error(`サンドボックスの削除に失敗しました: ${sandbox}`);
}

assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

console.log("capture-eval.mjs: OK (tests/fixtures/eval/{queries.jsonl,baseline.json} を書き出しました)");
