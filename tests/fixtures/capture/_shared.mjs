// tests/fixtures/capture/_shared.mjs
//
// M1 フィクスチャ採取スクリプトの共通基盤。
//
// 絶対条件: 旧リポジトリ (multi-source-knowledge-base) には一切書き込まない。
// このモジュールが提供する assertReadOnly() は「書き込んでいないはず」の願望ではなく、
// 実際に旧リポジトリの git status と主要ディレクトリの mtime を採取前後で比較して
// 変化を検出する自己チェックである。

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
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

/** スポットチェック対象（docs/ と data/ はディレクトリの mtime、batch-config.js はファイルの mtime） */
const SPOT_CHECK_PATHS = ["docs", "data", "batch-config.js"];

function collectMtimes(root) {
  const mtimes = {};
  for (const rel of SPOT_CHECK_PATHS) {
    const full = join(root, rel);
    if (!existsSync(full)) {
      mtimes[rel] = null;
      continue;
    }
    mtimes[rel] = statSync(full).mtimeMs;
  }
  return mtimes;
}

let _snapshot = null;

/**
 * 旧リポジトリに書き込みが発生していないことを確認する。
 *
 * 各採取スクリプトの冒頭と末尾で呼び出すこと:
 *   - 1回目の呼び出し（冒頭）: git status と主要パスの mtime を記録する（基準点）。
 *   - 2回目の呼び出し（末尾）: 基準点と再度比較し、差異があれば例外を投げて異常終了する。
 *
 * 3回目以降は再度 diff せず新しい基準点として記録し直す（同一プロセス内で複数回
 * 採取処理を行うスクリプトのため）。
 */
export function assertReadOnly() {
  const root = oldRepoRoot();
  const current = { status: gitStatus(root), mtimes: collectMtimes(root) };

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
  for (const rel of SPOT_CHECK_PATHS) {
    if (current.mtimes[rel] !== _snapshot.mtimes[rel]) {
      throw new Error(
        `旧リポジトリの mtime が採取前後で変化しました: ${rel}\n` +
          `採取前: ${_snapshot.mtimes[rel]} / 採取後: ${current.mtimes[rel]}`
      );
    }
  }
  _snapshot = current;
}
