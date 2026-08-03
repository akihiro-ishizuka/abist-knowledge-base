#!/usr/bin/env node
// tests/fixtures/capture/capture-real-docs.mjs
//
// M1 Task 2: 実データ層化サンプル採取。
//
// Task 1 (capture-kernel.mjs) は手で転記した境界ケースを記録した。手書きの
// fixture は境界ケースには強いが、実データの分布は別物である。このスクリプトは
// 旧リポジトリの `data/sync-state.sqlite`（read-only）から実文書を層化抽出し、
// Task 1 と同じカーネル出力（parseFrontmatter / hashBody / chunker /
// rangeHash / e5 embedding input）を実行して記録する。
//
// 出力: tests/fixtures/real-docs/samples.json
//
// 選定は完全に決定的（パスの辞書順・乱数不使用）。層ごとの実採取数・除外件数は
// すべて manifest（layers[] / exclusions[]）に記録し、黙って落とさない。

import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertReadOnly, b64, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する

const ROOT = oldRepoRoot();
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "real-docs");

const { parseFrontmatter, hashBody } = await import(pathToFileURL(join(ROOT, "tools/lib/frontmatter.js")).href);
const { chunkMarkdown, DEFAULT_CHUNK_OPTIONS } = await import(
  pathToFileURL(join(ROOT, "tools/lib/chunker.js")).href
);
const { splitDocLines, rangeHash } = await import(pathToFileURL(join(ROOT, "tools/lib/line-range.js")).href);
const { embeddingInput, embeddingInputHash } = await import(
  pathToFileURL(join(ROOT, "tools/lib/embeddings.js")).href
);

const E5_MODEL = "Xenova/multilingual-e5-small";
const MAX_BYTES = 64 * 1024; // brief: 64KBを超えるものは除外する

// ===========================================================================
// 秘密情報スキャン（M0 の src/abist_kb/domain/redaction.py::mask_secrets と
// 同じパターン構造を JS 側に持つ簡易ミラー）。
//
// ここでは置換ではなく「1件でも当たるか」だけを判定する（当たれば
// サンプル全体を除外するため、置換テンプレートは不要）。
// 採取後、Python 側テスト (test_real_docs_fixture.py) で本物の mask_secrets を
// samples.json の全 *_b64 コンテンツに適用し、無変化であることを確認する
// （このミラーが Python 実装と食い違っていないかの検証）。
// ===========================================================================
const SECRET_NAME_SUFFIX = "(?:TOKEN|KEY|SECRET|PASSWORD)";
const NAME_PREFIX = "[A-Za-z][A-Za-z0-9_-]{0,64}";
const SECRET_PATTERNS = [
  { name: "key_value", re: new RegExp(`${NAME_PREFIX}[-_]${SECRET_NAME_SUFFIX}\\s*[:=]\\s*\\S+`, "i") },
  { name: "authorization", re: /Authorization\s*:\s*\S+\s+\S+/i },
  { name: "cookie", re: /Cookie\s*:\s*[^\r\n]+/i },
  { name: "url_credential", re: /[A-Za-z][A-Za-z0-9+.-]{0,64}:\/\/[^\s:/@]+:[^@\s/]+@/ },
  { name: "json_secret", re: new RegExp(`"${NAME_PREFIX}[-_]${SECRET_NAME_SUFFIX}"\\s*:\\s*"[^"]*"`, "i") },
];

function detectSecretPattern(text) {
  for (const { name, re } of SECRET_PATTERNS) {
    if (re.test(text)) return name;
  }
  return null;
}

// ===========================================================================
// 1. sync-state.sqlite から documents を read-only で取得する
// ===========================================================================
const dbPath = join(ROOT, "data", "sync-state.sqlite").replace(/\\/g, "/");
const db = new DatabaseSync(`file:${dbPath}?mode=ro&immutable=1`, { readOnly: true });
const rows = db.prepare("SELECT path, source, document_type FROM documents").all();
db.close();

// 決定的な辞書順（コードポイント比較）に固定する。SQLite の既定 BINARY 照合と
// 一致する想定だが、比較主体をこのスクリプト側に置くために明示的に再ソートする。
rows.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));

// chunker.js の estimateTokens と同じ CJK 判定範囲を再利用する
// （「日本語パス」の定義を独自に作らず、旧システム自身の CJK 定義に合わせる）。
const CJK_RE = /[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;

const docs = rows.map((row) => {
  const fullPath = join(ROOT, "docs", row.path);
  const buf = readFileSync(fullPath);
  const hasBom = buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf;
  const content = buf.toString("utf8");
  const fm = parseFrontmatter(content);
  return {
    path: row.path,
    source: row.source,
    document_type: row.document_type,
    byteSize: buf.length,
    hasBom,
    hasCrlf: content.includes("\r\n"),
    hasCjkPath: CJK_RE.test(row.path),
    hasFrontmatter: fm.hasFrontmatter,
    content,
  };
});

// ===========================================================================
// 2. 層化抽出（決定的・重複除去・64KB除外・秘密情報除外）
// ===========================================================================
const LAYERS = [
  { name: "esa", target: 10, predicate: (d) => d.source === "esa" },
  { name: "web", target: 10, predicate: (d) => d.source === "web" },
  { name: "git", target: 10, predicate: (d) => d.source === "git" },
  { name: "reference", target: 10, predicate: (d) => d.document_type === "reference" },
  { name: "japanese_path", target: 5, predicate: (d) => d.hasCjkPath },
  { name: "long_path", target: 3, rankByLength: true },
  { name: "bom_prefixed", target: 5, predicate: (d) => d.hasBom },
  { name: "crlf_body", target: 5, predicate: (d) => d.hasCrlf },
  { name: "no_frontmatter", target: 5, predicate: (d) => !d.hasFrontmatter },
];

// 層をまたいで同じ path が重複選択されないようにする（brief: 重複は除く）。
// 上の LAYERS の並び順を優先度とする（先に処理した層が path を「先取り」する）。
const claimed = new Set();
// path ごとに1件だけ記録する（複数層の候補として同じファイルに複数回当たっても
// 除外理由は1つにまとめる。「除外件数」を水増ししないため）。
const exclusionsByPath = new Map();

function pathLengthComparator(a, b) {
  if (b.path.length !== a.path.length) return b.path.length - a.path.length;
  return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
}

const layerReports = [];

for (const layer of LAYERS) {
  let candidatesOrdered;
  let matchingTotal;
  if (layer.rankByLength) {
    // 「長いパス」は真偽の条件ではなくパス長のランキングなので、対象母集団は
    // documents 全件（3523件）。matching_total はランキング対象母集団のサイズ。
    matchingTotal = docs.length;
    candidatesOrdered = [...docs].sort(pathLengthComparator);
  } else {
    // Array#sort は安定ソート（ES2019+）なので、docs が既に辞書順である以上
    // filter 後も辞書順が保たれる。
    candidatesOrdered = docs.filter(layer.predicate);
    matchingTotal = candidatesOrdered.length;
  }

  const selectedPaths = [];
  for (const d of candidatesOrdered) {
    if (selectedPaths.length >= layer.target) break;
    if (claimed.has(d.path)) continue;

    if (d.byteSize > MAX_BYTES) {
      if (!exclusionsByPath.has(d.path)) {
        exclusionsByPath.set(d.path, {
          path: d.path,
          layer: layer.name,
          reason: "size_exceeds_64kb",
          byte_size: d.byteSize,
        });
      }
      continue;
    }

    const secretHit = detectSecretPattern(d.content);
    if (secretHit) {
      if (!exclusionsByPath.has(d.path)) {
        exclusionsByPath.set(d.path, {
          path: d.path,
          layer: layer.name,
          reason: `secret_pattern:${secretHit}`,
        });
      }
      continue;
    }

    selectedPaths.push(d.path);
    claimed.add(d.path);
  }

  layerReports.push({
    name: layer.name,
    target_count: layer.target,
    matching_total: matchingTotal,
    selected_count: selectedPaths.length,
    selected_paths: selectedPaths,
  });
}

const exclusions = [...exclusionsByPath.values()].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));

// ===========================================================================
// 3. 選ばれた各サンプルについてカーネル出力を記録する
// ===========================================================================
const byPath = new Map(docs.map((d) => [d.path, d]));
const cases = [];

for (const layer of layerReports) {
  let i = 0;
  for (const path of layer.selected_paths) {
    const d = byPath.get(path);
    const content = d.content;

    const fm = parseFrontmatter(content);
    const chunks = chunkMarkdown(content, DEFAULT_CHUNK_OPTIONS);
    const lines = splitDocLines(content);
    const totalLines = lines.length;

    const frontmatter = {
      hasFrontmatter: fm.hasFrontmatter,
      bomPresent: fm.bom.length > 0,
      bom_b64: b64(fm.bom),
      eol: fm.eol,
      data: fm.data,
      keys: fm.keys,
      blockKeys: [...fm.blockKeys].sort(),
      body_b64: b64(fm.body),
      raw_b64: b64(fm.raw),
      hashBody: hashBody(content),
    };

    const chunker = {
      chunks: chunks.map((c) => ({
        index: c.index,
        heading_path: c.heading_path,
        text_b64: b64(c.text),
        start_line: c.start_line,
        end_line: c.end_line,
        token_estimate: c.token_estimate,
        content_hash: c.content_hash,
        split_by_size: c.split_by_size ?? false,
      })),
      chunkCount: chunks.length,
    };

    const lineRange = {
      totalLines,
      firstLine: totalLines >= 1 ? rangeHash(content, 1, 1) : null,
      lastLine: totalLines >= 1 ? rangeHash(content, totalLines, totalLines) : null,
      fullRange: totalLines >= 1 ? rangeHash(content, 1, totalLines) : null,
      firstChunkRange: chunks.length > 0 ? rangeHash(content, chunks[0].start_line, chunks[0].end_line) : null,
    };

    const embedding =
      chunks.length > 0
        ? {
            model: E5_MODEL,
            embeddingInput_b64: b64(embeddingInput(chunks[0], E5_MODEL)),
            embeddingInputHash: embeddingInputHash(chunks[0], E5_MODEL),
            inputLength: embeddingInput(chunks[0], E5_MODEL).length,
          }
        : null;

    cases.push({
      id: `${layer.name}_${String(i).padStart(2, "0")}`,
      layer: layer.name,
      path,
      source: d.source,
      document_type: d.document_type,
      byte_size: d.byteSize,
      expected: { frontmatter, chunker, line_range: lineRange, embedding },
    });
    i++;
  }
}

writeDeterministicJson(join(OUT_DIR, "samples.json"), {
  schema: 1,
  source: "data/sync-state.sqlite documents (real docs stratified sample; files read from docs/)",
  layers: layerReports,
  exclusions,
  cases,
});

assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

console.log(
  `capture-real-docs.mjs: OK (tests/fixtures/real-docs/samples.json, ${cases.length} cases, ${exclusions.length} exclusions)`
);
