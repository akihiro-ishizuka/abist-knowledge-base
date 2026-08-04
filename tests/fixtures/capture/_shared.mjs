// tests/fixtures/capture/_shared.mjs
//
// M1 フィクスチャ採取スクリプトの共通基盤。
//
// 絶対条件: 旧リポジトリ (multi-source-knowledge-base) には一切書き込まない。
// このモジュールが提供する assertReadOnly() は「書き込んでいないはず」の願望ではなく、
// 実際に旧リポジトリの git status と、docs/・data/ 配下の全ファイルの (size, mtime)
// フィンガープリントを採取前後で比較して変化を検出する自己チェックである。
//
// 注意（レビュー指摘で判明した経緯）: docs/ はほぼ全体、data/ は全体が .gitignore
// 対象のため、git status だけでは既存ファイルの書き換えをほぼ検出できない。
// また「ディレクトリ自体の mtime」は、既存ファイルを同一バイト列で上書きしても
// 変化しない（NTFS はディレクトリエントリの中身書き換えで親ディレクトリの mtime を
// 更新しない）。そのため、docs/・data/ は配下の全ファイルを再帰的に stat し、
// ファイルごとの (size, mtimeMs) を比較する。約 89,000 ファイルの再帰 stat は
// 実測 2秒強で、採取スクリプト1回あたり2回（冒頭・末尾）呼んでも許容範囲。

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

/**
 * 旧リポジトリのルートパスを解決する。
 * 既定値は `C:\Temp\multi-source-knowledge-base`。環境変数 `KB_OLD_REPO` で上書きできる。
 *
 * 存在しない、または期待するファイルが見つからない場合は明示的なエラーで即座に停止する
 * (誤って別のディレクトリを読み書きしてしまうことを避けるため)。
 */
export function oldRepoRoot() {
  const root = resolve(process.env.KB_OLD_REPO || "C:\\Temp\\multi-source-knowledge-base");

  if (!existsSync(root)) {
    throw new Error(
      `旧リポジトリが見つかりません: ${root}\n` +
        "KB_OLD_REPO 環境変数で正しいパスを指定してください。"
    );
  }
  const anchor = join(root, "tools", "lib", "frontmatter.js");
  if (!existsSync(anchor)) {
    throw new Error(
      `旧リポジトリらしき場所ではありません（${anchor} が存在しません）: ${root}`
    );
  }
  const packageJsonPath = join(root, "package.json");
  if (existsSync(packageJsonPath)) {
    const pkg = JSON.parse(readFileSync(packageJsonPath, "utf8"));
    if (pkg.name !== "multi-source-knowledge-base") {
      throw new Error(
        `${packageJsonPath} の name が想定と異なります: "${pkg.name}"\n` +
          "KB_OLD_REPO が正しいリポジトリを指しているか確認してください。"
      );
    }
  }
  return root;
}

/** 文字列を UTF-8 として base64 に変換する（BOM・CRLF・サロゲートペアも無変換で保持する） */
export function b64(str) {
  if (typeof str !== "string") {
    throw new TypeError(`b64() には文字列を渡してください（実際: ${typeof str}）`);
  }
  return Buffer.from(str, "utf8").toString("base64");
}

/**
 * オブジェクトのキーを再帰的にソートしてから JSON.stringify する replacer。
 * ネストしたオブジェクトすべてに適用され、配列の順序はそのまま保つ。
 */
function sortedReplacer(_key, value) {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    return Object.keys(value)
      .sort()
      .reduce((sorted, k) => {
        sorted[k] = value[k];
        return sorted;
      }, {});
  }
  return value;
}

/**
 * 決定的な JSON をファイルに書き出す。
 *
 * - キーは再帰的にソート済み（採取のたびに同じバイト列になる）
 * - 改行は LF 固定、インデントは 2 スペース
 * - 末尾に改行を 1 つ付ける
 * - タイムスタンプや実行環境依存の値を含めてはいけない（呼び出し側の責務）
 */
export function writeDeterministicJson(path, obj) {
  const json = JSON.stringify(obj, sortedReplacer, 2).replace(/\r\n/g, "\n") + "\n";
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, json, { encoding: "utf8" });
}

/** 旧リポジトリの git status --porcelain を取得する（追跡外ファイルも含めた完全な状態） */
function gitStatus(root) {
  try {
    return execFileSync("git", ["-C", root, "status", "--porcelain"], {
      encoding: "utf8",
    });
  } catch (error) {
    throw new Error(`旧リポジトリの git status 取得に失敗しました: ${error.message}`);
  }
}

/** 再帰的にファイルの (size, mtime) フィンガープリントを取る対象ディレクトリ */
const SPOT_CHECK_DIRS = ["docs", "data"];
/** 単体ファイルとして mtime を見る対象（ディレクトリ内を再帰する必要が無いもの） */
const SPOT_CHECK_FILES = ["batch-config.js"];

/** dir 配下のファイルを再帰的に辿り、`relPath -> "size:mtimeMs"` を results に積む */
function walkFingerprint(root, dir, results) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return; // 存在しない・読めない場合は素通り（無いことは無いことで一貫比較できる）
  }
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      walkFingerprint(root, full, results);
    } else if (entry.isFile()) {
      const stat = statSync(full);
      // 比較用キーは旧リポジトリルートからの相対パス（root の位置に依存しない）
      const relKey = full.slice(root.length).replace(/\\/g, "/");
      results.set(relKey, `${stat.size}:${Math.round(stat.mtimeMs)}`);
    }
  }
}

/**
 * docs/・data/ 配下の全ファイルと batch-config.js の (size, mtime) フィンガープリントを集める。
 *
 * ディレクトリ自体の mtime ではなくファイル単位で見るのは、既存ファイルを同一バイト列で
 * 上書きしてもディレクトリの mtime は変化しないため（この関数はその書き換えも検出する）。
 */
function collectFingerprint(root) {
  const results = new Map();
  for (const rel of SPOT_CHECK_DIRS) {
    walkFingerprint(root, join(root, rel), results);
  }
  for (const rel of SPOT_CHECK_FILES) {
    const full = join(root, rel);
    if (existsSync(full)) {
      const stat = statSync(full);
      results.set(rel, `${stat.size}:${Math.round(stat.mtimeMs)}`);
    } else {
      results.set(rel, null);
    }
  }
  return results;
}

/** 2つのフィンガープリント Map を比較し、追加・削除・変更されたキーを返す */
function diffFingerprints(before, after) {
  const added = [];
  const removed = [];
  const changed = [];
  for (const [key, value] of after) {
    if (!before.has(key)) added.push(key);
    else if (before.get(key) !== value) changed.push(key);
  }
  for (const key of before.keys()) {
    if (!after.has(key)) removed.push(key);
  }
  return { added, removed, changed };
}

let _snapshot = null;

/**
 * 旧リポジトリに書き込みが発生していないことを確認する。
 *
 * 各採取スクリプトの冒頭と末尾で呼び出すこと:
 *   - 1回目の呼び出し（冒頭）: git status と docs/・data/ 配下全ファイルの
 *     (size, mtime) フィンガープリント、batch-config.js の mtime を基準点として記録する。
 *   - 2回目の呼び出し（末尾）: 基準点と再度比較し、差異があれば例外を投げて異常終了する。
 *
 * 3回目以降は再度 diff せず新しい基準点として記録し直す（同一プロセス内で複数回
 * 採取処理を行うスクリプトのため）。
 *
 * git status だけでは検出できない変更がある点に注意: docs/ はほぼ全体、data/ は
 * 全体が .gitignore 対象なので、既存ファイルの中身書き換えは git status に出ない。
 * そのため本チェックの本体は git status ではなく、ファイル単位のフィンガープリント比較。
 */
export function assertReadOnly() {
  const root = oldRepoRoot();
  const current = { status: gitStatus(root), fingerprint: collectFingerprint(root) };

  if (_snapshot === null) {
    _snapshot = current;
    return;
  }

  if (current.status !== _snapshot.status) {
    throw new Error(
      "旧リポジトリの git status が採取前後で変化しました。書き込みが発生した可能性があります。\n" +
        `--- 採取前 ---\n${_snapshot.status}\n--- 採取後 ---\n${current.status}`
    );
  }

  const diff = diffFingerprints(_snapshot.fingerprint, current.fingerprint);
  if (diff.added.length > 0 || diff.removed.length > 0 || diff.changed.length > 0) {
    const describe = (label, list) =>
      list.length > 0 ? `${label} (${list.length}件): ${list.slice(0, 20).join(", ")}` : null;
    const lines = [describe("追加", diff.added), describe("削除", diff.removed), describe("変更", diff.changed)].filter(
      Boolean
    );
    throw new Error(
      "旧リポジトリの docs/・data/・batch-config.js が採取前後で変化しました。書き込みが発生した可能性があります。\n" +
        lines.join("\n")
    );
  }
  _snapshot = current;
}
