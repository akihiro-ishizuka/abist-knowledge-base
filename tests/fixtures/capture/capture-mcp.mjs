#!/usr/bin/env node
// tests/fixtures/capture/capture-mcp.mjs
//
// M1 Task 3: MCP 契約 fixture 採取(15ツール)。
//
// 3つの kb-* MCP stdio サーバーを旧リポジトリ内で子プロセスとして起動し、
// JSON-RPC 2.0 を stdin/stdout でやり取りして、生のレスポンスをそのまま記録する。
//
// 【絶対条件】旧リポジトリ (multi-source-knowledge-base) には一切書き込まない。
// このスクリプトは「実行しても副作用が無い」ことが事前に判明しているケースのみを
// 呼び出す（正常系読み取り専用ツール、または zod スキーマ検証・ハンドラ内の
// 早期リターンで実ダウンロード/レンダリング処理に到達する前に弾かれるエラー系）。
// 詳細は tests/fixtures/mcp/README 相当のコメントを各ケース定義に添えている。
//
// 出力:
//   tests/fixtures/mcp/tools-list.json
//   tests/fixtures/mcp/<server>/<tool>/<case>.json

import { spawn, execFileSync } from "node:child_process";
import { cpSync, copyFileSync, mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { assertReadOnly, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する

const ROOT = oldRepoRoot();
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "mcp");

const DEFAULT_TIMEOUT_MS = 30_000;
const SEARCH_FIRST_CALL_TIMEOUT_MS = 60_000; // 埋め込みモデルの初回読み込みを見込む
const DEPS_CHECK_TIMEOUT_MS = 30_000; // python/manim/ffmpeg の実プロセス起動を伴う

// ===========================================================================
// Windows: 実行前後で node.exe の PID 集合を比較し、孤児プロセスが残らないことを
// 確認する（索引 DB を開いたまま残る kb-search サーバーは後続タスクへの
// 変更ハザードになるため）。
// ===========================================================================
function listNodePids() {
  if (process.platform !== "win32") return new Set();
  try {
    const out = execFileSync("tasklist", ["/FI", "IMAGENAME eq node.exe", "/FO", "CSV", "/NH"], {
      encoding: "utf8",
    });
    const pids = new Set();
    for (const line of out.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("INFO:")) continue;
      const cols = trimmed.split('","').map((c) => c.replace(/^"|"$/g, ""));
      if (cols[1]) pids.add(cols[1]);
    }
    return pids;
  } catch {
    return new Set();
  }
}

function killTree(pid) {
  if (process.platform === "win32") {
    try {
      execFileSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
    } catch {
      /* 既に終了している場合など */
    }
  } else {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      /* noop */
    }
  }
}

// ===========================================================================
// kb-search 専用: 実 SQLite 索引を隔離したサンドボックスで動かす
//
// 【重大インシデントへの対処】初回実行で、search_kb/get_document/get_chunk/
// index_status のいずれも「読み取り専用のはず」だったが、実際には
// better-sqlite3 が data/kb-index.sqlite・data/reference-index.sqlite を
// 開閉する過程で mtime を変化させることが assertReadOnly() により実測で
// 検出された(ファイルサイズは前後で完全一致 — 294,846,464 / 217,280,512
// バイトのまま — であり、WAL チェックポイント等によるヘッダの変更カウンタ
// 更新のような「論理内容は不変だがファイルへの書き込みは発生する」挙動と
// 推測されるが、内容が一切変わっていないことを事前ハッシュ無しに証明する
// 手段が無いため、安全側に倒して「実ファイルを一切開かない」方式に切り替えた)。
//
// 対処: kb-search-mcp.js とその依存(tools/lib/*.js)を旧リポジトリ外の
// 使い捨てサンドボックスへコピーし、cwd をサンドボックスにして起動する。
// スクリプト自身の ROOT (= import.meta.url から算出される自分の場所の親)が
// サンドボックスになるため、data/kb-index.sqlite・data/reference-index.sqlite
// は「コピー」を、docs/・node_modules/ は「ジャンクション」(読み取り専用参照。
// kb-search はどちらにも書き込まないことをソース読解と probe 実行で確認済み)
// を使うことで、実ファイルには一切触れずに完全に同じ動作を再現する。
// ジャンクションの後始末は fs.rmSync(recursive) がジャンクションを
// isSymbolicLink() として検出し、リンク先には決して再帰しないことを
// 事前に隔離環境で実証済み(container 配下を丸ごと rmSync しても
// junction 先の real-target は無傷だった)。
// ===========================================================================

function createKbSearchSandbox(root) {
  const sandbox = mkdtempSync(join(tmpdir(), "kb-search-sandbox-"));
  mkdirSync(join(sandbox, "tools"), { recursive: true });
  cpSync(join(root, "tools", "kb-search-mcp.js"), join(sandbox, "tools", "kb-search-mcp.js"));
  cpSync(join(root, "tools", "lib"), join(sandbox, "tools", "lib"), { recursive: true });
  writeFileSync(join(sandbox, "package.json"), JSON.stringify({ type: "module" }) + "\n");

  mkdirSync(join(sandbox, "data"), { recursive: true });
  for (const name of ["kb-index.sqlite", "reference-index.sqlite"]) {
    const src = join(root, "data", name);
    const dst = join(sandbox, "data", name);
    copyFileSync(src, dst);
    const srcSize = statSync(src).size;
    const dstSize = statSync(dst).size;
    if (srcSize !== dstSize) {
      throw new Error(`サンドボックスへのコピーが不完全です: ${name} (src=${srcSize} dst=${dstSize})`);
    }
  }

  // 読み取り専用参照(kb-search は docs/・node_modules/ のどちらにも書き込まない)
  execFileSync("cmd", ["/c", "mklink", "/J", join(sandbox, "node_modules"), join(root, "node_modules")], {
    windowsHide: true,
  });
  execFileSync("cmd", ["/c", "mklink", "/J", join(sandbox, "docs"), join(root, "docs")], { windowsHide: true });

  return sandbox;
}

function destroySandbox(sandbox) {
  if (!sandbox) return;
  // rmSync(recursive) はジャンクションを isSymbolicLink() として検出し、
  // リンク先(旧リポジトリの docs/・node_modules/)へは絶対に再帰しない
  // (このスクリプト冒頭のコメント参照。隔離環境で事前実証済み)。
  rmSync(sandbox, { recursive: true, force: true });
}

// ===========================================================================
// JSON-RPC 2.0 stdio クライアント(最小実装)
// ===========================================================================

class McpClient {
  constructor(serverKey, args, { cwd = ROOT } = {}) {
    this.serverKey = serverKey;
    this.args = args;
    this.cwd = cwd;
    this.nextId = 1;
    this.pending = new Map();
    this.stdoutBuf = "";
    this.stderrFull = "";
    this.stderrOffset = 0;
    this.notificationsSeenOnStdout = []; // stdout に非JSON-RPC混入が無いことの検証用
  }

  start() {
    this.child = spawn("node", this.args, {
      cwd: this.cwd,
      shell: false,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child.stdout.setEncoding("utf8");
    this.child.stderr.setEncoding("utf8");
    this.child.stdout.on("data", (chunk) => this._onStdout(chunk));
    this.child.stderr.on("data", (chunk) => {
      this.stderrFull += chunk;
    });
    this.exitPromise = new Promise((resolve) => {
      this.child.on("exit", (code, signal) => resolve({ code, signal }));
    });
  }

  _onStdout(chunk) {
    this.stdoutBuf += chunk;
    let idx;
    while ((idx = this.stdoutBuf.indexOf("\n")) >= 0) {
      const line = this.stdoutBuf.slice(0, idx);
      this.stdoutBuf = this.stdoutBuf.slice(idx + 1);
      if (!line.trim()) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch (e) {
        // stdout に JSON-RPC 以外の何かが混ざっていた場合は致命的(契約違反)なので
        // 即座に例外にする(隠蔽しない)。
        throw new Error(
          `[${this.serverKey}] stdout に非JSON-RPC行が混入しました: ${JSON.stringify(line)} (${e.message})`
        );
      }
      if (msg && typeof msg === "object" && msg.id !== undefined && this.pending.has(msg.id)) {
        const { resolve } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        resolve({ raw: line, msg });
      }
      // id を持たない(通知)メッセージが来ることは想定していないが、記録だけしておく
      if (msg && typeof msg === "object" && msg.id === undefined) {
        this.notificationsSeenOnStdout.push(line);
      }
    }
  }

  /** stderr のうち、前回 takeStderrDelta() 以降に出力された部分を返す(末尾のみ保持)。 */
  takeStderrDelta(tailChars = 4000) {
    const delta = this.stderrFull.slice(this.stderrOffset);
    this.stderrOffset = this.stderrFull.length;
    return delta.length > tailChars ? delta.slice(-tailChars) : delta;
  }

  /** リクエストを送り、応答(通知の場合は null)を返す。 */
  send(method, params, { hasId = true, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    const id = hasId ? this.nextId++ : undefined;
    const msg = { jsonrpc: "2.0", method, ...(params !== undefined ? { params } : {}), ...(hasId ? { id } : {}) };
    const serialized = JSON.stringify(msg);
    if (!hasId) {
      this.child.stdin.write(serialized + "\n");
      return Promise.resolve({ request: msg, raw: null, msg: null });
    }
    const promise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`[${this.serverKey}] ${method} (id=${id}) がタイムアウトしました(${timeoutMs}ms)`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (result) => {
          clearTimeout(timer);
          resolve({ request: msg, ...result });
        },
      });
    });
    this.child.stdin.write(serialized + "\n");
    return promise;
  }

  async callTool(name, args, opts = {}) {
    return this.send("tools/call", { name, arguments: args }, opts);
  }

  /** stdin を閉じて終了を待つ。応答が無ければプロセスツリーを強制終了する。 */
  async shutdown() {
    try {
      this.child.stdin.end();
    } catch {
      /* noop */
    }
    const result = await Promise.race([
      this.exitPromise,
      new Promise((resolve) => setTimeout(() => resolve({ timedOut: true }), 5000)),
    ]);
    if (result?.timedOut) {
      killTree(this.child.pid);
      await Promise.race([this.exitPromise, new Promise((resolve) => setTimeout(resolve, 5000))]);
    }
    return result;
  }
}

// ===========================================================================
// 決定性チェック用の再帰 diff
//
// JSON-RPC の result(または error)を比較する。content[].text が JSON文字列として
// パースできる場合は、その中身まで再帰的に diff する(elapsedMs 等の実測値の
// 揺れを検出するのはここ)。パス表記は Python 側 test_mcp_fixture_coverage.py の
// 実装とキー順序(ソート済み)・配列添字表記([i])を完全に一致させること
// (このJS版とPython版が同じアルゴリズムであることが「宣言した
// nondeterministic_fields が run1/run2 間で実際に異なる唯一のフィールドである」
// という主張の根拠になるため)。
// ===========================================================================

function classifyType(v) {
  if (v === null || v === undefined) return "null";
  if (Array.isArray(v)) return "array";
  return typeof v;
}

function comparableView(resultOrError) {
  if (!resultOrError || typeof resultOrError !== "object") return resultOrError;
  const clone = JSON.parse(JSON.stringify(resultOrError));
  if (Array.isArray(clone.content)) {
    clone.content = clone.content.map((item) => {
      if (item && item.type === "text" && typeof item.text === "string") {
        try {
          return { ...item, text: JSON.parse(item.text) };
        } catch {
          return item;
        }
      }
      return item;
    });
  }
  return clone;
}

function diffPaths(a, b, path, out) {
  if (a === b) return;
  const ta = classifyType(a);
  const tb = classifyType(b);
  if (ta !== tb) {
    out.push(path || "$");
    return;
  }
  if (ta === "null") return; // 両方 null/undefined 相当
  if (ta === "array") {
    const len = Math.max(a.length, b.length);
    for (let i = 0; i < len; i++) diffPaths(a[i], b[i], `${path}[${i}]`, out);
    return;
  }
  if (ta === "object") {
    const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
    for (const k of keys) diffPaths(a[k], b[k], path ? `${path}.${k}` : k, out);
    return;
  }
  out.push(path || "$");
}

function computeNondeterministicFields(response1, response2) {
  const payload1 = response1.result ?? response1.error ?? null;
  const payload2 = response2.result ?? response2.error ?? null;
  const out = [];
  diffPaths(comparableView(payload1), comparableView(payload2), "", out);
  return out;
}

// ===========================================================================
// 1ケース実行: tools/call を2回実行し、run1 を fixture として、run1/run2間の
// 差分パスを nondeterministic_fields として記録する。
// ===========================================================================

async function runCase(client, { server, tool, id, args, timeoutMs, notes, deviation, executionEnvironment }) {
  const run1 = await client.callTool(tool, args, { timeoutMs });
  const stderr1 = client.takeStderrDelta();
  const run2 = await client.callTool(tool, args, { timeoutMs });
  const stderr2 = client.takeStderrDelta();

  if (!run1.msg || !run2.msg) {
    throw new Error(`[${server}/${tool}/${id}] レスポンスを受信できませんでした`);
  }

  const result1 = run1.msg.result;
  const isErrorPresent = !!result1 && Object.prototype.hasOwnProperty.call(result1, "isError");
  const isErrorValue = isErrorPresent ? result1.isError : null;

  // content[0].text の形状を判別する:
  //   - "tool_result_json": ツール自身の JSON エラー/成功応答(content[0].text が JSON としてパース可能)
  //   - "sdk_validation_error": zod スキーマ検証失敗を McpServer が横取りして返す平文プロース
  //     (例: "MCP error -32602: Input validation error: ..."。JSON としてパースできない)
  let responseKind = "unknown";
  let textParsed = null;
  const content0 = result1?.content?.[0];
  if (content0 && content0.type === "text" && typeof content0.text === "string") {
    try {
      textParsed = JSON.parse(content0.text);
      responseKind = "tool_result_json";
    } catch {
      responseKind = /^MCP error -32602: Input validation error:/.test(content0.text)
        ? "sdk_validation_error"
        : "unparseable_text";
    }
  }

  const nondeterministicFields = computeNondeterministicFields(run1.msg, run2.msg);

  const caseDoc = {
    schema: 1,
    server,
    tool,
    case: id,
    notes: notes ?? null,
    deviation_from_brief: deviation ?? null,
    execution_environment: executionEnvironment ?? "live_old_repo",
    request: run1.request,
    response_raw_line: run1.raw,
    response: run1.msg,
    response_kind: responseKind,
    is_error_present: isErrorPresent,
    is_error_value: isErrorValue,
    stderr_tail: stderr1,
    run2: {
      request: run2.request,
      response_raw_line: run2.raw,
      response: run2.msg,
      stderr_tail: stderr2,
    },
    nondeterministic_fields: nondeterministicFields,
  };

  writeDeterministicJson(join(OUT_DIR, server, tool, `${id}.json`), caseDoc);
  return { server, tool, id, responseKind, isErrorValue, nondeterministicFields };
}

function writeSkippedCase({ server, tool, id, skippedReason, toolSchema }) {
  writeDeterministicJson(join(OUT_DIR, server, tool, `${id}.json`), {
    schema: 1,
    server,
    tool,
    case: id,
    skipped_reason: skippedReason,
    tool_schema_from_tools_list: toolSchema ?? null,
  });
}

// ===========================================================================
// サーバー定義とケース表(brief の表 + システム上の安全確認結果に基づく)
// ===========================================================================

const SERVERS = [
  { key: "kb-download", args: ["tools/kb-download-mcp.js"], expectedToolCount: 8 },
  { key: "kb-search", args: ["tools/kb-search-mcp.js"], expectedToolCount: 4 },
  { key: "kb-visualize", args: ["tools/kb-visualize-mcp.js"], expectedToolCount: 3 },
];

// eval/queries.jsonl から実際のクエリを2件転記(Task 4 のベースラインと突き合わせるため)。
// q07: natural(日本語自然文) / q01: identifier(識別子)
const QUERY_JAPANESE_NATURAL = "CATIAの起動時間を短縮するにはどうすればよいか"; // eval/queries.jsonl:7 (q07)
const QUERY_IDENTIFIER = "shrink_clamp_bellow_overlap_mm"; // eval/queries.jsonl:1 (q01)

// render_scene の SOURCE_HASH_MISMATCH 用: 実在する docs/ 配下のファイルを参照するが、
// content_hash はわざと不一致にする(読み取りのみ・レンダリングには到達しない)。
const HASH_MISMATCH_SOURCE_PATH = "設計効率化メモ/設計効率化/メモ/CATIAマクロ/Factory系オブジェクト仕様.md";

function caseTable() {
  return {
    "kb-download": [
      { tool: "list_batches", id: "all_batches", args: {}, notes: "正常系(全バッチ一覧)。読み取り専用。" },
      {
        tool: "list_batches",
        id: "all_batches_repeat",
        args: {},
        notes: "引数を取らないツールのため、呼び出し間の冪等性を確認する目的で2件目とした。",
      },
      {
        tool: "run_batch",
        id: "unknown_batch_name",
        args: { batch: "存在しないバッチ名XYZ_capture用架空バッチ" },
        notes: "batch-config.js に存在しないバッチ名。loadBatchConfigs() で読み込むだけで書き込みは無い。",
      },
      { skip: true, tool: "add_web_batch", id: "schema_only", skippedReason: "batch-config.js を書き換えるため呼び出さない(brief指定)。tools/list のスキーマのみ記録する。" },
      {
        tool: "download_esa_post",
        id: "invalid_post_number_negative",
        args: { post: -1 },
        notes: "post は z.number().int().positive() のため -1 は zod 検証で弾かれ、ハンドラ(=子プロセスspawn)には到達しない。",
        deviation: null,
      },
      {
        tool: "download_esa_category",
        id: "empty_category",
        args: { category: "" },
        notes: "category は z.string().min(1) のため空文字は zod 検証で弾かれ、ハンドラには到達しない。",
      },
      {
        tool: "download_esa_search",
        id: "empty_query",
        args: { query: "" },
        notes: "query は z.string().min(1) のため空文字は zod 検証で弾かれ、ハンドラには到達しない。",
      },
      {
        tool: "download_web",
        id: "invalid_url_format",
        args: { url: "not-a-valid-url" },
        notes:
          "url は z.string().min(1) を通過するが、ハンドラ冒頭の /^https?:\\/\\//i チェックで" +
          "spawn 前に errorResult() を返す(実装を読んで確認済み)。tool_result_json 形式のエラー。",
      },
      {
        tool: "download_git",
        id: "empty_repository_schema_validation",
        args: { repository: "" },
        notes: "repository は z.string().min(1) のため空文字は zod 検証で弾かれる。",
        deviation:
          "brief は「不正リポジトリURLエラー」を指定しているが、download_git のハンドラには " +
          "download_web のような URL 形式チェックが無く、非空文字列を渡すと withLock 内で即座に " +
          "updateGitCache() → git clone が data/git-cache/ 配下へ spawn される(実装を読んで確認済み)。" +
          "不正だが非空の URL(例: 'not-a-repo') を渡した場合、git コマンドの挙動(clone失敗時に " +
          "空ディレクトリを残すか等)がgitのバージョン・状況に依存し、旧リポジトリ絶対不変の制約に対して " +
          "検証しきれないリスクがあると判断した。空文字列は zod の min(1) でハンドラ実行前(spawn前)に " +
          "確実に拒否されるため、副作用ゼロを保証できるこちらを採用した。",
      },
    ],
    "kb-search": [
      {
        tool: "search_kb",
        id: "japanese_natural_query",
        args: { query: QUERY_JAPANESE_NATURAL, limit: 5 },
        timeoutMs: SEARCH_FIRST_CALL_TIMEOUT_MS,
        notes: "eval/queries.jsonl q07(natural)のクエリをそのまま使用。",
      },
      {
        tool: "search_kb",
        id: "identifier_query",
        args: { query: QUERY_IDENTIFIER, limit: 5 },
        notes: "eval/queries.jsonl q01(identifier)のクエリをそのまま使用。",
      },
      {
        tool: "search_kb",
        id: "zero_hit_query",
        args: { query: "PySide6", path_prefix: "this-path-prefix-does-not-exist-anywhere-xyz/", limit: 5 },
        notes: "存在しない path_prefix と組み合わせることで count:0 を確実に再現する(検証済み)。",
      },
      {
        tool: "search_kb",
        id: "filtered_by_source_status_document_type",
        args: { query: "PySide6", source: "esa", status: "active", document_type: "meeting", limit: 5 },
        notes: "source/status/document_type の複数フィルタを同時に指定するケース。",
      },
      {
        tool: "search_kb",
        id: "corpus_reference",
        args: { query: "PySide6", corpus: "reference", limit: 5 },
        notes: "corpus=reference(CATIA原本/B32doc索引)を指定するケース。",
      },
      {
        tool: "get_document",
        id: "normal",
        args: { path: "設計効率化メモ/設計効率化/メモ/CATIAマクロ/Factory系オブジェクト仕様.md" },
        notes: "正常系。行範囲指定なし(全文)。",
      },
      {
        tool: "get_document",
        id: "with_line_range",
        args: { path: "設計効率化メモ/設計効率化/メモ/CATIAマクロ/Factory系オブジェクト仕様.md", start_line: 1, end_line: 5 },
        notes: "行範囲指定あり。range_hash が付与されることを確認する。",
      },
      {
        tool: "get_document",
        id: "nonexistent_path",
        args: { path: "存在しない/パス/nope.md" },
        notes: "存在しないパス。fs.readFileSync の ENOENT を fail() で返す。",
      },
      {
        tool: "get_document",
        id: "path_traversal_attempt",
        args: { path: "../../../../etc/passwd" },
        notes: "パストラバーサル試行。DOCS_DIR 配下チェックで拒否される(読み取りは発生しない)。",
      },
      {
        tool: "get_chunk",
        id: "normal",
        args: { chunk_id: 19985 },
        notes: "search_kb の identifier_query 等で観測された実在 chunk_id。",
      },
      {
        tool: "get_chunk",
        id: "nonexistent_chunk_id",
        args: { chunk_id: 999999999 },
        notes: "存在しない chunk_id。",
      },
      { tool: "index_status", id: "call_1", args: {}, notes: "正常系。引数なし。" },
      { tool: "index_status", id: "call_2", args: {}, notes: "引数を取らないため、呼び出し間の冪等性確認を兼ねた2件目。" },
    ],
    "kb-visualize": [
      { tool: "list_scene_kinds", id: "call_1", args: {}, notes: "正常系。引数なし。" },
      { tool: "list_scene_kinds", id: "call_2", args: {}, notes: "引数を取らないため、呼び出し間の冪等性確認を兼ねた2件目。" },
      {
        tool: "check_visualize_deps",
        id: "call_1",
        args: {},
        timeoutMs: DEPS_CHECK_TIMEOUT_MS,
        notes: "正常系。python/manim/ffmpeg の実プロセスを起動して診断する(読み取り専用)。",
      },
      {
        tool: "check_visualize_deps",
        id: "call_2",
        args: {},
        timeoutMs: DEPS_CHECK_TIMEOUT_MS,
        notes: "呼び出し間の冪等性確認を兼ねた2件目。",
      },
      {
        tool: "render_scene",
        id: "invalid_scene_spec",
        args: { sceneSpec: { foo: "bar" } },
        notes:
          "validateSceneSpec() は純粋関数で fs に一切触れない(renderScene の Step 1、実装で確認済み)。" +
          "スキーマ検証エラーのみで INVALID_SCENE_SPEC を返す。",
      },
      {
        tool: "render_scene",
        id: "source_hash_mismatch",
        args: {
          sceneSpec: {
            schema_version: "1.0",
            scene_kind: "explain",
            output_format: "mp4",
            template: "step_explanation",
            title: "テスト用シーン(fixture採取用、実レンダリングはしない)",
            sources: [
              {
                id: "s1",
                path: HASH_MISMATCH_SOURCE_PATH,
                start_line: 1,
                end_line: 5,
                content_hash: "f".repeat(64),
              },
            ],
            beats: [{ type: "metric", label: "テスト指標", value: "1", source_refs: ["s1"] }],
          },
        },
        notes:
          "verifySources() は docs/ 配下の実ファイルを読み取るのみ(fs書き込み無し、実装で確認済み)。" +
          "content_hash をわざと不一致にすることで SOURCE_HASH_MISMATCH を発生させ、Python 実行(render_scene.py)には到達しない。",
      },
    ],
  };
}

// ===========================================================================
// メイン処理
// ===========================================================================

async function main() {
  const pidsBefore = listNodePids();

  const toolsListResult = {};
  const caseSummaries = [];

  for (const serverDef of SERVERS) {
    // kb-search だけは実 SQLite 索引を開くため、隔離サンドボックスで動かす
    // (このファイル冒頭のインシデント注記を参照)。
    const sandbox = serverDef.key === "kb-search" ? createKbSearchSandbox(ROOT) : null;
    const client = new McpClient(serverDef.key, serverDef.args, { cwd: sandbox ?? ROOT });
    client.start();

    const initRes = await client.send("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "abist-kb-fixture-capture", version: "0.0.1" },
    });
    await client.send("notifications/initialized", undefined, { hasId: false });
    const startupStderr = client.takeStderrDelta(); // [kb-*] MCP server started ... バナー

    const toolsRes = await client.send("tools/list", {});
    const tools = toolsRes.msg.result?.tools ?? [];
    if (tools.length !== serverDef.expectedToolCount) {
      throw new Error(
        `[${serverDef.key}] tools/list の件数が想定と異なります: 期待=${serverDef.expectedToolCount} 実際=${tools.length}`
      );
    }

    toolsListResult[serverDef.key] = {
      server_info: initRes.msg.result?.serverInfo ?? null,
      protocol_version: initRes.msg.result?.protocolVersion ?? null,
      startup_stderr: startupStderr,
      tool_count: tools.length,
      tools,
    };

    const table = caseTable()[serverDef.key];
    const toolsByName = Object.fromEntries(tools.map((t) => [t.name, t]));

    for (const def of table) {
      if (def.skip) {
        writeSkippedCase({
          server: serverDef.key,
          tool: def.tool,
          id: def.id,
          skippedReason: def.skippedReason,
          toolSchema: toolsByName[def.tool] ?? null,
        });
        caseSummaries.push({ server: serverDef.key, tool: def.tool, id: def.id, skipped: true });
        continue;
      }
      const summary = await runCase(client, {
        server: serverDef.key,
        tool: def.tool,
        id: def.id,
        args: def.args,
        timeoutMs: def.timeoutMs ?? DEFAULT_TIMEOUT_MS,
        notes: def.notes,
        deviation: def.deviation,
        executionEnvironment: sandbox
          ? "sandbox_copy (data/*.sqlite はコピー、docs/・node_modules/ はジャンクション経由の読み取り専用参照。理由はこのファイル冒頭のインシデント注記を参照)"
          : "live_old_repo",
      });
      caseSummaries.push(summary);
      console.error(
        `  ${serverDef.key}/${def.tool}/${def.id}: ${summary.responseKind} isError=${summary.isErrorValue} nondeterministic=${JSON.stringify(summary.nondeterministicFields)}`
      );
    }

    const shutdownResult = await client.shutdown();
    console.error(`[${serverDef.key}] shutdown: ${JSON.stringify(shutdownResult)}`);
    destroySandbox(sandbox);

    if (client.notificationsSeenOnStdout.length > 0) {
      throw new Error(
        `[${serverDef.key}] stdout に id を持たないメッセージが混入しました(想定外): ${JSON.stringify(client.notificationsSeenOnStdout)}`
      );
    }
  }

  writeDeterministicJson(join(OUT_DIR, "tools-list.json"), {
    schema: 1,
    source: "MCP tools/list (kb-download / kb-search / kb-visualize)",
    servers: toolsListResult,
  });

  const pidsAfter = listNodePids();
  const orphaned = [...pidsAfter].filter((pid) => !pidsBefore.has(pid));
  if (orphaned.length > 0) {
    throw new Error(
      `孤児プロセスの疑いがあります(採取前に無かった node.exe PID が残存): ${orphaned.join(", ")}`
    );
  }
  console.error(`orphan process check: OK (pids before=${pidsBefore.size}, after=${pidsAfter.size})`);

  assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

  console.error(`capture-mcp.mjs: OK (${caseSummaries.length} cases written to tests/fixtures/mcp/)`);
}

await main();
