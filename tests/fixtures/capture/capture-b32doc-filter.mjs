#!/usr/bin/env node
// tests/fixtures/capture/capture-b32doc-filter.mjs
//
// M1 追加 Task (b32doc-filter ギャップの解消): tools/lib/b32doc-filter.js の
// ゴールデン採取。
//
// PROVENANCE.md の「M2範囲だが未fixture化(既知のギャップ)」に記録されていた
// b32doc-filter.test.js の転記漏れを埋める。b32doc-filter.js は
// M4 の reference-index 構築(どの文書を索引対象にするか)の選定ロジックの
// 移植元であり、他のkernelモジュールと同じビット互換階層(M2)にある。
//
// 出力: tests/fixtures/kernel/b32doc-filter.json
//
// 重要な注意(report参照): tools/lib/b32doc-filter.js は
// tools/knowledge-curator/filters.py の「移植」と自称し、食い違えば filters.py
// を正とすることが明記されている。しかし実際に検証すると、
// - filters.py 自身が持つ decide() 関数(判定の優先順位を1つにまとめた関数)は
//   実は本番コードから一度も呼ばれていない(死んだコード)。
// - 実際に procedures-index.jsonl を生成する build_index.py は、
//   filters.decide() とは異なる優先順位(path_excluded を最初に見る)で
//   個々の関数を直接呼んでいる。
// - b32doc-filter.js の decideIndexable() は、build_index.py が実際に実行する
//   優先順位と一致する(path_excluded → toc_or_default → not_html_category →
//   language_excluded → empty_content/too_short)。
// - しかし toc/default ファイル名判定については、b32doc-filter.js は
//   frontmatter の source_name を優先し(無ければパスのbasenameへfallback)、
//   build_index.py の実際の呼び出しはディスク上のファイル名
//   (md_path.name)だけを見る。filters.decide() はここでも
//   meta.get("source_name") を見ており、b32doc-filter.js と一致する。
// このスクリプトはこの2つの発見的差異を、旧リポジトリの filters.py を実際に
// 実行して得た値として fixture に記録する(推測ではない)。

import { readFileSync, readdirSync, statSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertReadOnly, b64, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する

const ROOT = oldRepoRoot();
const DOCS_DIR = join(ROOT, "docs");
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "kernel");

const b32docFilterMod = await import(pathToFileURL(join(ROOT, "tools/lib/b32doc-filter.js")).href);
const {
  EXCLUDED_PATH_SEGMENTS,
  NOISE_FILENAME_SUBSTR,
  INDEXABLE_CATEGORIES,
  INDEXABLE_LANGUAGES,
  EMPTY_CONTENT_MARKER,
  MIN_CONTENT_CHARS,
  SECTION_HEADERS,
  extractedContent,
  extractSummaryKeys,
  decideIndexable,
} = b32docFilterMod;

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

/** 実データはすべて CRLF。転記元のテストと同じヘルパーで CRLF 文字列を組み立てる。 */
const CRLF = (lines) => lines.join("\r\n");

const cases = [];

// ===========================================================================
// 0. 定数(直接 export された値。転記ではなく import した値そのもの)
// ===========================================================================
for (const [id, value] of [
  ["excluded_path_segments", EXCLUDED_PATH_SEGMENTS],
  ["noise_filename_substr", NOISE_FILENAME_SUBSTR],
  ["indexable_categories", [...INDEXABLE_CATEGORIES].sort()],
  ["indexable_languages", [...INDEXABLE_LANGUAGES].sort()],
  ["empty_content_marker", EMPTY_CONTENT_MARKER],
  ["min_content_chars", MIN_CONTENT_CHARS],
  ["section_headers", [...SECTION_HEADERS].sort()],
]) {
  cases.push({ id, input: null, expected: { value } });
}

// ===========================================================================
// 1. extractSummaryKeys / extractedContent
// 転記元: test/b32doc-filter.test.js
// ===========================================================================

// 転記元: test/b32doc-filter.test.js:14-30 (SAMPLE)
const SAMPLE = CRLF([
  "# Abaqus for CATIA V5 自動インタフェースを使用する例",
  "",
  "## Summary Keys",
  "",
  "- タイトル: Abaqus for CATIA V5 自動インタフェースを使用する例",
  "- 概要: この節の最後に示されているスクリプトは…",
  "",
  "## Extracted Content",
  "",
  "あ".repeat(300),
  "",
  "## Links",
  "",
  "- http://example.com",
  "",
]);

{
  // 転記元: test/b32doc-filter.test.js:36-40 (CRLF でタイトル・概要を取り出せる)
  const { title, summary } = extractSummaryKeys(SAMPLE);
  assertMatchesOldTest(title, "Abaqus for CATIA V5 自動インタフェースを使用する例", "extractSummaryKeys(SAMPLE).title", "test/b32doc-filter.test.js:38");
  assertMatchesOldTest(/スクリプト/.test(summary), true, "extractSummaryKeys(SAMPLE).summary", "test/b32doc-filter.test.js:39");
  cases.push({
    id: "extract_summary_keys_crlf_sample",
    input_b64: b64(SAMPLE),
    expected: { title, summary },
  });

  // 転記元: test/b32doc-filter.test.js:42-45 (LF でも同じ結果になる)
  const lf = SAMPLE.replace(/\r/g, "");
  const lfResult = extractSummaryKeys(lf);
  assertMatchesOldTest(lfResult, { title, summary }, "extractSummaryKeys(LF variant of SAMPLE)", "test/b32doc-filter.test.js:44");
  cases.push({
    id: "extract_summary_keys_lf_equivalence",
    input_b64: b64(lf),
    expected: { title: lfResult.title, summary: lfResult.summary, equalsCrlfResult: JSON.stringify(lfResult) === JSON.stringify({ title, summary }) },
  });
}

{
  // 転記元: test/b32doc-filter.test.js:47-49 (Summary Keys が無ければ null)
  const input = "# 見出しだけ\r\n";
  const result = extractSummaryKeys(input);
  assertMatchesOldTest(result, { title: null, summary: null }, "extractSummaryKeys(no Summary Keys)", "test/b32doc-filter.test.js:48");
  cases.push({ id: "extract_summary_keys_missing_section", input_b64: b64(input), expected: result });
}

{
  // 転記元: test/b32doc-filter.test.js:55-60 (Extracted Content セクションだけを取り出す)
  const extracted = extractedContent(SAMPLE);
  assertMatchesOldTest(extracted.includes("あ"), true, "extractedContent(SAMPLE) includes body", "test/b32doc-filter.test.js:57");
  assertMatchesOldTest(extracted.includes("Summary Keys"), false, "extractedContent(SAMPLE) excludes previous section", "test/b32doc-filter.test.js:58");
  assertMatchesOldTest(extracted.includes("http://example.com"), false, "extractedContent(SAMPLE) excludes next section", "test/b32doc-filter.test.js:59");
  cases.push({ id: "extracted_content_basic", input_b64: b64(SAMPLE), expected: { extracted_b64: b64(extracted), length: extracted.length } });
}

{
  // 転記元: test/b32doc-filter.test.js:62-79 (本文中の ## 見出しでは切らない。固定セクション見出しのみで区切る)
  const body = CRLF([
    "## Extracted Content",
    "",
    "本文の始まり",
    "",
    "## これは本文中の見出し",
    "",
    "続きの本文",
    "",
    "## Links",
    "",
    "- link",
  ]);
  const extracted = extractedContent(body);
  assertMatchesOldTest(extracted.includes("続きの本文"), true, "extractedContent does not cut at inline heading", "test/b32doc-filter.test.js:77");
  assertMatchesOldTest(extracted.includes("- link"), false, "extractedContent stops at next fixed section header", "test/b32doc-filter.test.js:78");
  cases.push({
    id: "extracted_content_not_cut_by_inline_heading",
    input_b64: b64(body),
    expected: { extracted_b64: b64(extracted), includesInlineHeadingLine: extracted.includes("## これは本文中の見出し") },
  });
}

{
  // 転記元: test/b32doc-filter.test.js:81-83 (Extracted Content が無ければ空文字)
  const input = "## Summary Keys\r\n- タイトル: x\r\n";
  const extracted = extractedContent(input);
  assertMatchesOldTest(extracted, "", "extractedContent(no Extracted Content section)", "test/b32doc-filter.test.js:82");
  cases.push({ id: "extracted_content_missing_section", input_b64: b64(input), expected: { extracted_b64: b64(extracted) } });
}

// ===========================================================================
// 2. decideIndexable — ルールごとの accept/reject ケース(理由文字列を必ず記録)
// 転記元: test/b32doc-filter.test.js:89-149
// ===========================================================================

// 転記元: test/b32doc-filter.test.js:89-98 (decide ヘルパーの既定値)
function decide(overrides = {}) {
  return decideIndexable({
    relPosix: "/knowledge/B32doc/md_out/online/Japanese/x_C2/a.htm.htm.abc.md",
    sourceName: "a.htm",
    category: "html",
    language: "ja",
    body: SAMPLE,
    ...overrides,
  });
}

function decideCase(id, overrides, citation) {
  const result = decide(overrides);
  cases.push({
    id,
    input: { ...overrides, _citation: citation },
    expected: result,
  });
  return result;
}

// 転記元: test/b32doc-filter.test.js:100-102 (条件を満たせば索引対象 — 全ルールの accept 基準ケース)
{
  const r = decideCase("decide_accept_baseline", {}, "test/b32doc-filter.test.js:100-102");
  assertMatchesOldTest(r, { indexable: true, reason: null }, "decide() baseline", "test/b32doc-filter.test.js:101");
}

// --- ルール1: path_excluded (5セグメント全部、accept/reject) ---
// 転記元: test/b32doc-filter.test.js:104-109
{
  let i = 0;
  for (const segment of ["/icons_C2/", "/images/", "/samples/", "/control/", "/navigation/"]) {
    const r = decideCase(
      `decide_reject_path_excluded_${i}_${segment.replace(/\//g, "")}`,
      { relPosix: `/knowledge/B32doc${segment}a.md` },
      "test/b32doc-filter.test.js:105-108"
    );
    assertMatchesOldTest(r.reason, "path_excluded", `decide() path segment ${segment}`, "test/b32doc-filter.test.js:107");
    i++;
  }
}
decideCase("decide_accept_path_not_excluded", { relPosix: "/knowledge/B32doc/md_out/online/Japanese/x_C2/normal/a.md" }, "追加ケース(brief要求: 各ルールにaccept/reject両方)");

// --- ルール2: toc_or_default (ファイル名) ---
// 転記元: test/b32doc-filter.test.js:111-114
{
  const r1 = decideCase("decide_reject_toc_filename", { sourceName: "prtugCATIAtoc.htm" }, "test/b32doc-filter.test.js:112");
  assertMatchesOldTest(r1.reason, "toc_or_default", "decide() toc filename", "test/b32doc-filter.test.js:112");
  const r2 = decideCase("decide_reject_default_filename", { sourceName: "default.htm" }, "test/b32doc-filter.test.js:113");
  assertMatchesOldTest(r2.reason, "toc_or_default", "decide() default filename", "test/b32doc-filter.test.js:113");
}
decideCase("decide_accept_normal_filename", { sourceName: "normal_page.htm" }, "追加ケース(brief要求: 各ルールにaccept/reject両方)");

// --- ルール3: not_html_category ---
// 転記元: test/b32doc-filter.test.js:116-118
{
  const r = decideCase("decide_reject_not_html_category", { category: "text" }, "test/b32doc-filter.test.js:117");
  assertMatchesOldTest(r.reason, "not_html_category", "decide() non-html category", "test/b32doc-filter.test.js:117");
}
decideCase("decide_accept_html_category", { category: "html" }, "追加ケース(brief要求: 各ルールにaccept/reject両方。baselineと同一だが明示のため再掲)");

// --- ルール4: language_excluded (ja/mixed は accept, それ以外は reject) ---
// 転記元: test/b32doc-filter.test.js:120-123
{
  const r1 = decideCase("decide_reject_language_en", { language: "en" }, "test/b32doc-filter.test.js:121");
  assertMatchesOldTest(r1.reason, "language_excluded", "decide() language=en", "test/b32doc-filter.test.js:121");
  const r2 = decideCase("decide_accept_language_mixed", { language: "mixed" }, "test/b32doc-filter.test.js:122");
  assertMatchesOldTest(r2.indexable, true, "decide() language=mixed is accepted", "test/b32doc-filter.test.js:122");
}
decideCase("decide_accept_language_ja", { language: "ja" }, "追加ケース(baselineと同一だが明示のため再掲)");

// --- ルール5: empty_content ---
// 転記元: test/b32doc-filter.test.js:146-149
{
  const body = CRLF(["## Extracted Content", "", "(抽出されたコンテンツなし)"]);
  const r = decideCase("decide_reject_empty_content_marker", { body }, "test/b32doc-filter.test.js:147-148");
  assertMatchesOldTest(r.reason, "empty_content", "decide() empty content marker", "test/b32doc-filter.test.js:148");
}
{
  // Extracted Content セクション自体が無い場合も empty_content(空文字列 === '' で判定に落ちる)
  const body = "## Summary Keys\r\n- タイトル: x\r\n";
  const r = decideCase("decide_reject_no_extracted_content_section", { body }, "追加ケース(Extracted Content セクション自体が無い場合。空文字列と同じ扱いになることの確認)");
  assertMatchesOldTest(r.reason, "empty_content", "decide() missing Extracted Content section", "b32doc-filter.js:118-121");
}

// --- ルール6: too_short (境界: MIN_CONTENT_CHARS-1 は reject, ちょうど MIN_CONTENT_CHARS は accept) ---
// 転記元: test/b32doc-filter.test.js:125-131
{
  const short = CRLF(["## Extracted Content", "", "あ".repeat(MIN_CONTENT_CHARS - 1)]);
  const r1 = decideCase("decide_reject_too_short_boundary_minus_1", { body: short }, "test/b32doc-filter.test.js:126-127");
  assertMatchesOldTest(r1.reason, "too_short", "decide() too_short boundary-1", "test/b32doc-filter.test.js:127");

  const enough = CRLF(["## Extracted Content", "", "あ".repeat(MIN_CONTENT_CHARS)]);
  const r2 = decideCase("decide_accept_too_short_boundary_exact", { body: enough }, "test/b32doc-filter.test.js:129-130");
  assertMatchesOldTest(r2.indexable, true, "decide() exact MIN_CONTENT_CHARS is accepted", "test/b32doc-filter.test.js:130");
}
{
  // 転記元: test/b32doc-filter.test.js:133-144 (Summary Keys が長くても Extracted Content が短ければ除外 — 長さ判定の対象を確認する)
  const body = CRLF([
    "## Summary Keys",
    "",
    `- 概要: ${"あ".repeat(500)}`,
    "",
    "## Extracted Content",
    "",
    "短い",
  ]);
  const r = decideCase("decide_reject_too_short_despite_long_summary_keys", { body }, "test/b32doc-filter.test.js:133-144");
  assertMatchesOldTest(r.reason, "too_short", "decide() length judged on Extracted Content only, not Summary Keys", "test/b32doc-filter.test.js:143");
}

// ===========================================================================
// 3. JS/Python 優先順位の食い違い(発見的差異) — filters.py を実行して確認する
// ===========================================================================
//
// 複数のルールに同時に違反する入力を用意し、JS の decideIndexable() が返す
// 理由と、旧リポジトリの filters.py を実際に実行して得られる理由(2種類:
// filters.decide() 関数自体の優先順位 と、build_index.py が実際に実行する
// 個別呼び出しの優先順位)を突き合わせる。
{
  const multiViolationInput = {
    relPosix: "/knowledge/B32doc/images/a.md",
    sourceName: "a.htm",
    category: "text",
    language: "ja",
    body: SAMPLE,
  };
  const jsResult = decideIndexable(multiViolationInput);
  assertMatchesOldTest(jsResult.reason, "path_excluded", "decideIndexable() multi-violation ordering", "b32doc-filter.js:102-113 (path checked before category)");

  // 旧リポジトリの filters.py を実際に実行して2種類の優先順位を取得する(推測しない)。
  // read-only: filters.py を import して呼び出すだけで、旧リポジトリには一切書き込まない。
  const pyScript = `
import sys, json
sys.path.insert(0, r"${join(ROOT, "tools", "knowledge-curator").replace(/\\/g, "\\\\")}")
import filters

meta = {"category": "text", "language": "ja", "source_name": "a.htm"}
extracted = "x" * 300
rel_posix = "/knowledge/B32doc/images/a.md"

decide_result = filters.decide(meta, extracted, rel_posix)

def build_index_actual_order(meta, extracted, rel_posix, disk_filename):
    excluded, reason = filters.is_excluded_path(rel_posix)
    if excluded:
        return reason
    noisy, reason = filters.is_noise_filename(disk_filename)
    if noisy:
        return reason
    if not filters.is_indexable_category(meta.get("category", "")):
        return "not_html_category"
    if not filters.is_indexable_language(meta.get("language", "")):
        return "language_excluded"
    ok, reason = filters.has_real_content(extracted)
    if not ok:
        return reason
    return None

actual_reason = build_index_actual_order(meta, extracted, rel_posix, "a.htm.htm.hash.md")

# ノイズファイル名判定の情報源の食い違い(frontmatter source_name vs ディスク上のファイル名)
fm_source_name = "CustomToc.htm"
disk_name = "renamed_page.htm.htm.abcdef123.md"
noise_via_source_name = filters.is_noise_filename(fm_source_name)
noise_via_disk_name = filters.is_noise_filename(disk_name)

print(json.dumps({
    "filters_decide_reason": decide_result.reason,
    "build_index_actual_order_reason": actual_reason,
    "noise_check_source_name_input": fm_source_name,
    "noise_check_source_name_result": {"noisy": noise_via_source_name[0], "reason": noise_via_source_name[1]},
    "noise_check_disk_name_input": disk_name,
    "noise_check_disk_name_result": {"noisy": noise_via_disk_name[0], "reason": noise_via_disk_name[1]},
}))
`;
  const pyOutRaw = execFileSync("uv", ["run", "python", "-c", pyScript], {
    encoding: "utf8",
    cwd: join(dirname(fileURLToPath(import.meta.url)), "..", "..", ".."),
  });
  const pyOut = JSON.parse(pyOutRaw.trim().split("\n").pop());

  cases.push({
    id: "js_vs_python_priority_order_divergence",
    input: multiViolationInput,
    expected: {
      js_decideIndexable_reason: jsResult.reason,
      python_filters_decide_reason: pyOut.filters_decide_reason,
      python_build_index_actual_order_reason: pyOut.build_index_actual_order_reason,
      divergence: {
        js_vs_filters_decide: jsResult.reason !== pyOut.filters_decide_reason,
        js_vs_build_index_actual: jsResult.reason !== pyOut.build_index_actual_order_reason,
      },
      note:
        "JS の decideIndexable() は path_excluded を最初に判定する(b32doc-filter.js:102-113の実装順)。" +
        "旧リポジトリの filters.py が持つ decide() 関数(build_index.pyからは一度も呼ばれない死んだコード)は" +
        "not_html_category を最初に判定する優先順位を持ち、この入力に対してJSと異なる理由を返す。" +
        "一方、実際に procedures-index.jsonl を生成する build_index.py 本体の呼び出し順序" +
        "(is_excluded_path → is_noise_filename → is_indexable_category → is_indexable_language → has_real_content)は" +
        "JSのdecideIndexable()と一致する。つまりJSは『実際に実行されるPython挙動』とは一致し、" +
        "『filters.py内の未使用のdecide()関数』とは一致しない。",
    },
  });

  cases.push({
    id: "js_vs_python_noise_filename_source_divergence",
    input: {
      fm_source_name: pyOut.noise_check_source_name_input,
      disk_name: pyOut.noise_check_disk_name_input,
    },
    expected: {
      python_noise_via_frontmatter_source_name: pyOut.noise_check_source_name_result,
      python_noise_via_disk_filename: pyOut.noise_check_disk_name_result,
      note:
        "JSのdecideIndexable()はtoc/defaultファイル名判定に frontmatter の source_name を優先し" +
        "(無ければpath.basename(relPosix)にfallback)、filters.pyのdecide()関数も" +
        "meta.get('source_name','')を見る点でJSと一致する。しかし実際にbuild_index.pyが呼ぶのは" +
        "filters.is_noise_filename(md_path.name)であり、frontmatterのsource_nameではなく" +
        "ディスク上の実ファイル名だけを見る。この例(fm_source_name='CustomToc.htm'は" +
        "noisy、対応するdisk_name='renamed_page.htm.htm.abcdef123.md'はnoisyでない)は、" +
        "frontmatterのsource_nameとディスクファイル名が食い違えば判定も食い違うことを示す" +
        "(実データでは通常両者は一致するため、この食い違いが実害を持つケースは希少と推測されるが、" +
        "M2はどちらの情報源を使うかを意識的に選ぶ必要がある)。",
    },
  });
}

// ===========================================================================
// 4. 実データ: docs/knowledge/B32doc からの実文書サンプル
// ===========================================================================
const MAX_BYTES = 64 * 1024;
const REAL_SAMPLE_TARGET = 8;

function walkMd(dir, out) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      walkMd(full, out);
    } else if (entry.isFile() && entry.name.toLowerCase().endsWith(".md")) {
      out.push(full);
    }
  }
}

const b32docRoot = join(DOCS_DIR, "knowledge", "B32doc");
const allMdFiles = [];
walkMd(b32docRoot, allMdFiles);
allMdFiles.sort(); // 決定的な辞書順(パス文字列比較)

const { parseFrontmatter } = await import(pathToFileURL(join(ROOT, "tools/lib/frontmatter.js")).href);

const realDocCases = [];
let skippedOver64kb = 0;
for (const full of allMdFiles) {
  if (realDocCases.length >= REAL_SAMPLE_TARGET) break;
  const size = statSync(full).size;
  if (size > MAX_BYTES) {
    skippedOver64kb++;
    continue;
  }
  const content = readFileSync(full, "utf8");
  const relFromDocs = "/" + relative(DOCS_DIR, full).split("\\").join("/");
  const { data, body } = parseFrontmatter(content);
  const decision = decideIndexable({
    relPosix: relFromDocs,
    sourceName: data.source_name,
    category: data.category,
    language: data.language,
    body,
  });
  const { title: summaryTitle } = extractSummaryKeys(body);

  realDocCases.push({
    id: `real_doc_${String(realDocCases.length).padStart(2, "0")}`,
    path: relFromDocs,
    byte_size: size,
    frontmatter_category: data.category ?? null,
    frontmatter_language: data.language ?? null,
    frontmatter_source_name: data.source_name ?? null,
    content_b64: b64(content),
    expected: {
      decision,
      summaryTitle,
      resolvedTitle: summaryTitle || (typeof data.title === "string" ? data.title : full.split(/[\\/]/).pop().replace(/\.md$/i, "")),
    },
  });
}

if (realDocCases.length < 5) {
  throw new Error(
    `実データサンプルが5件に満たない(${realDocCases.length}件)。母集団または64KB除外条件を見直すこと。`
  );
}

// ---------------------------------------------------------------------------
// 4b. 実データ: accept 側(indexable: true)の実文書サンプル
//
// 上の realDocCases は「path-sorted で先頭から REAL_SAMPLE_TARGET 件」を機械的に
// 拾ったところ、たまたま全件 reject(制御ファイル・非html/非ja)だった
// (レビュー指摘: b32doc-filter capture 完了報告)。accept 経路を実データで
// 端から端まで確認する real doc が無いままでは、decideIndexable の accept 分岐が
// 実データに対しても機能することを保証できない。そのため、同じ母集団
// (docs/knowledge/B32doc、path-sorted、64KB超は除外)を今度は decision.indexable
// === true になる文書だけを対象に、決定的に(先頭から)REAL_ACCEPT_TARGET 件集める。
// ---------------------------------------------------------------------------
const REAL_ACCEPT_TARGET = 3;
const realAcceptCases = [];
let skippedOver64kbForAccept = 0;

for (const full of allMdFiles) {
  if (realAcceptCases.length >= REAL_ACCEPT_TARGET) break;
  const size = statSync(full).size;
  if (size > MAX_BYTES) {
    skippedOver64kbForAccept++;
    continue;
  }
  const content = readFileSync(full, "utf8");
  const relFromDocs = "/" + relative(DOCS_DIR, full).split("\\").join("/");
  const { data, body } = parseFrontmatter(content);
  const decision = decideIndexable({
    relPosix: relFromDocs,
    sourceName: data.source_name,
    category: data.category,
    language: data.language,
    body,
  });
  if (!decision.indexable) continue; // accept のみを集める(reject は既に realDocCases 側で網羅済み)

  const { title: summaryTitle } = extractSummaryKeys(body);
  realAcceptCases.push({
    id: `real_doc_accept_${String(realAcceptCases.length).padStart(2, "0")}`,
    path: relFromDocs,
    byte_size: size,
    frontmatter_category: data.category ?? null,
    frontmatter_language: data.language ?? null,
    frontmatter_source_name: data.source_name ?? null,
    content_b64: b64(content),
    expected: {
      decision,
      summaryTitle,
      resolvedTitle: summaryTitle || (typeof data.title === "string" ? data.title : full.split(/[\\/]/).pop().replace(/\.md$/i, "")),
    },
  });
}

if (realAcceptCases.length < 2) {
  throw new Error(
    `real_doc_accept サンプルが2件に満たない(${realAcceptCases.length}件)。母集団の accept 分布を見直すこと。`
  );
}

// ===========================================================================
// 実行
// ===========================================================================
writeDeterministicJson(join(OUT_DIR, "b32doc-filter.json"), {
  schema: 1,
  source: "tools/lib/b32doc-filter.js",
  authority_note:
    "tools/lib/b32doc-filter.js は tools/knowledge-curator/filters.py の移植と自称し、" +
    "食い違えば filters.py を正とすることが明記されている。実際に検証した結果、" +
    "filters.py内のdecide()関数(優先順位を1つにまとめた関数)はbuild_index.pyから" +
    "一度も呼ばれない死んだコードであり、実際にインデックスを生成するbuild_index.py本体は" +
    "decide()とは異なる優先順位で個々の判定関数を呼んでいる。b32doc-filter.jsの" +
    "decideIndexable()はbuild_index.py本体の実際の優先順位(path_excluded最優先)と一致し、" +
    "decide()の優先順位(not_html_category最優先)とは一致しない。詳細は" +
    "case id 'js_vs_python_priority_order_divergence' と " +
    "'js_vs_python_noise_filename_source_divergence' を参照。M2はb32doc-filter.js" +
    "(=このfixtureのdecideIndexableケース群)の優先順位を移植すること" +
    "(build_index.py本体の実際の挙動と一致するため)。",
  real_docs_sample: {
    root: "docs/knowledge/B32doc",
    selection: "path-sorted(辞書順)決定的選定。乱数不使用",
    target_count: REAL_SAMPLE_TARGET,
    selected_count: realDocCases.length,
    max_bytes: MAX_BYTES,
    skipped_over_64kb_count: skippedOver64kb,
    note:
      "この REAL_SAMPLE_TARGET 件は「先頭から機械的に拾う」選定のため、たまたま全件 reject" +
      "(制御ファイル・非html/非ja)だった。accept 側の実データ確認は real_docs_accept_sample" +
      "(cases 内の real_doc_accept_* )を参照。",
  },
  real_docs_accept_sample: {
    root: "docs/knowledge/B32doc",
    selection: "path-sorted(辞書順)で decision.indexable===true になる文書のみを決定的に収集。乱数不使用",
    target_count: REAL_ACCEPT_TARGET,
    selected_count: realAcceptCases.length,
    max_bytes: MAX_BYTES,
    skipped_over_64kb_count: skippedOver64kbForAccept,
    note:
      "real_docs_sample が全件 reject だったギャップを埋めるための追加採取。" +
      "accept 経路(decision.indexable===true)を実データで確認する。",
  },
  cases: [...cases, ...realDocCases, ...realAcceptCases],
});

assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

console.log(
  `capture-b32doc-filter.mjs: OK (tests/fixtures/kernel/b32doc-filter.json, ${cases.length + realDocCases.length + realAcceptCases.length} cases, ` +
    `${realDocCases.length} real docs (reject-heavy sample) + ${realAcceptCases.length} real docs (accept sample, ${skippedOver64kbForAccept} skipped >64KB), ` +
    `${skippedOver64kb} skipped >64KB for main sample)`
);
