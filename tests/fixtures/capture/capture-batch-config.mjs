#!/usr/bin/env node
// tests/fixtures/capture/capture-batch-config.mjs
//
// M1 Task 1: batch-config.js の読み書き（formatConfig / loadBatchConfigsFresh）のゴールデン採取。
//
// 実物の batch-config.js を読み、パース結果と re-serialize した文字列が
// 実ファイルとバイト同一かどうかを記録する（M2 のパーサーはこの JSON と一致すればよい）。
// 一致するかどうか自体が知見であり、事前に決め打ちしない。

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertReadOnly, b64, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する

const ROOT = oldRepoRoot();
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "kernel");

const { formatConfig, loadBatchConfigsFresh, saveBatchConfigs } = await import(
  pathToFileURL(join(ROOT, "tools/lib/batch-config-store.js")).href
);

const cases = [];

// ---------------------------------------------------------------------------
// 転記元: test/batch-config-store.test.js:23-39
// esa 配列・web/git オブジェクト・日本語キー・シングルクォート入り名を網羅したサンプル
// ---------------------------------------------------------------------------
const SAMPLE_CONFIG = {
  蛇腹形状の自動設計: ["設計効率化/三桜工業様/蛇腹形状の自動設計", "議事録/設計効率化/三桜工業様定例"],
  "o'reilly-notes": ["メモ/O'Reilly"],
  catiadoc: {
    type: "web",
    url: "http://catiadoc.free.fr/online/interfaces/CAAHomeIdx.htm",
    outputDir: "docs/catiadoc",
    maxDepth: 10,
    delay: 1000,
  },
  "catia-flotherm-prep": {
    type: "git",
    repository: "https://github.com/abist-co-ltd/catia-flotherm-prep",
    branch: "main",
    outputDir: "docs/catia-flotherm-prep",
  },
};

cases.push({
  id: "format_config_sample",
  input: { config: SAMPLE_CONFIG, _citation: "test/batch-config-store.test.js:23-39" },
  expected: { formatted_b64: b64(formatConfig(SAMPLE_CONFIG)) },
});

// UI サーバの正規表現フォールバックでもパースできることの確認
// 転記元: test/batch-config-store.test.js:63-76
{
  const content =
    `#!/usr/bin/env node\n\n` +
    `// バッチダウンロード用の設定ファイル\n` +
    `// 複数のカテゴリパスを配列で定義してください\n\n` +
    `export const batchConfigs = ${formatConfig(SAMPLE_CONFIG)};\n`;
  const match = content.match(/export const batchConfigs = ({[\s\S]*?});/);
  const parsedBack = match ? new Function("return " + match[1])() : null;
  cases.push({
    id: "format_config_regex_fallback_parseable",
    input: { _citation: "test/batch-config-store.test.js:63-76" },
    expected: {
      content_b64: b64(content),
      regexMatched: match !== null,
      parsedBackEqualsOriginal: JSON.stringify(parsedBack) === JSON.stringify(SAMPLE_CONFIG),
    },
  });
}

// ---------------------------------------------------------------------------
// 合成設定: esa(配列) / web / git の3型に加え、formatConfig の分岐を広く踏むための
// 追加ケース（空配列・空オブジェクト・ネスト・特殊文字）。旧テストには無いため
// 実行して値を記録する。
// ---------------------------------------------------------------------------
const SYNTHETIC_CONFIG = {
  "空配列バッチ": [],
  "単純esaバッチ": ["カテゴリA", "カテゴリB/サブ"],
  "quote'in'name": ["path/with'quote"],
  webBatch: {
    type: "web",
    url: "https://example.com/docs/",
    outputDir: "docs/example",
    maxDepth: 5,
    delay: 500,
  },
  webNoOutputDir: {
    type: "web",
    url: "https://example.com/",
    maxDepth: 1,
    delay: 100,
  },
  gitBatch: {
    type: "git",
    repository: "https://github.com/example/repo.git",
    branch: "feature/x",
    outputDir: "docs/repo",
  },
  numericAndBoolLikeStrings: {
    type: "web",
    url: "https://example.com/",
    outputDir: "docs/x",
    maxDepth: 0,
    delay: 0,
  },
};

cases.push({
  id: "format_config_synthetic",
  input: { config: SYNTHETIC_CONFIG },
  expected: { formatted_b64: b64(formatConfig(SYNTHETIC_CONFIG)) },
});

// ---------------------------------------------------------------------------
// 実物の batch-config.js: パース結果と re-serialize のバイト同一性
// 転記元: test/batch-config-store.test.js:51-61
// ---------------------------------------------------------------------------
{
  const realConfigPath = join(ROOT, "batch-config.js");
  const originalContent = readFileSync(realConfigPath, "utf8");
  const loadedConfig = await loadBatchConfigsFresh(realConfigPath);

  // saveBatchConfigs は書き込み先ファイルパスを引数に取るため、
  // 旧リポジトリの外の一時ディレクトリに書き出してから比較する（旧リポジトリは一切変更しない）。
  const tmpDir = mkdtempSync(join(tmpdir(), "kb-batch-config-capture-"));
  const tmpFile = join(tmpDir, "batch-config.js");
  try {
    await saveBatchConfigs(tmpFile, loadedConfig);
    const regenerated = readFileSync(tmpFile, "utf8");
    const matches = regenerated === originalContent;
    let firstDiffIndex = null;
    if (!matches) {
      const len = Math.min(regenerated.length, originalContent.length);
      for (let i = 0; i < len; i++) {
        if (regenerated[i] !== originalContent[i]) {
          firstDiffIndex = i;
          break;
        }
      }
      if (firstDiffIndex === null) firstDiffIndex = len; // 長さ違いで末尾まで一致
    }
    cases.push({
      id: "real_batch_config_roundtrip",
      input: { realConfigPath: "batch-config.js (旧リポジトリ直下)" },
      expected: {
        loadedConfig,
        regeneratedMatchesOriginalByteForByte: matches,
        original_b64: b64(originalContent),
        regenerated_b64: b64(regenerated),
        firstDiffIndex,
      },
    });
  } finally {
    rmSync(tmpDir, { recursive: true, force: true });
  }
}

writeDeterministicJson(join(OUT_DIR, "batch-config.json"), {
  schema: 1,
  source: "tools/lib/batch-config-store.js",
  cases,
});

assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

console.log("capture-batch-config.mjs: OK (tests/fixtures/kernel/batch-config.json)");
