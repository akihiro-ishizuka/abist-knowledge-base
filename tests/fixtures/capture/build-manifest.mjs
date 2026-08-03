#!/usr/bin/env node
// tests/fixtures/capture/build-manifest.mjs
//
// M1 Task 5 (Part 3): capture-manifest.json の生成。
//
// tests/fixtures/ 配下の全カテゴリ(kernel/, real-docs/, mcp/, eval/, embedding/,
// html/, capture/ スクリプト自身, および tests/fixtures/ 直下のファイル)を
// 集計し、ファイル数・総バイト数・各ファイルの SHA-256、採取日時(git log由来、
// 実行時刻ではない)、Node版数、旧リポジトリのパスと git HEAD、Task 1〜5で
// 判明した除外・逸脱・非決定項目のレジストリを記録する。
//
// このスクリプトは再実行可能(re-runnable)かつ決定的である: 同じリポジトリの
// 状態(コミット履歴・tests/fixtures/の内容)に対して実行すれば、常に同じ
// バイト列を出力する。generated_at のような「実行した時刻」は記録しない
// (brief: 採取日時は git log から取る。壁時計の「今」を埋め込むと再実行の
// たびに出力が変わってしまい、決定性の検証ができなくなるため)。
//
// capture-manifest.json 自身は自分自身の一覧に含めない(自己参照による
// 循環を避けるため。brief で明示的に指示されている)。
//
// 【絶対条件】旧リポジトリ (multi-source-knowledge-base) には一切書き込まない。
// このスクリプトが旧リポジトリに対して行う唯一の操作は `git rev-parse HEAD`
// (完全に読み取り専用のgitコマンド)。

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { assertReadOnly, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する(このスクリプトが行う唯一の旧リポジトリ操作は git rev-parse HEAD)

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURES_DIR = join(REPO_ROOT, "tests", "fixtures");
const MANIFEST_PATH = join(FIXTURES_DIR, "capture-manifest.json");

// ===========================================================================
// 1. 旧リポジトリの情報(パス・git HEAD)。読み取り専用の git rev-parse のみ。
// ===========================================================================

const oldRoot = oldRepoRoot();
let oldRepoHead = null;
let oldRepoHeadError = null;
try {
  oldRepoHead = execFileSync("git", ["-C", oldRoot, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
} catch (error) {
  oldRepoHeadError = error.message;
}

// ===========================================================================
// 2. tests/fixtures/ 配下の全ファイルを列挙する(capture-manifest.json自身は除く)
// ===========================================================================

function walk(dir, results) {
  const entries = readdirSync(dir, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1));
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(full, results);
    } else if (entry.isFile()) {
      if (full === MANIFEST_PATH) continue; // 自己参照を避ける(brief指定)
      results.push(full);
    }
  }
}

const allFiles = [];
walk(FIXTURES_DIR, allFiles);

function sha256File(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function toPosix(relPath) {
  return relPath.split("\\").join("/");
}

// カテゴリ = tests/fixtures/ から見た相対パスの先頭セグメント。
// 直下の単独ファイル(例: .gitattributes)は "root" カテゴリにまとめる。
const byCategory = new Map();
for (const full of allFiles) {
  const rel = toPosix(relative(FIXTURES_DIR, full));
  const segments = rel.split("/");
  const category = segments.length > 1 ? segments[0] : "root";
  if (!byCategory.has(category)) byCategory.set(category, []);
  byCategory.get(category).push({
    path: rel,
    bytes: statSync(full).size,
    sha256: sha256File(full),
  });
}

const categories = {};
for (const [category, files] of [...byCategory.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1))) {
  files.sort((a, b) => (a.path < b.path ? -1 : 1));
  categories[category] = {
    file_count: files.length,
    total_bytes: files.reduce((sum, f) => sum + f.bytes, 0),
    files,
  };
}

// ===========================================================================
// 3. 採取日時(git log由来。壁時計の「今」は使わない)
//
// 各カテゴリに属する全ファイルの git log を個別に引き、最も新しいコミットを
// そのカテゴリの「採取日時」として記録する(カテゴリ名がそのままディレクトリ名に
// なるとは限らない — "root" は tests/fixtures/ 直下の単独ファイルを束ねた
// 合成カテゴリであり、実在するディレクトリパスではないため、ディレクトリ単位の
// git log では引けない)。このリポジトリ(abist-knowledge-base)自身の commit
// 履歴に対して問い合わせる(旧リポジトリではない)。tests/fixtures/embedding・
// tests/fixtures/html・capture-manifest.json自身はこのタスク(Task 5)で
// まさに今コミットされるものなので、manifest生成時点(コミット前)では
// git logが空になる。これは「取得できなかった」のではなく「まだコミットされて
// いない」という正直な状態であり、null + note として記録する(コミット後に
// 本スクリプトを再実行すれば、その時点のHEADの日時が入るようになる)。
// ===========================================================================

function lastCommitFor(relPathFromRepoRoot) {
  let out;
  try {
    out = execFileSync(
      "git",
      ["-C", REPO_ROOT, "log", "-1", "--format=%H%x1f%aI%x1f%s", "--", relPathFromRepoRoot],
      { encoding: "utf8" }
    ).trim();
  } catch {
    return { commit: null, date: null, subject: null, note: "git log の取得に失敗した" };
  }
  if (!out) {
    return {
      commit: null,
      date: null,
      subject: null,
      note: "このパスに対するコミットがまだ無い(manifest生成時点で未コミット。本タスクの成果物である可能性が高い)",
    };
  }
  const [commit, date, subject] = out.split("\x1f");
  return { commit, date, subject };
}

/** カテゴリ内の全ファイルの最終コミットのうち、最も新しいものを返す。 */
function latestCommitAcrossFiles(fileRelPaths) {
  let best = null;
  for (const relPath of fileRelPaths) {
    const info = lastCommitFor(`tests/fixtures/${relPath}`);
    if (info.date && (!best || new Date(info.date) > new Date(best.date))) {
      best = info;
    }
  }
  return (
    best ?? {
      commit: null,
      date: null,
      subject: null,
      note: "このカテゴリの全ファイルがまだ未コミット(manifest生成時点)",
    }
  );
}

const captureDates = {};
for (const [category, info] of Object.entries(categories)) {
  captureDates[category] = latestCommitAcrossFiles(info.files.map((f) => f.path));
}
captureDates["capture-manifest.json (this file)"] = lastCommitFor("tests/fixtures/capture-manifest.json");

// ===========================================================================
// 4. Task 1〜5 で判明した除外・逸脱の登録簿(黙って落とさない)
// ===========================================================================

const exclusionsRegistry = [
  {
    id: "real_docs_secret_pattern_key_value",
    category: "real-docs",
    source_task: "Task 2 (capture-real-docs.mjs)",
    excluded_count: 1,
    description:
      "esa層の候補 'knowledge/memo/設計効率化/CATIAマクロ(Python)/Element-Type取得(VBS経由).md' が" +
      "secret_pattern:key_value に一致し除外された。実際の秘密情報ではなく、Pythonコード中の変数名" +
      "`script_key = f\"...\"` が *_KEY 接尾辞パターンに偶然一致した偽陽性。brief通り" +
      "「1件でも一致したサンプルは全体を除外する」規定に忠実に従った結果。",
    reference: ".superpowers/sdd/M1-fixture-capture/task-2-report.md §3",
  },
  {
    id: "real_docs_64kb_oversize",
    category: "real-docs",
    source_task: "Task 2 (capture-real-docs.mjs)",
    excluded_count: 0,
    population_over_64kb: 46,
    description:
      "母集団3,523件中46件が64KB超だが、層化選定で実際に候補として遭遇したのは0件" +
      "(各層が64KB超に到達する前に目標件数を充足した)。除外ロジック自体は実装・テスト済みで、" +
      "64KB超のファイルが存在すること自体は確認済み。「除外候補まで到達しなかった」だけであり、" +
      "除外の仕組みが機能していないわけではない。",
    reference: ".superpowers/sdd/M1-fixture-capture/task-2-report.md §4",
  },
  {
    id: "mcp_add_web_batch_skipped",
    category: "mcp",
    source_task: "Task 3 (capture-mcp.mjs)",
    excluded_count: 1,
    description:
      "kb-download の add_web_batch は batch-config.js を書き換える副作用があるため呼び出さず、" +
      "tools/list のスキーマのみを skipped_reason 付きで記録した(brief指定の除外)。",
    reference: "tests/fixtures/mcp/kb-download/add_web_batch/schema_only.json",
  },
  {
    id: "mcp_download_git_deviation",
    category: "mcp",
    source_task: "Task 3 (capture-mcp.mjs)",
    excluded_count: 0,
    description:
      "brief は「不正リポジトリURLエラー」ケースを指定していたが、download_git のハンドラには" +
      "URL形式の事前チェックが無く、非空文字列を渡すと実際に git clone が spawn される。" +
      "旧リポジトリ絶対不変の制約に対するリスクを避けるため、空文字列(zodのmin(1)でspawn前に" +
      "確実に拒否される)を採用した。各ケースファイルの deviation_from_brief フィールドに明記済み。",
    reference: "tests/fixtures/mcp/kb-download/download_git/empty_repository_schema_validation.json",
  },
  {
    id: "mcp_sdk_validation_error_caveat",
    category: "mcp",
    source_task: "Task 3 (capture-mcp.mjs)",
    excluded_count: 0,
    affected_cases: 4,
    description:
      "download_esa_post/download_esa_category/download_esa_search/download_git のzodスキーマ" +
      "検証エラー4件は content[0].text が「MCP error -32602: ...」という非JSONのプレーン文字列に" +
      "なる(response_kind: 'sdk_validation_error')。この文言は旧リポジトリの zod +" +
      "@modelcontextprotocol/sdk バージョン固有の生成物であり、Python版(pydantic + MCP Python SDK)" +
      "は必然的に異なる文言を返す。M5の比較器は response_kind + isError + JSON-RPCエラーコード" +
      "'-32602' の一致で判定し、content[0].text の文字列一致で比較してはならない。",
    reference: ".superpowers/sdd/M1-fixture-capture/task-3-report.md §2",
  },
  {
    id: "kb_search_sandbox_copy_execution_environment",
    category: "mcp, eval, embedding",
    source_task: "Task 3 / Task 4 / Task 5",
    excluded_count: 0,
    description:
      "better-sqlite3 で data/kb-index.sqlite・data/reference-index.sqlite を開くだけで mtime が" +
      "変化する(内容・サイズは不変)ことが Task 3 で実測判明した。このため kb-search の MCP ケース" +
      "(Task 3)・検索評価ベースライン(Task 4)・埋め込みゲート素材(Task 5)はいずれも旧リポジトリ外の" +
      "使い捨てサンドボックスへ両DBを『コピー』し、コピーだけを開く方式を採る。旧リポジトリの実DB" +
      "ファイルは一度も直接開いていない。",
    reference:
      ".superpowers/sdd/M1-fixture-capture/task-3-report.md §0, task-4-report.md §1, capture-embeddings.mjs",
  },
  {
    id: "eval_baseline_plain_json_deviation",
    category: "eval",
    source_task: "Task 4 (capture-eval.mjs)",
    excluded_count: 0,
    description:
      "kernel/real-docs/mcp の各フィクスチャは BOM・CRLF を保持する必要がある生テキストを扱うため" +
      "*_b64 で格納するが、baseline.json は検索結果の構造化データ(パス・スコア・行番号等)のみで" +
      "BOM/CRLF を保持すべき生テキストを含まないため、base64化せず素のJSON文字列で記録した。",
    reference: ".superpowers/sdd/M1-fixture-capture/task-4-report.md §7",
  },
  {
    id: "web_and_reference_layers_empty_corrected",
    category: "real-docs",
    source_task: "Task 2 (レビューにより訂正)",
    excluded_count: 0,
    description:
      "real-docs の層化サンプルで web層・reference層が0件になったのは層化クエリの不具合ではない。" +
      "data/sync-state.sqlite は backfill-metadata.js が直近に作った1回限りのバックフィル" +
      "スナップショットであり、明示的include指定が無い場合の defaultExclude が" +
      "['knowledge/B32doc','knowledge/catiadoc','knowledge/generated',...gitOutputDirs] を" +
      "ハードコードしているため、reference文書もweb由来の履歴コンテンツもこのDBには最初から" +
      "登録されない。data/reference-index.sqlite は knowledge/B32doc のみをカバー" +
      "(b32doc-filter.js)し、docs/knowledge/catiadoc(1,313ファイル、frontmatterは" +
      "source:web・2026年1月クロール)と docs/knowledge/generated(4ファイル)はどちらのDBにも" +
      "登録されないメタデータ孤児である。現行 batch-config.js の type:web バッチ" +
      "(catiadoc/C#ATIA)はどちらも出力先ディレクトリがディスク上に存在せず、現行設定の下では" +
      "web バッチは一度も実行されていない。M8移行は docs/ を実ファイルベースで棚卸しする必要がある。",
    reference:
      ".superpowers/sdd/M1-fixture-capture/progress.md 'CORRECTED BY REVIEWER' セクション, task-2-report.md §2",
  },
  {
    id: "reference_index_source_guess_corrected",
    category: "reference-index",
    source_task: "M4 Task1b (capture-reference-index-columns.mjs)",
    excluded_count: 0,
    description:
      "M4 Task1 の select_reference_targets は brief 未指定・fixture 未採取のまま" +
      "reference コーパスの source 列を 'b32doc' と決め打ちしていた。" +
      "data/reference-index.sqlite をサンドボックスコピー経由で実測した結果、" +
      "documents 7,563行全件は source='manual'(b32doc という値はどの列にも存在しない)・" +
      "document_type='reference'・status='active'・post_number=NULL・url=NULL・" +
      "category='html' であることが判明し、実装を source='manual' に修正した。",
    reference: "tests/fixtures/PROVENANCE.md §4, tests/fixtures/reference-index/columns.json",
  },
];

const nondeterministicItems = [
  {
    field: "mcp kb-search/search_kb content[0].text.diagnostics.elapsedMs",
    affected_cases: 5,
    note:
      "実測経過時間(ミリ秒)。run1/run2で値が変わることを確認済みで、各ケースファイルの" +
      "nondeterministic_fields に明示的に宣言されている。他23ケースは完全に決定的。",
    reference: ".superpowers/sdd/M1-fixture-capture/task-3-report.md §3",
  },
  {
    field: "eval capture response timings (embedding model load ms, per-query embed ms, search timings)",
    affected_cases: null,
    note:
      "capture-eval.mjs実行ログ(console.error)にのみ出力し、fixture(baseline.json)自体には" +
      "含めていない(意図的に除外)。3回の採取実行で値は変動したが、決定的であるべきスコア・" +
      "ランキング・Recall@5/MRR/nDCG@10の数値は3回ともSHA-256完全一致を確認済み。",
    reference: ".superpowers/sdd/M1-fixture-capture/task-4-report.md §4",
  },
  {
    field: "kb-index.sqlite / reference-index.sqlite の mtime(better-sqlite3で開くだけで変化)",
    affected_cases: null,
    note:
      "既知の非決定的副作用(内容・サイズは不変)。このため kb_search_sandbox_copy_execution_environment" +
      "(上記exclusions_registry参照)の通り、サンドボックスコピー方式を一貫して採用している。",
    reference: ".superpowers/sdd/M1-fixture-capture/task-3-report.md §0",
  },
];

// ===========================================================================
// 5. 書き出し
// ===========================================================================

writeDeterministicJson(MANIFEST_PATH, {
  schema: 1,
  generated_by: "tests/fixtures/capture/build-manifest.mjs (再実行可能・決定的。再実行手順はcapture/README.md参照)",
  node_version: process.version,
  old_repo: {
    path: oldRoot,
    git_head: oldRepoHead,
    git_head_error: oldRepoHeadError,
  },
  capture_dates: captureDates,
  categories,
  exclusions_registry: exclusionsRegistry,
  nondeterministic_items: nondeterministicItems,
});

assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

const totalFiles = Object.values(categories).reduce((sum, c) => sum + c.file_count, 0);
const totalBytes = Object.values(categories).reduce((sum, c) => sum + c.total_bytes, 0);
console.log(
  `build-manifest.mjs: OK (tests/fixtures/capture-manifest.json, ${totalFiles} files across ${Object.keys(categories).length} categories, ${totalBytes} bytes)`
);
