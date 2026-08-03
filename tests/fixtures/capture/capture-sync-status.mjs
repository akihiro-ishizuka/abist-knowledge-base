#!/usr/bin/env node
// tests/fixtures/capture/capture-sync-status.mjs
//
// M2 追加タスク: sync_status_for マッピング(11 の同期アクション → 5 の sync_status
// 列挙値)のゴールデン採取。
//
// このマッピングは download-article.js の syncStatusFor()、download-web.js の
// downloadPage() 内の無名インラインマッピング、download-git.js の
// recordMarkdownFiles() に実装されているが、いずれも export されていない。
// 「export されていない関数は手で転記しない」という M1 の原則に従い、
// 旧リポジトリの該当ファイルを os.tmpdir() 配下の使い捨てサンドボックスへコピーし、
// savePost() を直接呼ぶ(esa)/ download-web.js・download-git.js を子プロセスとして
// 実行する(web・git)ことで、実際に sync-state DB へ書かれた sync_status を観測する。
//
// 旧リポジトリ本体(docs/・data/・batch-config.js)には一切書き込まない。
// サンドボックス側だけに書き込ませ、旧リポジトリのファイルはコピーのみ・
// node_modules はジャンクション(読み取り専用参照)を使う。
//
// 出力: tests/fixtures/kernel/sync-status-for.json

import { createServer } from "node:http";
import { spawn, execFileSync } from "node:child_process";
import {
  cpSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertReadOnly, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly();

const OLD_ROOT = oldRepoRoot();
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "kernel");
const OUT_FILE = join(OUT_DIR, "sync-status-for.json");

// ===========================================================================
// サンドボックス構築(旧リポジトリはコピーのみ・node_modules はジャンクション)
// ===========================================================================

const SANDBOX = mkdtempSync(join(tmpdir(), "kb-sync-status-"));
const REPO = join(SANDBOX, "repo");
mkdirSync(join(REPO, "tools", "lib"), { recursive: true });
mkdirSync(join(REPO, "docs"), { recursive: true });
mkdirSync(join(REPO, "data"), { recursive: true });

for (const f of ["download-article.js", "download-web.js", "download-git.js", "package.json"]) {
  cpSync(join(OLD_ROOT, f), join(REPO, f));
}
cpSync(join(OLD_ROOT, "tools", "lib"), join(REPO, "tools", "lib"), { recursive: true });
symlinkSync(join(OLD_ROOT, "node_modules"), join(REPO, "node_modules"), "junction");

function cleanupSandbox() {
  try {
    rmSync(join(REPO, "node_modules"), { force: true }); // ジャンクション自体を切り離す(旧リポジトリには影響しない)
  } catch {
    /* ignore */
  }
  try {
    rmSync(SANDBOX, { recursive: true, force: true });
  } catch {
    /* 使い捨てのため失敗しても致命的ではない */
  }
}

async function loadSandbox(rel) {
  return import(pathToFileURL(join(REPO, rel)).href);
}

const DB_PATH = join(REPO, "data", "sync-state.sqlite");

// ===========================================================================
// 1. esa (download-article.js の savePost を直接 import して実行)
// ===========================================================================

async function captureEsa() {
  const articleMod = await loadSandbox("download-article.js");
  const docRecordMod = await loadSandbox("tools/lib/doc-record.js");
  const frontmatterMod = await loadSandbox("tools/lib/frontmatter.js");
  const { savePost } = articleMod;
  const { recordDocument, getRecord } = docRecordMod;
  const { sha256, hashBody } = frontmatterMod;

  const out = {};

  async function run(id, { seedRecord, seedLocalBody, post, options }) {
    const category = id;
    const fullPost = { number: null, name: id, category, updated_at: "2026-07-25T00:00:00+09:00", ...post };
    const relativePath = `${category}/${id}.md`;

    if (seedLocalBody !== undefined) {
      mkdirSync(join(REPO, "docs", category), { recursive: true });
      writeFileSync(join(REPO, "docs", category, `${id}.md`), seedLocalBody, "utf-8");
    }
    if (seedRecord !== undefined) {
      recordDocument({ path: relativePath, ...seedRecord });
    }

    const result = await savePost(fullPost, "docs", options || {});
    const recordAfter = getRecord(relativePath);
    out[id] = {
      action: result.action,
      sync_status_after: recordAfter ? recordAfter.sync_status : null,
    };
  }

  const BODY_A = "本文A";
  const BODY_A_HASH = sha256(BODY_A);
  const LOCAL_UNCHANGED = BODY_A;
  const LOCAL_UNCHANGED_HASH = hashBody(LOCAL_UNCHANGED);
  const BODY_MODIFIED_LOCAL = "本文A(ローカルで手編集済み)";
  const BODY_B_REMOTE_CHANGED = "本文B(取得元で更新された)";

  await run("unchanged", {
    seedRecord: { source_content_hash: BODY_A_HASH, source_updated_at: "2026-07-25T00:00:00+09:00", local_content_hash: LOCAL_UNCHANGED_HASH },
    seedLocalBody: LOCAL_UNCHANGED,
    post: { body_md: BODY_A },
  });
  await run("local_modified", {
    seedRecord: { source_content_hash: BODY_A_HASH, source_updated_at: "2026-07-25T00:00:00+09:00", local_content_hash: LOCAL_UNCHANGED_HASH },
    seedLocalBody: BODY_MODIFIED_LOCAL,
    post: { body_md: BODY_A },
  });
  await run("conflict", {
    seedRecord: { source_content_hash: BODY_A_HASH, source_updated_at: "2026-07-01T00:00:00+09:00", local_content_hash: LOCAL_UNCHANGED_HASH },
    seedLocalBody: BODY_MODIFIED_LOCAL,
    post: { body_md: BODY_B_REMOTE_CHANGED, updated_at: "2026-07-25T00:00:00+09:00" },
  });
  await run("conflict_overwritten", {
    seedRecord: { source_content_hash: BODY_A_HASH, source_updated_at: "2026-07-01T00:00:00+09:00", local_content_hash: LOCAL_UNCHANGED_HASH },
    seedLocalBody: BODY_MODIFIED_LOCAL,
    post: { body_md: BODY_B_REMOTE_CHANGED, updated_at: "2026-07-25T00:00:00+09:00" },
    options: { force: true },
  });
  await run("adopt", {
    seedRecord: undefined,
    seedLocalBody: LOCAL_UNCHANGED,
    post: { body_md: BODY_A },
  });
  await run("unknown_local", {
    seedRecord: { source_content_hash: BODY_A_HASH, source_updated_at: "2026-07-01T00:00:00+09:00", local_content_hash: null },
    seedLocalBody: LOCAL_UNCHANGED,
    post: { body_md: BODY_A },
  });
  await run("create", { seedRecord: undefined, seedLocalBody: undefined, post: { body_md: "新規記事の本文" } });
  await run("update", {
    seedRecord: { source_content_hash: BODY_A_HASH, source_updated_at: "2026-07-01T00:00:00+09:00", local_content_hash: LOCAL_UNCHANGED_HASH },
    seedLocalBody: LOCAL_UNCHANGED,
    post: { body_md: BODY_B_REMOTE_CHANGED, updated_at: "2026-07-25T00:00:00+09:00" },
  });

  return out;
}

// ===========================================================================
// 2. web (download-web.js を子プロセスとして実行し、127.0.0.1 の使い捨て
//    HTTP サーバーへ向ける。外部ネットワークへは一切アクセスしない)
// ===========================================================================

async function captureWeb() {
  const docRecordMod = await loadSandbox("tools/lib/doc-record.js");
  const frontmatterMod = await loadSandbox("tools/lib/frontmatter.js");
  const { recordDocument, getRecord } = docRecordMod;
  const { sha256, hashBody, parseFrontmatter } = frontmatterMod;

  const routes = new Map();
  const HTML_HEADERS = { "content-type": "text/html; charset=utf-8" };
  const BODY_A_HTML = "<html><head><title>t</title></head><body><h1>本文A</h1></body></html>";
  const BODY_B_HTML = "<html><head><title>t</title></head><body><h1>本文B(更新後)</h1></body></html>";

  const route = (caseId, handler) => routes.set(`/${caseId}/index`, handler);
  route("create", () => ({ status: 200, body: BODY_A_HTML, headers: HTML_HEADERS }));
  route("unchanged_decide", () => ({ status: 200, body: BODY_A_HTML, headers: HTML_HEADERS }));
  route("local_modified", () => ({ status: 200, body: BODY_A_HTML, headers: HTML_HEADERS }));
  route("conflict", () => ({ status: 200, body: BODY_B_HTML, headers: HTML_HEADERS }));
  route("conflict_overwritten", () => ({ status: 200, body: BODY_B_HTML, headers: HTML_HEADERS }));
  route("adopt", () => ({ status: 200, body: BODY_A_HTML, headers: HTML_HEADERS }));
  route("unknown_local", () => ({ status: 200, body: BODY_A_HTML, headers: HTML_HEADERS }));
  route("update", () => ({ status: 200, body: BODY_B_HTML, headers: HTML_HEADERS }));
  route("unchanged_304", (req) => {
    if (req.headers["if-none-match"]) return { status: 304, body: "", headers: {} };
    return { status: 200, body: BODY_A_HTML, headers: { ...HTML_HEADERS, etag: '"etag-a"' } };
  });
  route("http_error", () => ({ status: 500, body: "boom", headers: HTML_HEADERS }));

  const server = createServer((req, res) => {
    const handler = routes.get(req.url.split("?")[0]);
    if (!handler) {
      res.writeHead(404);
      res.end("not found");
      return;
    }
    const { status, body, headers } = handler(req);
    res.writeHead(status, headers);
    res.end(body);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;

  function runDownloadWeb(url, { force = false } = {}) {
    return new Promise((resolve, reject) => {
      const args = [join(REPO, "download-web.js"), url, "--output-dir", "docs", "--max-depth", "0", "--delay", "0"];
      if (force) args.push("--force");
      const child = spawn(process.execPath, args, { cwd: REPO, env: { ...process.env, KB_SYNC_STATE_PATH: DB_PATH } });
      let stdout = "";
      child.stdout.on("data", (d) => (stdout += d));
      child.stderr.on("data", () => {});
      child.on("close", (code) => resolve({ code, stdout }));
      child.on("error", reject);
      setTimeout(() => {
        try {
          child.kill();
        } catch {
          /* already exited */
        }
      }, 15000);
    });
  }

  const out = {};
  async function seedAndRun(caseId, { seedRecord, seedLocalMarkdown, force } = {}) {
    const url = `${base}/${caseId}/index`;
    const relativePath = `${caseId}/index.md`;
    if (seedLocalMarkdown !== undefined) {
      mkdirSync(join(REPO, "docs", caseId), { recursive: true });
      writeFileSync(join(REPO, "docs", caseId, "index.md"), seedLocalMarkdown, "utf-8");
    }
    if (seedRecord !== undefined) recordDocument({ path: relativePath, ...seedRecord });
    await runDownloadWeb(url, { force });
    const recordAfter = getRecord(relativePath);
    out[caseId] = { sync_status_after: recordAfter ? recordAfter.sync_status : null };
  }

  await seedAndRun("create");
  const createdMd = readFileSync(join(REPO, "docs", "create", "index.md"), "utf-8");
  const parsed = parseFrontmatter(createdMd);
  // content = frontMatter(末尾 "---\n\n") + markdown なので、body には markdown の前に
  // 空行1つ分の "\n" が含まれる(frontmatter.test.js の WEB_CRLF ケースと同じ挙動)。
  // decideSyncAction が実際にハッシュする生の markdown と揃えるため、その1文字を落とす。
  const bodyOnly = parsed.body.slice(1);
  const bodyHash = hashBody(createdMd);

  await seedAndRun("unchanged_decide", {
    seedRecord: { source_content_hash: sha256(bodyOnly), source_updated_at: null, local_content_hash: bodyHash },
    seedLocalMarkdown: createdMd,
  });
  await seedAndRun("local_modified", {
    seedRecord: { source_content_hash: sha256(bodyOnly), source_updated_at: null, local_content_hash: bodyHash },
    seedLocalMarkdown: createdMd + "\n追記(ローカル編集)\n",
  });
  await seedAndRun("adopt", { seedRecord: undefined, seedLocalMarkdown: createdMd });
  await seedAndRun("unknown_local", {
    seedRecord: { source_content_hash: sha256(bodyOnly), source_updated_at: null, local_content_hash: null },
    seedLocalMarkdown: createdMd,
  });
  await seedAndRun("http_error", {});
  await seedAndRun("unchanged_304", {
    seedRecord: { source_content_hash: sha256(bodyOnly), source_updated_at: null, local_content_hash: bodyHash, etag: '"etag-a"' },
    seedLocalMarkdown: createdMd,
  });
  await seedAndRun("update", {
    seedRecord: { source_content_hash: sha256(bodyOnly), source_updated_at: null, local_content_hash: bodyHash },
    seedLocalMarkdown: createdMd,
  });
  await seedAndRun("conflict", {
    seedRecord: { source_content_hash: sha256(bodyOnly), source_updated_at: null, local_content_hash: bodyHash },
    seedLocalMarkdown: createdMd + "\n追記(ローカル編集)\n",
  });
  await seedAndRun("conflict_overwritten", {
    seedRecord: { source_content_hash: sha256(bodyOnly), source_updated_at: null, local_content_hash: bodyHash },
    seedLocalMarkdown: createdMd + "\n追記(ローカル編集)\n",
    force: true,
  });

  server.close();
  return out;
}

// ===========================================================================
// 3. git (download-git.js を子プロセスとして実行。上流はネットワーク不要の
//    ローカル使い捨て git リポジトリ)
// ===========================================================================

async function captureGit() {
  const docRecordMod = await loadSandbox("tools/lib/doc-record.js");
  const { getRecord } = docRecordMod;

  const upstream = join(SANDBOX, "git-upstream");
  mkdirSync(upstream, { recursive: true });
  const git = (args, cwd) => execFileSync("git", args, { cwd, encoding: "utf-8" });
  git(["init", "-q", "-b", "main"], upstream);
  git(["config", "user.email", "sandbox@example.com"], upstream);
  git(["config", "user.name", "sandbox"], upstream);
  writeFileSync(join(upstream, "a.md"), "# A\n\n本文A\n", "utf-8");
  writeFileSync(join(upstream, "b.md"), "# B\n\n本文B\n", "utf-8");
  git(["add", "."], upstream);
  git(["commit", "-q", "-m", "initial"], upstream);

  function runDownloadGit(repoPath, outputDir) {
    execFileSync(process.execPath, [join(REPO, "download-git.js"), repoPath, "--output-dir", outputDir], {
      cwd: REPO,
      env: { ...process.env, KB_SYNC_STATE_PATH: DB_PATH },
      encoding: "utf-8",
    });
  }

  const out = {};

  runDownloadGit(upstream, "docs/gittest");
  out.create_a = { sync_status_after: getRecord("gittest/a.md")?.sync_status ?? null };
  out.create_b = { sync_status_after: getRecord("gittest/b.md")?.sync_status ?? null };

  writeFileSync(join(upstream, "a.md"), "# A\n\n本文A(更新後)\n", "utf-8");
  execFileSync("git", ["rm", "-q", "b.md"], { cwd: upstream });
  writeFileSync(join(upstream, "c.md"), "# C\n\n本文C\n", "utf-8");
  git(["add", "."], upstream);
  git(["commit", "-q", "-m", "update"], upstream);

  const beforeBUpdated = getRecord("gittest/b.md")?.updated_at ?? null;
  runDownloadGit(upstream, "docs/gittest");
  out.update_a = { sync_status_after: getRecord("gittest/a.md")?.sync_status ?? null };
  out.create_c = { sync_status_after: getRecord("gittest/c.md")?.sync_status ?? null };
  const afterB = getRecord("gittest/b.md");
  out.missing_deleted_upstream_b = {
    sync_status_after: afterB ? afterB.sync_status : "ROW_ABSENT",
    row_untouched: afterB ? afterB.updated_at === beforeBUpdated : null,
  };

  // error: 存在しないローカルパスを指定してクローンを失敗させる(ネットワーク不要)
  try {
    runDownloadGit(join(SANDBOX, "no-such-repo-here"), "docs/gittest-err");
  } catch {
    /* 想定どおり非ゼロ終了(git clone 失敗) */
  }
  out.error_no_such_repo = { row_created: getRecord("gittest-err/anything.md") !== null };

  return out;
}

// ===========================================================================
// 実行
// ===========================================================================

let esaResults;
let webResults;
let gitResults;
try {
  esaResults = await captureEsa();
  webResults = await captureWeb();
  gitResults = await captureGit();
} finally {
  cleanupSandbox();
}

function expectSyncStatus(observed) {
  return { sync_status: observed };
}

const fixture = {
  schema: 1,
  source:
    "download-article.js (syncStatusFor, 非export) / download-web.js (downloadPage内の無名インラインマッピング, 非export) / download-git.js (recordMarkdownFiles, 非export)",
  additional_sources: ["tools/lib/sync-planner.js (decideSyncAction / SYNC_ACTIONS)", "tools/lib/metadata-schema.js (SYNC_STATUSES)"],
  note: [
    "M2 実装者が download-article.js / verify-integrity.js を読んで導出した sync_status_for マッピングの正しさを、",
    "実行結果で裏付ける(または覆す)ための capture。3つの private 関数(syncStatusFor / downloadPage内の",
    "インラインマッピング / recordMarkdownFiles)はいずれも export されていないため、手で転記せず、",
    "旧リポジトリの該当ファイルを os.tmpdir() 配下のサンドボックスへコピーし(node_modules はジャンクション、",
    "旧リポジトリ本体は一切変更しない)、savePost()を直接実行(esa)、download-web.js/download-git.jsを",
    "子プロセスとして実行(web・git、web は127.0.0.1の使い捨てHTTPサーバーへ、gitはネットワーク不要の",
    "ローカル使い捨てリポジトリへ向けた)して、実際に sync-state DB へ書かれた sync_status を観測した。",
    "重大な発見: 3ソースは同じマッピング関数を共有していない。esa と web は(別ファイルに重複実装されているが)",
    "同じ入出力を持つ。git は decideSyncAction / SYNC_ACTIONS を一切使わず、記録するすべての Markdown に",
    "無条件で 'synced' を書く(アクションに依存しない)。missing/orphan/error の一部は sync_status を",
    "一切書き換えない(直前の値のまま、または行自体が作られない)。",
  ],
  esa_executed: esaResults,
  web_executed: webResults,
  git_executed: gitResults,
  cases: [
    { id: "esa_create", source_file: "download-article.js", action: "create", derivation: "executed_sandbox", citation: "download-article.js:328-336", expected: expectSyncStatus(esaResults.create.sync_status_after) },
    { id: "esa_update", source_file: "download-article.js", action: "update", derivation: "executed_sandbox", citation: "download-article.js:328-336", expected: expectSyncStatus(esaResults.update.sync_status_after) },
    { id: "esa_unchanged", source_file: "download-article.js", action: "unchanged", derivation: "executed_sandbox", citation: "download-article.js:250-263,290-303", expected: expectSyncStatus(esaResults.unchanged.sync_status_after) },
    { id: "esa_local_modified", source_file: "download-article.js", action: "local_modified", derivation: "executed_sandbox", citation: "download-article.js:257,364", expected: expectSyncStatus(esaResults.local_modified.sync_status_after) },
    { id: "esa_conflict", source_file: "download-article.js", action: "conflict", derivation: "executed_sandbox", citation: "download-article.js:257,363", expected: expectSyncStatus(esaResults.conflict.sync_status_after) },
    { id: "esa_conflict_overwritten", source_file: "download-article.js", action: "conflict_overwritten", derivation: "executed_sandbox", citation: "download-article.js:328-336", expected: expectSyncStatus(esaResults.conflict_overwritten.sync_status_after) },
    { id: "esa_adopt", source_file: "download-article.js", action: "adopt", derivation: "executed_sandbox", citation: "download-article.js:257,362-365", expected: expectSyncStatus(esaResults.adopt.sync_status_after) },
    { id: "esa_unknown_local", source_file: "download-article.js", action: "unknown_local", derivation: "executed_sandbox", citation: "download-article.js:257,362-365", expected: expectSyncStatus(esaResults.unknown_local.sync_status_after) },
    {
      id: "esa_missing_below_threshold_or_individual_fetch_succeeded",
      source_file: "download-article.js",
      action: "missing",
      derivation: "read_only_no_safe_execution_path",
      citation: "download-article.js:445-521 (detectMissingPosts, 非export)",
      note: "detectMissingPosts は非exportで、main() 経由(実 esa API 呼び出し必須)以外の到達経路が無い。missingCount が閾値未満、または個別取得(getPost)成功時は db.upsertDocument が呼ばれず sync_status は変化しない。",
      expected: { sync_status: null, meaning: "変更されない(直前の値を保持)" },
    },
    {
      id: "esa_missing_source_confirmed",
      source_file: "download-article.js",
      action: "missing",
      derivation: "read_only_no_safe_execution_path",
      citation: "download-article.js:505-511",
      note: "全件同期成功+連続不在が閾値到達+個別取得も失敗の3条件が揃った場合のみ db.upsertDocument({sync_status:'source_missing'})。3条件判定自体(decideMissingCandidate)は tests/fixtures/kernel/sync-planner.json の missing_candidate_* 4ケースとして実行済み。",
      expected: { sync_status: "source_missing" },
    },
    {
      id: "esa_orphan",
      source_file: "download-article.js",
      action: "orphan",
      derivation: "read_only_no_safe_execution_path",
      citation: "download-article.js:438-461",
      note: "detectMissingPosts 内のカテゴリ移動分岐。recordSyncResult(summary記録)と任意のfs.rm+db.deleteDocumentのみで、sync_statusを書き換えるdb.upsertDocument呼び出しは無い。",
      expected: { sync_status: null, meaning: "変更されない(削除されるか直前の値のまま)" },
    },
    {
      id: "esa_error",
      source_file: "download-article.js",
      action: "error",
      derivation: "read_only_corroborated_by_web_execution",
      citation: "download-article.js:747",
      note: "main()のcatchはrecordSyncResultのみ。同型のパターン(recordSyncResultのみ、db呼び出し無し)をweb側で実行確認済み(web_error_http_status)。",
      expected: { sync_status: null, meaning: "変更されない(sync_statusを書く呼び出しが無い)" },
    },
    { id: "web_create", source_file: "download-web.js", action: "create", derivation: "executed_sandbox_http", citation: "download-web.js:456", expected: expectSyncStatus(webResults.create.sync_status_after) },
    { id: "web_update", source_file: "download-web.js", action: "update", derivation: "executed_sandbox_http", citation: "download-web.js:456", expected: expectSyncStatus(webResults.update.sync_status_after) },
    { id: "web_unchanged_via_decide_sync_action", source_file: "download-web.js", action: "unchanged", derivation: "executed_sandbox_http", citation: "download-web.js:399-419,478", expected: expectSyncStatus(webResults.unchanged_decide.sync_status_after) },
    {
      id: "web_unchanged_via_http_304",
      source_file: "download-web.js",
      action: "unchanged",
      derivation: "executed_sandbox_http",
      citation: "download-web.js:307-310",
      note: "esa/gitには無いweb固有の経路。304応答時はdecideSyncActionを呼ばずrecordDocument({path,last_checked_at})のみ実行し、sync_statusキー自体を渡さない。実行時に観測された値は直前値の保持であり、304経路がその値を書いたわけではない。",
      expected: { sync_status: null, meaning: `変更されない(直前の値を保持。実行時はたまたま '${webResults.unchanged_304.sync_status_after}')` },
    },
    { id: "web_local_modified", source_file: "download-web.js", action: "local_modified", derivation: "executed_sandbox_http", citation: "download-web.js:409-411", expected: expectSyncStatus(webResults.local_modified.sync_status_after) },
    { id: "web_conflict", source_file: "download-web.js", action: "conflict", derivation: "executed_sandbox_http", citation: "download-web.js:409", expected: expectSyncStatus(webResults.conflict.sync_status_after) },
    { id: "web_conflict_overwritten", source_file: "download-web.js", action: "conflict_overwritten", derivation: "executed_sandbox_http", citation: "download-web.js:456", expected: expectSyncStatus(webResults.conflict_overwritten.sync_status_after) },
    { id: "web_adopt", source_file: "download-web.js", action: "adopt", derivation: "executed_sandbox_http", citation: "download-web.js:399-419", expected: expectSyncStatus(webResults.adopt.sync_status_after) },
    { id: "web_unknown_local", source_file: "download-web.js", action: "unknown_local", derivation: "executed_sandbox_http", citation: "download-web.js:399-419", expected: expectSyncStatus(webResults.unknown_local.sync_status_after) },
    {
      id: "web_error_http_status",
      source_file: "download-web.js",
      action: "error",
      derivation: "executed_sandbox_http",
      citation: "download-web.js:325-341",
      note: `初回アクセス(それまでsync-stateに行が無い状態)でHTTP 500を受けた実行結果、観測されたsync_statusは '${webResults.http_error.sync_status_after}'(スキーマ既定値であり、この経路が書いたのではない)。`,
      expected: { sync_status: null, meaning: "変更されない(recordDocumentはsync_errorのみ更新しsync_statusキーを渡さない)" },
    },
    {
      id: "web_error_network",
      source_file: "download-web.js",
      action: "error",
      derivation: "read_only_no_safe_execution_path",
      citation: "download-web.js:280-299",
      note: "fetch自体が失敗する経路(DNS失敗・接続拒否等)は再現に到達不能ホストへの接続試行とタイムアウト待ちを要する。recordDocumentすら呼ばれない点は同型のweb_error_http_statusで実行確認済みのコード形状から読解のみに留めた。",
      expected: { sync_status: null, meaning: "変更されない(recordDocument自体が呼ばれない)" },
    },
    { id: "git_create", source_file: "download-git.js", action: "create", derivation: "executed_sandbox_local_repo", citation: "download-git.js:552", expected: expectSyncStatus(gitResults.create_a.sync_status_after) },
    { id: "git_update", source_file: "download-git.js", action: "update", derivation: "executed_sandbox_local_repo", citation: "download-git.js:552", expected: expectSyncStatus(gitResults.update_a.sync_status_after) },
    {
      id: "git_missing_deleted_upstream",
      source_file: "download-git.js",
      action: "missing",
      derivation: "executed_sandbox_local_repo",
      citation: "download-git.js:483-487,521-538",
      note: `上流から削除されたファイルの sync-state 行は recordMarkdownFiles の走査対象外(実在するファイルしか見ない)になり残存する。実行確認: row_untouched=${gitResults.missing_deleted_upstream_b.row_untouched}(updated_atが1回目の同期時刻のまま変化していない)。`,
      expected: { sync_status: null, meaning: "変更されない(削除されたファイルの行は放置される)" },
    },
    {
      id: "git_error",
      source_file: "download-git.js",
      action: "error",
      derivation: "executed_sandbox_local_repo",
      citation: "download-git.js:440",
      note: `存在しないローカルパスを指定してclone失敗を再現。row_created=${gitResults.error_no_such_repo.row_created}(recordMarkdownFilesが呼ばれず行自体が作られない)。`,
      expected: { sync_status: null, meaning: "行自体が作られない" },
    },
    {
      id: "git_actions_not_applicable",
      source_file: "download-git.js",
      action: null,
      derivation: "read_only",
      citation: "download-git.js 全体(decideSyncAction を一切呼ばない)",
      note: "git-syncは取得元を丸ごとミラーしローカル編集の有無を判定しない設計のため、local_modified/conflict/conflict_overwritten/adopt/unknown_local/orphanの6アクションはgit-syncの実行結果として発生しない。",
      expected: { sync_status: null, meaning: "該当なし" },
    },
  ],
};

writeDeterministicJson(OUT_FILE, fixture);

assertReadOnly();

console.log("capture-sync-status.mjs: OK (tests/fixtures/kernel/sync-status-for.json written)");
