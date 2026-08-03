#!/usr/bin/env node
// tests/fixtures/capture/capture-kernel.mjs
//
// M1 Task 1: 共通基盤とカーネルゴールデン採取。
//
// 旧リポジトリ (multi-source-knowledge-base) の ESM を直接 import して実行し、
// その戻り値をそのままゴールデン値として保存する。期待値はここで計算・推測しない。
//
// 出力: tests/fixtures/kernel/{frontmatter,chunker,line-range,embeddings,sync-planner,metadata-schema}.json

import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertReadOnly, b64, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する

const ROOT = oldRepoRoot();
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "kernel");

async function loadOld(relPath) {
  return import(pathToFileURL(join(ROOT, relPath)).href);
}

/**
 * 転記した値が旧テストの assert.equal / assert.deepEqual と一致することを確認する。
 * 食い違えば転記ミスか仕様の見落としなので、値を合わせにいかず例外で止める。
 */
function assertMatchesOldTest(actual, expected, label, citation) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    throw new Error(
      `${label} の実行結果が旧テストの期待値と食い違います。\n` +
        `旧テスト (${citation}): ${e}\n` +
        `実行結果: ${a}`
    );
  }
}

const frontmatterMod = await loadOld("tools/lib/frontmatter.js");
const chunkerMod = await loadOld("tools/lib/chunker.js");
const lineRangeMod = await loadOld("tools/lib/line-range.js");
const embeddingsMod = await loadOld("tools/lib/embeddings.js");
const syncPlannerMod = await loadOld("tools/lib/sync-planner.js");
const metadataSchemaMod = await loadOld("tools/lib/metadata-schema.js");
const downloadArticleMod = await loadOld("download-article.js");

// ===========================================================================
// 1. frontmatter (tools/lib/frontmatter.js)
// 転記元: test/frontmatter.test.js
// ===========================================================================

function buildFrontmatterFixture() {
  const { parseFrontmatter, setFrontmatterValues, serializeScalar, hashBody } = frontmatterMod;

  // 転記元: test/frontmatter.test.js:17-28 (esa: frontmatter は LF、本文は CRLF という混在)
  const ESA_MIXED =
    "---\n" +
    'title: "3 設計型再構築"\n' +
    'date: "2026-04-22"\n' +
    "tags: []\n" +
    "post_number: 4744\n" +
    'url: "https://abist.esa.io/posts/4744"\n' +
    "---\n" +
    "\n" +
    "# 見出し\r\n" +
    "\r\n" +
    "本文1行目\r\n";

  // 転記元: test/frontmatter.test.js:31-39 (web(catiadoc) / B32doc: 全 CRLF)
  const WEB_CRLF =
    "---\r\n" +
    'title: "IDL API Deprecated Index"\r\n' +
    'url: "http://catiadoc.free.fr/x.htm"\r\n' +
    "date: 2026-01-06\r\n" +
    "source: web\r\n" +
    "---\r\n" +
    "\r\n" +
    "本文\r\n";

  // 転記元: test/frontmatter.test.js:42-53 (B32doc: ブロック配列・ネストを含む)
  const B32DOC_BLOCK =
    "---\r\n" +
    "source_path: C:\\Temp\\docs\\B32doc\\Japanese\\X.logFWK\r\n" +
    "extracted_at: '2025-12-21T08:58:03.808142'\r\n" +
    "hash: sha1:95e1547bc81cb9b330f1e4fbd1606efb56a550be\r\n" +
    "tags:\r\n" +
    "  - catia\r\n" +
    "  - log\r\n" +
    "nested:\r\n" +
    "  a: 1\r\n" +
    "---\r\n" +
    "本文\r\n";

  // 転記元: test/frontmatter.test.js:55
  const NO_FRONTMATTER = "# 手書きメモ\n\n本文だけのファイル\n";

  // 転記元: test/frontmatter.test.js:57
  const EMPTY = "";

  // 転記元: test/frontmatter.test.js:59
  const BOM_DOC = "\uFEFF" + "---\n" + 'title: "BOM付き"\n' + "---\n" + "本文\n";

  const NAMED_INPUTS = { ESA_MIXED, WEB_CRLF, B32DOC_BLOCK, NO_FRONTMATTER, EMPTY, BOM_DOC };

  // 転記した定数が旧テストの主要な assert と一致することを実行して確認する
  // (test/frontmatter.test.js:87-131 の代表的な assert のみ。全 assert の再現はしない)
  {
    const esaFm = parseFrontmatter(ESA_MIXED);
    assertMatchesOldTest(esaFm.hasFrontmatter, true, "parseFrontmatter(ESA_MIXED).hasFrontmatter", "test/frontmatter.test.js:89");
    assertMatchesOldTest(esaFm.eol, "\n", "parseFrontmatter(ESA_MIXED).eol", "test/frontmatter.test.js:90");
    assertMatchesOldTest(esaFm.bom, "", "parseFrontmatter(ESA_MIXED).bom", "test/frontmatter.test.js:91");
    assertMatchesOldTest(esaFm.data.title, "3 設計型再構築", "parseFrontmatter(ESA_MIXED).data.title", "test/frontmatter.test.js:92");
    assertMatchesOldTest(esaFm.data.post_number, 4744, "parseFrontmatter(ESA_MIXED).data.post_number", "test/frontmatter.test.js:93");
    assertMatchesOldTest(esaFm.data.tags, [], "parseFrontmatter(ESA_MIXED).data.tags", "test/frontmatter.test.js:94");
    assertMatchesOldTest(esaFm.body, "\n# 見出し\r\n\r\n本文1行目\r\n", "parseFrontmatter(ESA_MIXED).body", "test/frontmatter.test.js:95");

    const webFm = parseFrontmatter(WEB_CRLF);
    assertMatchesOldTest(webFm.eol, "\r\n", "parseFrontmatter(WEB_CRLF).eol", "test/frontmatter.test.js:101");
    assertMatchesOldTest(webFm.data.source, "web", "parseFrontmatter(WEB_CRLF).data.source", "test/frontmatter.test.js:102");
    assertMatchesOldTest(webFm.body, "\r\n本文\r\n", "parseFrontmatter(WEB_CRLF).body", "test/frontmatter.test.js:104");

    const noFm = parseFrontmatter(NO_FRONTMATTER);
    assertMatchesOldTest(noFm.hasFrontmatter, false, "parseFrontmatter(NO_FRONTMATTER).hasFrontmatter", "test/frontmatter.test.js:109");
    assertMatchesOldTest(noFm.body, NO_FRONTMATTER, "parseFrontmatter(NO_FRONTMATTER).body", "test/frontmatter.test.js:111");

    const bomFm = parseFrontmatter(BOM_DOC);
    assertMatchesOldTest(bomFm.bom, "﻿", "parseFrontmatter(BOM_DOC).bom", "test/frontmatter.test.js:116");
    assertMatchesOldTest(bomFm.data.title, "BOM付き", "parseFrontmatter(BOM_DOC).data.title", "test/frontmatter.test.js:117");

    const blockFm = parseFrontmatter(B32DOC_BLOCK);
    assertMatchesOldTest(blockFm.data.hash, "sha1:95e1547bc81cb9b330f1e4fbd1606efb56a550be", "parseFrontmatter(B32DOC_BLOCK).data.hash", "test/frontmatter.test.js:125");
    assertMatchesOldTest(blockFm.data.extracted_at, "2025-12-21T08:58:03.808142", "parseFrontmatter(B32DOC_BLOCK).data.extracted_at", "test/frontmatter.test.js:126");
    assertMatchesOldTest([...blockFm.blockKeys].sort(), ["nested", "tags"], "parseFrontmatter(B32DOC_BLOCK).blockKeys", "test/frontmatter.test.js:127-128");
    assertMatchesOldTest(blockFm.data.tags, undefined, "parseFrontmatter(B32DOC_BLOCK).data.tags", "test/frontmatter.test.js:130");

    for (const [name, input] of Object.entries(NAMED_INPUTS)) {
      assertMatchesOldTest(setFrontmatterValues(input, {}).text, input, `setFrontmatterValues(${name}, {}).text`, "test/frontmatter.test.js:65-76");
    }
  }

  const cases = [];

  const setToBlockKeys = (blockKeys) => [...blockKeys].sort();

  for (const [name, input] of Object.entries(NAMED_INPUTS)) {
    const fm = parseFrontmatter(input);
    cases.push({
      id: `parse_${name}`,
      input_b64: b64(input),
      expected: {
        hasFrontmatter: fm.hasFrontmatter,
        // fm.bom は '' か '﻿' の二値なので bomPresent で全情報を失わず表せるが、
        // brief は parseFrontmatter の「全戻り値」を残せと明示しているため、
        // bom フィールドそのものも（生の BOM バイトを JSON テキストに直接埋め込まず）
        // base64 で記録する。
        bomPresent: fm.bom.length > 0,
        bom_b64: b64(fm.bom),
        eol: fm.eol,
        data: fm.data,
        keys: fm.keys,
        blockKeys: setToBlockKeys(fm.blockKeys),
        body_b64: b64(fm.body),
        raw_b64: b64(fm.raw),
        hashBody: hashBody(input),
      },
    });
  }

  // setFrontmatterValues で Stage 1 の統一スキーマ4キーを付ける
  // (brief: 「setFrontmatterValues で4メタキーを付けた結果」)
  const BACKFILL_UPDATE = {
    source: "esa",
    managed_by: "esa-sync",
    document_type: "meeting",
    status: "active",
  };
  for (const [name, input] of Object.entries(NAMED_INPUTS)) {
    const result = setFrontmatterValues(input, BACKFILL_UPDATE);
    cases.push({
      id: `setvalues_backfill_${name}`,
      input_b64: b64(input),
      input_updates: BACKFILL_UPDATE,
      expected: {
        text_b64: b64(result.text),
        changed: result.changed,
        created: result.created,
        added: result.added,
        updated: result.updated,
        skipped: result.skipped,
      },
    });
  }

  // 転記元: test/frontmatter.test.js:65-76 (無変更ラウンドトリップ: updates={})
  for (const [name, input] of Object.entries(NAMED_INPUTS)) {
    const result = setFrontmatterValues(input, {});
    cases.push({
      id: `setvalues_noop_${name}`,
      input_b64: b64(input),
      input_updates: {},
      expected: {
        text_b64: b64(result.text),
        changed: result.changed,
        created: result.created,
        added: result.added,
        updated: result.updated,
        skipped: result.skipped,
      },
    });
  }

  // 転記元: test/frontmatter.test.js:78-81 (値を既存と同じにする更新)
  {
    const result = setFrontmatterValues(ESA_MIXED, { post_number: 4744 });
    cases.push({
      id: "setvalues_same_value_no_op",
      input_b64: b64(ESA_MIXED),
      input_updates: { post_number: 4744 },
      expected: {
        text_b64: b64(result.text),
        changed: result.changed,
        added: result.added,
        updated: result.updated,
        skipped: result.skipped,
      },
    });
  }

  // 転記元: test/frontmatter.test.js:160-171 (既存キーの更新は該当行のみ差し替える)
  {
    const result = setFrontmatterValues(WEB_CRLF, { title: "変更後" });
    cases.push({
      id: "setvalues_update_existing_key",
      input_b64: b64(WEB_CRLF),
      input_updates: { title: "変更後" },
      expected: {
        text_b64: b64(result.text),
        changed: result.changed,
        added: result.added,
        updated: result.updated,
        skipped: result.skipped,
        parsedBodyAfter_b64: b64(parseFrontmatter(result.text).body),
      },
    });
  }

  // 転記元: test/frontmatter.test.js:182-192 (frontmatter が無いファイルには新規に frontmatter を作る)
  {
    const updates = { source: "manual", status: "active" };
    const result = setFrontmatterValues(NO_FRONTMATTER, updates);
    cases.push({
      id: "setvalues_create_new_frontmatter",
      input_b64: b64(NO_FRONTMATTER),
      input_updates: updates,
      expected: {
        text_b64: b64(result.text),
        created: result.created,
        added: result.added,
        hashBodyMatchesOriginal: hashBody(result.text) === hashBody(NO_FRONTMATTER),
      },
    });
  }

  // 転記元: test/frontmatter.test.js:114-121 (BOM を保持する)
  {
    const updates = { status: "active" };
    const result = setFrontmatterValues(BOM_DOC, updates);
    cases.push({
      id: "setvalues_bom_preserved",
      input_b64: b64(BOM_DOC),
      input_updates: updates,
      expected: {
        text_b64: b64(result.text),
        startsWithBomDelimiter: result.text.startsWith("\uFEFF---\n"),
        bodyAfter_b64: b64(parseFrontmatter(result.text).body),
      },
    });
  }

  // 転記元: test/frontmatter.test.js:194-198 (ブロック値のキーは更新せず skipped として報告する)
  {
    const updates = { tags: ["x"] };
    const result = setFrontmatterValues(B32DOC_BLOCK, updates);
    cases.push({
      id: "setvalues_block_key_skipped",
      input_b64: b64(B32DOC_BLOCK),
      input_updates: updates,
      expected: {
        text_b64: b64(result.text),
        skipped: result.skipped,
        unchanged: result.text === B32DOC_BLOCK,
      },
    });
  }

  // 転記元: test/frontmatter.test.js:221-233 (hashBody の性質)
  cases.push({
    id: "hashbody_unaffected_by_frontmatter_change",
    input_b64: b64(ESA_MIXED),
    expected: {
      hashWithMeta: hashBody(setFrontmatterValues(ESA_MIXED, { status: "active" }).text),
      hashOriginal: hashBody(ESA_MIXED),
      equal: hashBody(setFrontmatterValues(ESA_MIXED, { status: "active" }).text) === hashBody(ESA_MIXED),
    },
  });
  cases.push({
    id: "hashbody_detects_body_change",
    input_b64: b64(ESA_MIXED),
    expected: {
      hashOriginal: hashBody(ESA_MIXED),
      hashAppended: hashBody(ESA_MIXED + "追記\r\n"),
      equal: hashBody(ESA_MIXED) === hashBody(ESA_MIXED + "追記\r\n"),
    },
  });
  {
    // 転記元: test/frontmatter.test.js:231-233 (LF 本文 と CRLF 本文で hashBody が異なる — 混在保護の核心)
    const lfDoc = "---\ntitle: a\n---\n本文\n";
    const crlfDoc = "---\ntitle: a\n---\r\n本文\r\n";
    cases.push({
      id: "hashbody_detects_eol_change",
      input_b64: b64(lfDoc),
      expected: {
        hashLf: hashBody(lfDoc),
        hashCrlf: hashBody(crlfDoc),
        equal: hashBody(lfDoc) === hashBody(crlfDoc),
      },
    });
  }

  // serializeScalar 転記元: test/frontmatter.test.js:205-214
  // 第2要素は旧テストが assert.equal で固定した文字列そのもの。
  // 実行結果と食い違えば転記ミスか仕様変化なので、ここで自己検証して異常終了する
  // （brief: 転記した定数が旧テストの期待値と一致しない場合は値を合わせず調査して報告する）。
  const SERIALIZE_CASES = [
    ["esa", "esa"],
    ["esa-sync", "esa-sync"],
    ["2026-04-22", '"2026-04-22"'],
    ["true", '"true"'],
    ["a: b", '"a: b"'],
    ['say "hi"', '"say \\"hi\\""'],
    [true, "true"],
    [42, "42"],
    [["a", "b"], '["a", "b"]'],
    [[], "[]"],
  ];
  for (const [input, expectedFromOldTest] of SERIALIZE_CASES) {
    const actual = serializeScalar(input);
    if (actual !== expectedFromOldTest) {
      throw new Error(
        `serializeScalar(${JSON.stringify(input)}) の実行結果が旧テストの期待値と食い違います。\n` +
          `旧テスト (test/frontmatter.test.js:205-214): ${JSON.stringify(expectedFromOldTest)}\n` +
          `実行結果: ${JSON.stringify(actual)}`
      );
    }
    cases.push({
      id: `serialize_scalar_${JSON.stringify(input)}`,
      input,
      expected: { value: actual },
    });
  }

  return { schema: 1, source: "tools/lib/frontmatter.js", cases };
}

// ===========================================================================
// 2. chunker (tools/lib/chunker.js)
// 転記元: test/chunker.test.js
// ===========================================================================

function buildChunkerFixture() {
  const { chunkMarkdown, estimateTokens, DEFAULT_CHUNK_OPTIONS } = chunkerMod;

  const cases = [];

  const runChunk = (id, sourceLines, options, joiner = "\n") => {
    const md = Array.isArray(sourceLines) ? sourceLines.join(joiner) : sourceLines;
    const chunks = chunkMarkdown(md, options);
    cases.push({
      id,
      input_b64: b64(md),
      input_options: options,
      expected: {
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
      },
    });
  };

  // 転記元: test/chunker.test.js:16-34
  runChunk(
    "heading_structure",
    ["# タイトル", "", "導入文", "", "## 章1", "", "章1の本文", "", "### 節1-1", "", "節1-1の本文", "", "## 章2", "", "章2の本文"],
    { maxTokens: 50 }
  );

  // 転記元: test/chunker.test.js:44-45
  runChunk("start_end_line", ["# A", "", "1行目", "", "## B", "", "2行目"], { maxTokens: 50 });

  // 転記元: test/chunker.test.js:61-62
  runChunk(
    "frontmatter_line_count",
    ["---", 'title: "x"', "post_number: 1", "---", "", "# 見出し", "", "本文"],
    { maxTokens: 50 }
  );

  // 転記元: test/chunker.test.js:71
  runChunk("no_heading", "見出しのない\n短い本文\n", { maxTokens: 50 });

  // 転記元: test/chunker.test.js:78-80 (空文書はチャンクを作らない)
  runChunk("empty_string", "", {});
  runChunk("empty_frontmatter_only", '---\ntitle: "x"\n---\n', {});
  runChunk("empty_whitespace_only", "   \n\n  \n", {});

  // 転記元: test/chunker.test.js:88-89 (コードブロックを途中で分割しない)
  {
    const code = Array.from({ length: 60 }, (_, i) => `console.log(${i});`).join("\n");
    runChunk("code_block_not_split", ["# 見出し", "", "```js", code, "```", ""], { maxTokens: 50 });
  }

  // 転記元: test/chunker.test.js:100-101 (表を途中で分割しない)
  {
    const rows = Array.from({ length: 60 }, (_, i) => `| 行${i} | 値${i} |`).join("\n");
    runChunk("table_not_split", ["# 見出し", "", "| 列A | 列B |", "| --- | --- |", rows, ""], { maxTokens: 50 });
  }

  // 転記元: test/chunker.test.js:113
  runChunk(
    "code_block_fake_heading",
    ["# 本物の見出し", "", "```md", "# これは見出しではない", "```", "", "本文"],
    { maxTokens: 500 }
  );

  // 転記元: test/chunker.test.js:125-126 (長い節は maxTokens を目安に分割する)
  {
    const paragraphs = Array.from({ length: 40 }, (_, i) => `これは段落${i}です。`.repeat(10)).join("\n\n");
    runChunk("long_section_split", `# 見出し\n\n${paragraphs}\n`, { maxTokens: 200 });
  }

  // 転記元: test/chunker.test.js:138-146 (maxTokens は設定値で、既定値を固定しない)
  {
    const paragraphs = Array.from({ length: 40 }, (_, i) => `段落${i}。`.repeat(20)).join("\n\n");
    const md = `# 見出し\n\n${paragraphs}\n`;
    runChunk("maxtokens_small_100", md, { maxTokens: 100 });
    runChunk("maxtokens_large_2000", md, { maxTokens: 2000 });
  }
  cases.push({
    id: "default_chunk_options",
    input: null,
    expected: { value: DEFAULT_CHUNK_OPTIONS },
  });

  // 転記元: test/chunker.test.js:149-150 (分割された各チャンクが同じ heading_path を保つ)
  {
    const paragraphs = Array.from({ length: 30 }, (_, i) => `段落${i}。`.repeat(20)).join("\n\n");
    runChunk("heading_path_consistency", `# 親\n\n## 子\n\n${paragraphs}\n`, { maxTokens: 150 });
  }

  // 転記元: test/chunker.test.js:180-181 (識別子を含む行が欠落しない)
  {
    const identifiers = ["#398", "PyQt6", "HybridShapeFactory", "shrink_clamp_bellow_overlap_mm", "v2026.07.26.03"];
    runChunk(
      "identifiers_preserved",
      ["# 仕様", "", ...identifiers.map((id) => `- ${id} の説明`), ""],
      { maxTokens: 30 }
    );
  }

  // 転記元: test/chunker.test.js:195 (各チャンクに内容ハッシュが付く)
  runChunk("duplicate_content_hash", "# A\n\n同じ本文\n\n# B\n\n同じ本文\n", { maxTokens: 500 });

  // 転記元: test/chunker.test.js:210 (CRLF の文書でも行番号が正しい)
  runChunk(
    "crlf_line_numbers",
    ["# 見出し", "", "本文1", "", "## 章", "", "本文2"],
    { maxTokens: 50 },
    "\r\n"
  );

  // estimateTokens 単体テスト。転記元: test/chunker.test.js:163-172
  const estimateCases = [
    ["empty", ""],
    ["japanese", "これは日本語のテキストです"],
    ["english", "this is an english sentence for testing"],
    ["japanese_100", "あ".repeat(100)],
    ["japanese_10", "あ".repeat(10)],
    // brief 要求: CJK/ASCII 混在ケース（旧テストには無いため、実行して値を記録する）
    ["mixed_cjk_ascii", "これはmixed文章testです123 ABC"],
  ];
  const estimateTokenCases = estimateCases.map(([name, text]) => ({
    id: `estimate_tokens_${name}`,
    input_b64: b64(text),
    expected: { tokens: estimateTokens(text) },
  }));

  return {
    schema: 1,
    source: "tools/lib/chunker.js",
    cases: [...cases, ...estimateTokenCases],
  };
}

// ===========================================================================
// 3. line-range (tools/lib/line-range.js)
// 転記元: test/line-range.test.js
// ===========================================================================

function buildLineRangeFixture() {
  const { splitDocLines, sliceRange, rangeHash } = lineRangeMod;

  const cases = [];

  const record = (id, text, fn) => {
    cases.push({ id, input_b64: b64(text), expected: fn() });
  };

  // 転記元: test/line-range.test.js:9-14 (CRLF と LF で同じ行配列・同じハッシュになる)
  {
    const lf = "a\nb\nc\n";
    const crlf = "a\r\nb\r\nc\r\n";
    record("crlf_lf_equivalence_lf", lf, () => ({
      lines: splitDocLines(lf),
      rangeHash1_3: rangeHash(lf, 1, 3),
    }));
    record("crlf_lf_equivalence_crlf", crlf, () => ({
      lines: splitDocLines(crlf),
      rangeHash1_3: rangeHash(crlf, 1, 3),
    }));
  }

  // 転記元: test/line-range.test.js:16-20 (先頭の BOM は 1 個だけ除去される)
  {
    const withBom = "\uFEFFtitle\nbody";
    record("bom_stripped_once", withBom, () => ({
      lines: splitDocLines(withBom),
      rangeHash1_1: rangeHash(withBom, 1, 1),
    }));
  }

  // 転記元: test/line-range.test.js:22-28 (1 始まり・両端含みで切り出す)
  {
    const text = "l1\nl2\nl3\nl4";
    record("slice_range_inclusive", text, () => ({ slice2_3: sliceRange(text, 2, 3) }));
  }

  // 転記元: test/line-range.test.js:30-35 (ハッシュは切り出しテキストの sha256。trim なし・末尾改行なし)
  {
    const text = "  spaced  \nnext";
    record("hash_no_trim", text, () => ({ rangeHash1_2: rangeHash(text, 1, 2) }));
  }

  // 転記元: test/line-range.test.js:37-42 (単一行の範囲も切り出せる)
  record("single_line_range", "only", () => ({ rangeHash1_1: rangeHash("only", 1, 1) }));

  // 転記元: test/line-range.test.js:44-52 (範囲外はエラーを返す)
  {
    const text = "a\nb\nc";
    const boundaryPairs = [
      [0, 1],
      [1, 4],
      [3, 2],
      [-1, 2],
      [1.5, 2],
    ];
    record("out_of_bounds", text, () => ({
      results: boundaryPairs.map(([s, e]) => ({ start: s, end: e, result: rangeHash(text, s, e) })),
    }));
  }

  // 転記元: test/line-range.test.js:54-57 (kb-search get_document と同じ行分割になる)
  record("kb_search_line_split", "a\r\n\r\nc", () => ({ lines: splitDocLines("a\r\n\r\nc") }));

  // ---------------------------------------------------------------------
  // brief 要求のマトリクス: BOM有無 × CRLF/LF × 境界(先頭行・末尾行・単一行)
  // ---------------------------------------------------------------------
  const BASE_LINES = ["一行目 line1", "二行目 line2", "三行目 line3", "四行目 line4"];
  for (const bom of [false, true]) {
    for (const eol of ["\n", "\r\n"]) {
      const body = BASE_LINES.join(eol) + eol;
      const text = (bom ? "\uFEFF" : "") + body;
      const label = `bom${bom ? "1" : "0"}_${eol === "\n" ? "lf" : "crlf"}`;
      const totalLines = splitDocLines(text).length;
      record(`matrix_${label}_first_line`, text, () => ({ rangeHash: rangeHash(text, 1, 1) }));
      record(`matrix_${label}_last_line`, text, () => ({ rangeHash: rangeHash(text, totalLines, totalLines) }));
      record(`matrix_${label}_single_middle_line`, text, () => ({ rangeHash: rangeHash(text, 2, 2) }));
      record(`matrix_${label}_full_range`, text, () => ({ rangeHash: rangeHash(text, 1, totalLines) }));
    }
  }

  return { schema: 1, source: "tools/lib/line-range.js", cases };
}

// ===========================================================================
// 4. embeddings / e5 input (tools/lib/embeddings.js)
// 旧テストファイル無し（embeddings.test.js は存在しない）。ソースを直接実行して記録する。
// ===========================================================================

function buildEmbeddingsFixture() {
  const { embeddingInput, embeddingInputHash, queryInput, modelConfig, EMBEDDING_MODELS } = embeddingsMod;

  const cases = [];

  cases.push({
    id: "embedding_models_table",
    input: null,
    expected: { value: EMBEDDING_MODELS },
  });

  const E5_SMALL = "Xenova/multilingual-e5-small";
  const OPENAI_SMALL = "text-embedding-3-small";

  for (const model of [E5_SMALL, "Xenova/multilingual-e5-base", OPENAI_SMALL, "text-embedding-unknown-variant", "some-unknown-local-model"]) {
    cases.push({
      id: `model_config_${model.replace(/[^a-zA-Z0-9]/g, "_")}`,
      input: model,
      expected: { value: modelConfig(model) },
    });
  }

  const recordEmbeddingInput = (id, chunk, model) => {
    cases.push({
      id,
      input: { chunk: { title: chunk.title ?? null, heading_path: chunk.heading_path ?? null, text_b64: b64(chunk.text ?? "") }, model },
      expected: {
        embeddingInput_b64: b64(embeddingInput(chunk, model)),
        embeddingInputHash: embeddingInputHash(chunk, model),
        inputLength: embeddingInput(chunk, model).length,
      },
    });
  };

  // 短く、512字切詰の影響を受けないケース
  recordEmbeddingInput("short_no_truncation", { title: "仕様書", heading_path: "章1", text: "短い本文です。" }, E5_SMALL);

  // brief 必須ケース: 512字切詰の境界をまたぐ。
  // 「切詰の後に接頭辞を足す」正しい実装と「接頭辞を足してから切詰める」誤実装とで
  // 出力の長さ・内容が明確に異なることを、このケースで確認する。
  recordEmbeddingInput(
    "boundary_straddle_512",
    { title: "仕様書タイトル", heading_path: "章1 > 節1-1", text: "本文".repeat(300) },
    E5_SMALL
  );

  // 境界ちょうど（joined length === 512、切詰は発生しないが境界の等号条件を検証する）
  recordEmbeddingInput("boundary_exact_512", { title: "", heading_path: "", text: "x".repeat(512) }, E5_SMALL);

  // 境界を1文字だけ超える（オフバイワン検証）
  recordEmbeddingInput("boundary_513_one_over", { title: "", heading_path: "", text: "x".repeat(513) }, E5_SMALL);

  // OpenAI モデル（maxInputChars=16000, prefix=''）は同条件でも切詰も接頭辞も入らない
  recordEmbeddingInput(
    "openai_model_no_prefix_no_truncation",
    { title: "仕様書", heading_path: "章1 > 節1", text: "本文".repeat(700) },
    OPENAI_SMALL
  );

  // title / heading_path が無いチャンク（parts に含まれないことを確認）
  recordEmbeddingInput("no_title_no_heading_path", { text: "本文のみのチャンク" }, E5_SMALL);

  // queryInput: 短いクエリ・512字境界をまたぐクエリ
  for (const [id, query, model] of [
    ["query_short_e5", "検索クエリ", E5_SMALL],
    ["query_boundary_513_e5", "q".repeat(513), E5_SMALL],
    ["query_long_openai", "q".repeat(600), OPENAI_SMALL],
  ]) {
    cases.push({
      id,
      input: { query_b64: b64(query), model },
      expected: { queryInput_b64: b64(queryInput(query, model)), length: queryInput(query, model).length },
    });
  }

  return { schema: 1, source: "tools/lib/embeddings.js", cases };
}

// ===========================================================================
// 5. sync-planner (tools/lib/sync-planner.js)
// 転記元: test/sync-planner.test.js
// ===========================================================================

function buildSyncPlannerFixture() {
  const { decideSyncAction, decideMissingCandidate, SYNC_ACTIONS, newSyncSummary, recordSyncResult } = syncPlannerMod;

  const cases = [];

  // SYNC_ACTIONS はモジュールから直接取得（転記ではなく実行結果そのもの）
  cases.push({ id: "sync_actions_enum", input: null, expected: { value: SYNC_ACTIONS } });

  // 転記元: test/sync-planner.test.js:12-14
  const HASH_A = "a".repeat(64);
  const HASH_B = "b".repeat(64);
  const HASH_C = "c".repeat(64);

  // 転記元: test/sync-planner.test.js:16-27 (decide() ヘルパーの既定値)
  const baseInput = () => ({
    remote: { contentHash: HASH_A, updatedAt: "2026-07-01T00:00:00+09:00" },
    record: {
      source_content_hash: HASH_A,
      source_updated_at: "2026-07-01T00:00:00+09:00",
      local_content_hash: HASH_B,
    },
    local: { exists: true, bodyHash: HASH_B },
  });

  const decideCase = (id, overrides, citation) => {
    const input = { ...baseInput(), ...overrides };
    cases.push({
      id,
      input: { ...input, _citation: citation },
      expected: decideSyncAction(input),
    });
  };

  decideCase("decide_unchanged", {}, "test/sync-planner.test.js:33-37");
  decideCase("decide_create_no_local_file", { local: { exists: false } }, "test/sync-planner.test.js:39-43");
  decideCase("decide_adopt_no_record", { record: null }, "test/sync-planner.test.js:45-50");
  decideCase(
    "decide_update_content_changed",
    { remote: { contentHash: HASH_C, updatedAt: "2026-07-25T00:00:00+09:00" } },
    "test/sync-planner.test.js:56-60"
  );
  decideCase(
    "decide_unchanged_updated_at_only_hash_wins",
    { remote: { contentHash: HASH_A, updatedAt: "2026-07-25T00:00:00+09:00" } },
    "test/sync-planner.test.js:62-66"
  );
  decideCase(
    "decide_update_via_updated_at_fallback_older",
    {
      record: { source_content_hash: null, source_updated_at: "2026-07-01T00:00:00+09:00", local_content_hash: HASH_B },
      remote: { contentHash: HASH_C, updatedAt: "2026-07-25T00:00:00+09:00" },
    },
    "test/sync-planner.test.js:69-74"
  );
  decideCase(
    "decide_unchanged_via_updated_at_fallback_same",
    {
      record: { source_content_hash: null, source_updated_at: "2026-07-25T00:00:00+09:00", local_content_hash: HASH_B },
      remote: { contentHash: HASH_C, updatedAt: "2026-07-25T00:00:00+09:00" },
    },
    "test/sync-planner.test.js:75-79"
  );
  decideCase(
    "decide_local_modified",
    { local: { exists: true, bodyHash: HASH_C } },
    "test/sync-planner.test.js:86-90"
  );
  decideCase(
    "decide_conflict",
    { local: { exists: true, bodyHash: HASH_C }, remote: { contentHash: HASH_C, updatedAt: "2026-07-25T00:00:00+09:00" } },
    "test/sync-planner.test.js:92-100"
  );
  decideCase(
    "decide_conflict_overwritten_force",
    {
      local: { exists: true, bodyHash: HASH_C },
      remote: { contentHash: HASH_C, updatedAt: "2026-07-25T00:00:00+09:00" },
      force: true,
    },
    "test/sync-planner.test.js:102-111"
  );
  decideCase(
    "decide_unknown_local_no_recorded_hash",
    {
      record: { source_content_hash: HASH_A, source_updated_at: "2026-07-01T00:00:00+09:00", local_content_hash: null },
      local: { exists: true, bodyHash: HASH_C },
    },
    "test/sync-planner.test.js:113-121"
  );

  // 転記元: test/sync-planner.test.js:227-245 (再実行の安定性: update → unchanged)
  {
    const first = decideSyncAction({ ...baseInput(), remote: { contentHash: HASH_C, updatedAt: "2026-07-25T00:00:00+09:00" } });
    const nextRecord = {
      source_content_hash: HASH_C,
      source_updated_at: "2026-07-25T00:00:00+09:00",
      local_content_hash: "written-body-hash",
    };
    const secondInput = {
      remote: { contentHash: HASH_C, updatedAt: "2026-07-25T00:00:00+09:00" },
      record: nextRecord,
      local: { exists: true, bodyHash: "written-body-hash" },
    };
    const second = decideSyncAction(secondInput);
    cases.push({
      id: "decide_rerun_stability_update_then_unchanged",
      input: { first: { ...baseInput(), remote: { contentHash: HASH_C, updatedAt: "2026-07-25T00:00:00+09:00" } }, second: secondInput },
      expected: { first, second },
    });
  }

  // decideMissingCandidate 転記元: test/sync-planner.test.js:127-178 (3条件)
  const missingCase = (id, input, citation) => {
    cases.push({ id, input: { ...input, _citation: citation }, expected: decideMissingCandidate(input) });
  };
  missingCase(
    "missing_candidate_full_sync_failed",
    { fullSyncSucceeded: false, missingCount: 5, individualFetchFailed: true, threshold: 3 },
    "test/sync-planner.test.js:127-136"
  );
  missingCase(
    "missing_candidate_below_threshold",
    { fullSyncSucceeded: true, missingCount: 2, individualFetchFailed: true, threshold: 3 },
    "test/sync-planner.test.js:138-147"
  );
  missingCase(
    "missing_candidate_individual_fetch_succeeded",
    { fullSyncSucceeded: true, missingCount: 9, individualFetchFailed: false, threshold: 3 },
    "test/sync-planner.test.js:149-158"
  );
  missingCase(
    "missing_candidate_all_conditions_met",
    { fullSyncSucceeded: true, missingCount: 3, individualFetchFailed: true, threshold: 3 },
    "test/sync-planner.test.js:160-168"
  );

  // 転記元: test/sync-planner.test.js:193-212 (判定結果をサマリへ集計できる)
  {
    const summary = newSyncSummary("esa");
    recordSyncResult(summary, { path: "a.md", action: "create" });
    recordSyncResult(summary, { path: "b.md", action: "update" });
    recordSyncResult(summary, { path: "c.md", action: "unchanged" });
    recordSyncResult(summary, { path: "d.md", action: "local_modified" });
    recordSyncResult(summary, { path: "e.md", action: "conflict" });
    recordSyncResult(summary, { path: "f.md", action: "error", error: "boom" });
    cases.push({
      id: "record_sync_result_totals",
      input: null,
      expected: { totals: summary.totals, actionCounts: summary.actionCounts, items: summary.items },
    });
  }

  // ACTION_BUCKET はモジュール内部の非公開定数（export されていない）。
  // 転記ではなく、SYNC_ACTIONS の全要素それぞれについて recordSyncResult を実行し、
  // どの totals バケットが加算されたかを観測することで実行結果として導出する。
  // 転記元: test/sync-planner.test.js:214-221 (全ての行動が既知の区分に対応づく)
  {
    const actionBucket = {};
    for (const action of SYNC_ACTIONS) {
      const summary = newSyncSummary("probe");
      recordSyncResult(summary, { path: `${action}.md`, action });
      const bucket = Object.keys(summary.totals).find((k) => summary.totals[k] === 1);
      actionBucket[action] = bucket;
    }
    cases.push({
      id: "action_bucket_derived",
      input: { note: "ACTION_BUCKET は非公開のため recordSyncResult の観測結果から導出" },
      expected: { mapping: actionBucket, actionsCovered: Object.keys(actionBucket).sort() },
    });
  }

  return { schema: 1, source: "tools/lib/sync-planner.js", cases };
}

// ===========================================================================
// 6. metadata-schema (tools/lib/metadata-schema.js) + sanitize 系 (download-article.js)
// 転記元: test/metadata-schema.test.js, test/downloaders.test.js
// ===========================================================================

function buildMetadataSchemaFixture() {
  const {
    SOURCES,
    MANAGED_BY,
    DOCUMENT_TYPES,
    STATUSES,
    SYNC_STATUSES,
    FRONTMATTER_KEY_ORDER,
    MANAGED_BY_FOR_SOURCE,
    BACKFILL_KEYS,
    REFERENCE_CORPUS_PREFIXES,
    classifyDocument,
    validateMetadata,
    isReferenceCorpus,
    toPosixPath,
    resolveBatchOutputDirs,
    resolveGitOutputDirs,
  } = metadataSchemaMod;
  const { sanitizeFileName, sanitizeCategoryPath } = downloadArticleMod;

  const cases = [];

  // 直接 export された列挙値・定数（転記ではなく import した値そのもの）
  for (const [id, value] of [
    ["sources_enum", SOURCES],
    ["managed_by_enum", MANAGED_BY],
    ["document_types_enum", DOCUMENT_TYPES],
    ["statuses_enum", STATUSES],
    ["sync_statuses_enum", SYNC_STATUSES],
    ["frontmatter_key_order", FRONTMATTER_KEY_ORDER],
    ["managed_by_for_source", MANAGED_BY_FOR_SOURCE],
    ["backfill_keys", BACKFILL_KEYS],
    ["reference_corpus_prefixes", REFERENCE_CORPUS_PREFIXES],
  ]) {
    cases.push({ id, input: null, expected: { value } });
  }

  // 転記元: test/metadata-schema.test.js:17 (GIT_DIRS)
  const GIT_DIRS = ["docs/catia-macro-generator", "docs/catia-flotherm-prep"];
  const classify = (id, relativePath, frontmatter, citation) => {
    const input = { relativePath, frontmatter, gitOutputDirs: GIT_DIRS };
    cases.push({ id, input: { ...input, _citation: citation }, expected: classifyDocument(input) });
  };

  classify("classify_post_number_esa", "チーム内定例/議事録/2026_07_23.md", { post_number: 4744, title: "x" }, "test/metadata-schema.test.js:27-32");
  classify("classify_source_web", "knowledge/catiadoc/online/x.htm.md", { source: "web", url: "http://x" }, "test/metadata-schema.test.js:34-39");
  classify("classify_git_dir", "catia-macro-generator/README.md", {}, "test/metadata-schema.test.js:41-46");
  classify("classify_manual_default", "議事録/手書きメモ.md", {}, "test/metadata-schema.test.js:48-52");

  let i = 0;
  for (const p of [
    "superpowers/plans/2026-07-01-x.md",
    "2026-07-01.md",
    "議事録/設計効率化/三桜工業様定例/2026_07_23_三桜工業様定例.md",
  ]) {
    classify(`classify_manual_handwritten_${i++}`, p, {}, "test/metadata-schema.test.js:54-64");
  }

  i = 0;
  for (const value of ["", null, "なし", "#398 の話", false, {}]) {
    cases.push({
      id: `classify_post_number_non_numeric_${i++}`,
      input: { relativePath: "メモ.md", frontmatter: { post_number: value }, gitOutputDirs: GIT_DIRS, _citation: "test/metadata-schema.test.js:66-71" },
      expected: classifyDocument({ relativePath: "メモ.md", frontmatter: { post_number: value }, gitOutputDirs: GIT_DIRS }),
    });
  }

  classify("classify_source_website_not_web", "x.md", { source: "website" }, "test/metadata-schema.test.js:73-76");
  classify("classify_source_web_case_insensitive", "x.md", { source: "Web" }, "test/metadata-schema.test.js:73-76");
  classify("classify_existing_source_conflict", "チーム内定例/x.md", { source: "manual", post_number: 4744 }, "test/metadata-schema.test.js:78-83");

  classify("classify_meeting_path_1", "議事録/設計効率化/三桜工業様定例/2026_07_23.md", { post_number: 1 }, "test/metadata-schema.test.js:89-92");
  classify("classify_meeting_path_2", "チーム内定例/設計効率化/x.md", { post_number: 1 }, "test/metadata-schema.test.js:89-92");
  classify("classify_memo_path_1", "設計効率化メモ/CATIAマクロ/x.md", { post_number: 1 }, "test/metadata-schema.test.js:94-97");
  classify("classify_memo_path_2", "knowledge/memo/x.md", { post_number: 1 }, "test/metadata-schema.test.js:94-97");
  classify("classify_specification_path_1", "蛇腹形状の自動設計/要件/REQ-001.md", { post_number: 1 }, "test/metadata-schema.test.js:99-102");
  classify("classify_specification_path_2", "x/仕様書/y.md", { post_number: 1 }, "test/metadata-schema.test.js:99-102");
  classify("classify_reference_path_1", "knowledge/B32doc/md_out/x.md", {}, "test/metadata-schema.test.js:104-107");
  classify("classify_reference_path_2", "knowledge/catiadoc/x.md", { source: "web" }, "test/metadata-schema.test.js:104-107");
  classify("classify_knowledge_default", "蛇腹形状の自動設計/設計効率化/x.md", { post_number: 1 }, "test/metadata-schema.test.js:109-111");
  classify("classify_status_active_default", "チーム内定例/設計効率化/x.md", { post_number: 1 }, "test/metadata-schema.test.js:113-118");
  classify("classify_status_archived_path", "チーム内定例/Archived/設計効率化/x.md", { post_number: 1 }, "test/metadata-schema.test.js:113-118");
  classify("classify_status_preserved", "チーム内定例/Archived/x.md", { post_number: 1, status: "active" }, "test/metadata-schema.test.js:120-123");

  // isReferenceCorpus 転記元: test/metadata-schema.test.js:129-135
  i = 0;
  for (const [p, expected] of [
    ["knowledge/B32doc/md_out/x.md", true],
    ["knowledge/catiadoc/x.md", true],
    ["knowledge/generated/b32doc/x.md", true],
    ["knowledge/memo/x.md", false],
    ["チーム内定例/x.md", false],
  ]) {
    cases.push({
      id: `is_reference_corpus_${i++}`,
      input: { relativePath: p, _expectedNote: expected, _citation: "test/metadata-schema.test.js:129-135" },
      expected: { value: isReferenceCorpus(p) },
    });
  }

  // toPosixPath 転記元: test/metadata-schema.test.js:137-140
  i = 0;
  for (const p of ["docs\\チーム内定例\\x.md", "docs/チーム内定例/x.md"]) {
    cases.push({
      id: `to_posix_path_${i++}`,
      input: { raw: p, _citation: "test/metadata-schema.test.js:137-140" },
      expected: { value: toPosixPath(p) },
    });
  }

  // validateMetadata 転記元: test/metadata-schema.test.js:158-170
  const validate = (id, meta, citation) => {
    cases.push({ id, input: { meta, _citation: citation }, expected: validateMetadata(meta) });
  };
  validate("validate_metadata_ok", { source: "esa", managed_by: "esa-sync", status: "active" }, "test/metadata-schema.test.js:159");
  validate(
    "validate_metadata_bad_enums",
    { source: "esa2", status: "ACTIVE", document_type: "note" },
    "test/metadata-schema.test.js:160-164"
  );
  validate("validate_metadata_managed_by_mismatch", { source: "esa", managed_by: "human" }, "test/metadata-schema.test.js:166-169");

  // sanitizeFileName / sanitizeCategoryPath 転記元: test/downloaders.test.js:95-102
  // 旧テストの assert.equal と実行結果が一致することをまず確認する
  assertMatchesOldTest(sanitizeFileName("a/b:c*d"), "a-b-c-d", "sanitizeFileName('a/b:c*d')", "test/downloaders.test.js:96");
  assertMatchesOldTest(sanitizeFileName("  空白  区切り "), "空白-区切り", "sanitizeFileName('  空白  区切り ')", "test/downloaders.test.js:97");
  assertMatchesOldTest(sanitizeFileName("CON"), "file-CON", "sanitizeFileName('CON')", "test/downloaders.test.js:98");
  assertMatchesOldTest(sanitizeFileName(""), "untitled", "sanitizeFileName('')", "test/downloaders.test.js:99");
  assertMatchesOldTest(sanitizeCategoryPath("議事録/設計効率化/TS様"), "議事録/設計効率化/TS様", "sanitizeCategoryPath(...)", "test/downloaders.test.js:100");
  assertMatchesOldTest(sanitizeCategoryPath(""), "", "sanitizeCategoryPath('')", "test/downloaders.test.js:101");

  const sanitizeFile = (id, name, citation) => {
    cases.push({ id, input: { name_b64: b64(name), _citation: citation }, expected: { value: sanitizeFileName(name) } });
  };
  sanitizeFile("sanitize_filename_special_chars", "a/b:c*d", "test/downloaders.test.js:96");
  sanitizeFile("sanitize_filename_whitespace", "  空白  区切り ", "test/downloaders.test.js:97");
  sanitizeFile("sanitize_filename_reserved_con", "CON", "test/downloaders.test.js:98");
  sanitizeFile("sanitize_filename_empty", "", "test/downloaders.test.js:99");

  // brief 要求: Windows 予約名の網羅（大文字小文字・拡張子付き）
  for (const name of ["con", "con.txt", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9", "CONFERENCE", "***"]) {
    sanitizeFile(`sanitize_filename_reserved_or_edge_${name.replace(/[^a-zA-Z0-9]/g, "_")}`, name, "追加ケース（旧テストに無し。255バイト切詰・予約名の網羅のため実行して記録）");
  }
  // brief 要求: 255バイト切詰（日本語 = UTF-8 3バイト/文字を含む）
  sanitizeFile("sanitize_filename_255byte_truncation_japanese", "あ".repeat(200), "追加ケース（255バイト切詰の境界を確認するため実行して記録）");
  sanitizeFile("sanitize_filename_255byte_truncation_ascii", "a".repeat(300), "追加ケース（255バイト切詰の境界を確認するため実行して記録）");

  cases.push({
    id: "sanitize_category_path_japanese",
    input: { categoryPath_b64: b64("議事録/設計効率化/TS様"), _citation: "test/downloaders.test.js:100" },
    expected: { value: sanitizeCategoryPath("議事録/設計効率化/TS様") },
  });
  cases.push({
    id: "sanitize_category_path_empty",
    input: { categoryPath_b64: b64(""), _citation: "test/downloaders.test.js:101" },
    expected: { value: sanitizeCategoryPath("") },
  });
  cases.push({
    id: "sanitize_category_path_reserved_segment",
    input: { categoryPath_b64: b64("a/CON/b"), _citation: "追加ケース（セグメント単位で予約名処理が効くことの確認）" },
    expected: { value: sanitizeCategoryPath("a/CON/b") },
  });

  // safeBatchName は metadata-schema.js の非公開関数（export されていない）。
  // download-batch.js の generateFolderName と「同一ロジック」とコメントされているが、
  // sanitizeFileName（Windows予約名処理・255バイト切詰あり）とは異なり、
  // 4つの正規表現置換のみで予約名処理も長さ制限も行わない。
  // export されている resolveBatchOutputDirs 経由で（内部で safeBatchName を呼ぶため）
  // 実行結果として間接的に記録する。
  const BATCH_CONFIG_PROBE = {
    CON: [],
    "  spaced  name  ": [],
    "a/b:c*d": [],
    [`長い名前${"ば".repeat(150)}`]: [],
    "web-with-outputdir": { type: "web", outputDir: "custom/out", url: "http://x", maxDepth: 1, delay: 1 },
    "web-without-outputdir": { type: "web", url: "http://x", maxDepth: 1, delay: 1 },
    "git-with-outputdir": { type: "git", outputDir: "g/out", repository: "https://github.com/foo/bar.git" },
    "git-without-outputdir": { type: "git", repository: "https://github.com/foo/bar-repo.git" },
    "git-ssh-form": { type: "git", repository: "git@github.com:foo/ssh-repo.git" },
    "unknown-type-excluded": { type: "other" },
  };
  cases.push({
    id: "resolve_batch_output_dirs",
    input: {
      batchConfigKeys: Object.keys(BATCH_CONFIG_PROBE),
      batchConfig: BATCH_CONFIG_PROBE,
      _note: "safeBatchName / withDocsPrefix / extractRepoName を resolveBatchOutputDirs 経由で間接検証",
    },
    expected: {
      resolved: resolveBatchOutputDirs(BATCH_CONFIG_PROBE),
      gitOutputDirs: resolveGitOutputDirs(BATCH_CONFIG_PROBE),
    },
  });

  return {
    schema: 1,
    source: "tools/lib/metadata-schema.js",
    // sanitize_filename_* / sanitize_category_path_* / resolve_batch_output_dirs の
    // 一部ケース（約15件）は download-article.js の export
    // (sanitizeFileName / sanitizeCategoryPath) を実行したものであり、
    // metadata-schema.js 由来ではない。個々の case の _citation は正しく
    // download-article.js / test/downloaders.test.js を指しているが、
    // ファイル出典が単一モジュールに限らないことをトップレベルでも明示しておく。
    additional_sources: ["download-article.js"],
    cases,
  };
}

// ===========================================================================
// 実行
// ===========================================================================

writeDeterministicJson(join(OUT_DIR, "frontmatter.json"), buildFrontmatterFixture());
writeDeterministicJson(join(OUT_DIR, "chunker.json"), buildChunkerFixture());
writeDeterministicJson(join(OUT_DIR, "line-range.json"), buildLineRangeFixture());
writeDeterministicJson(join(OUT_DIR, "embeddings.json"), buildEmbeddingsFixture());
writeDeterministicJson(join(OUT_DIR, "sync-planner.json"), buildSyncPlannerFixture());
writeDeterministicJson(join(OUT_DIR, "metadata-schema.json"), buildMetadataSchemaFixture());

assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

console.log("capture-kernel.mjs: OK (6 files written to tests/fixtures/kernel/)");
