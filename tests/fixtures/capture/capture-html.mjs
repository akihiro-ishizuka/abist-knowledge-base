#!/usr/bin/env node
// tests/fixtures/capture/capture-html.mjs
//
// M1 Task 5 (Part 2): HTML→Markdown 変換ゴールデン採取。
//
// M3 は旧システムの Web クローラーの HTML→Markdown 変換(download-web.js の
// turndown + cheerio パイプライン)を Python(markdownify 等)で再実装する。
// このフィクスチャは「完全一致の契約」ではなく、実装前に差分の大きさを
// 見積もるための資料である(brief: 完全一致は要求しない)。kernel/ 配下の
// ビット互換ゴールデンとは性質が異なるため、`comparison_policy` フィールドで
// 明示する。
//
// 【設定の転記元】download-web.js:37-43 (TurndownService の options)。
// 追加ルール(addRule)は無い — `grep -n "addRule" download-web.js` で0件を確認済み。
//   { headingStyle: 'atx', codeBlockStyle: 'fenced', bulletListMarker: '-',
//     emDelimiter: '*', strongDelimiter: '**' }
//
// 【前処理の転記元】download-web.js:126-161 (htmlToMarkdown関数)。この関数は
// export されていない内部関数のため import できず、ロジックをそのまま複製する:
//   1. a[href] のうち http始まり・mailto:始まり・#始まりでないものを
//      `new URL(href, baseUrl).href` で絶対URL化する(変換エラーは無視)。
//   2. img[src] のうち http始まり・data:始まりでないものを同様に絶対URL化する。
//   3. `script, style, nav, header, footer, aside, .sidebar, .navigation` を
//      $.html()から削除する(nav/header/footer丸ごと。中身の見出し等も消える)。
//   4. `turndownService.turndown($.html())` — 個別要素ではなく文書全体を変換する。
//
// 実サイトへは一切アクセスしない(brief: 再現性のため合成HTMLを使う)。
// TurndownService・cheerio は旧リポジトリの node_modules から直接 import する
// (読み取りのみ。実行しても旧リポジトリへは一切書き込まない)。
//
// 出力: tests/fixtures/html/turndown-goldens.json

import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertReadOnly, b64, oldRepoRoot, writeDeterministicJson } from "./_shared.mjs";

assertReadOnly(); // 基準点を記録する

const ROOT = oldRepoRoot();
const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "html");

const { default: TurndownService } = await import(
  pathToFileURL(join(ROOT, "node_modules", "turndown", "lib", "turndown.cjs.js")).href
);
const cheerio = await import(pathToFileURL(join(ROOT, "node_modules", "cheerio", "dist", "esm", "index.js")).href);

// 転記元: download-web.js:37-43
const turndownService = new TurndownService({
  headingStyle: "atx",
  codeBlockStyle: "fenced",
  bulletListMarker: "-",
  emDelimiter: "*",
  strongDelimiter: "**",
});

// 転記元: download-web.js:126-161 (非exportのためロジック複製。上記ヘッダコメント参照)
function htmlToMarkdown(html, baseUrl) {
  const $ = cheerio.load(html);

  $("a[href]").each((_, el) => {
    const href = $(el).attr("href");
    if (href && !href.startsWith("http") && !href.startsWith("mailto:") && !href.startsWith("#")) {
      try {
        const absoluteUrl = new URL(href, baseUrl).href;
        $(el).attr("href", absoluteUrl);
      } catch {
        // URL変換エラーは無視(download-web.jsと同じ)
      }
    }
  });

  $("img[src]").each((_, el) => {
    const src = $(el).attr("src");
    if (src && !src.startsWith("http") && !src.startsWith("data:")) {
      try {
        const absoluteUrl = new URL(src, baseUrl).href;
        $(el).attr("src", absoluteUrl);
      } catch {
        // URL変換エラーは無視
      }
    }
  });

  $("script, style, nav, header, footer, aside, .sidebar, .navigation").remove();

  return turndownService.turndown($.html());
}

const BASE_URL = "https://example.com/docs/guide/page.html";

const CASES = [
  {
    id: "headings_atx",
    note: "h1〜h6の見出し。headingStyle:'atx'なのでMarkdownは#の数で表現される。",
    html: "<h1>タイトル</h1><p>本文1</p><h2>章1</h2><p>本文2</p><h3>節1-1</h3><p>本文3</p><h4>小見出し</h4><p>本文4</p><h5>H5見出し</h5><h6>H6見出し</h6>",
  },
  {
    id: "nested_unordered_list",
    note: "2階層の入れ子箇条書き。bulletListMarker:'-'。",
    html: "<ul><li>項目A<ul><li>子A-1</li><li>子A-2</li></ul></li><li>項目B</li></ul>",
  },
  {
    id: "nested_ordered_list",
    note: "2階層の入れ子番号付きリスト。",
    html: "<ol><li>手順1<ol><li>手順1-1</li><li>手順1-2</li></ol></li><li>手順2</li></ol>",
  },
  {
    id: "mixed_list_types",
    note: "箇条書きの中に番号付きリストが入れ子になったケース。",
    html: "<ul><li>箇条書き1</li><li>箇条書き2<ol><li>番号1</li><li>番号2</li></ol></li></ul>",
  },
  {
    id: "table_basic",
    note:
      "基本的なテーブル。旧システムは turndown-plugin-gfm を導入していない" +
      "(package.jsonにもnode_modulesにも存在しないことを確認済み)ため、" +
      "vanilla turndown が table 要素をどう扱うか(GFM形式のパイプテーブルには" +
      "ならない可能性が高い)を実測で記録する — M3実装が markdownify 等で" +
      "GFMテーブルを生成すると、ここが最大の既知の差分になりうる。",
    html: "<table><thead><tr><th>列A</th><th>列B</th></tr></thead><tbody><tr><td>a1</td><td>b1</td></tr><tr><td>a2</td><td>b2</td></tr></tbody></table>",
  },
  {
    id: "code_block_language_js",
    note:
      "pre>code.language-javascript。turndownの既定fencedコードブロックルールは" +
      "子codeのclassから`language-(\\S+)`を抽出する(turndown.cjs.js:191-192、" +
      "プラグイン不要の組み込み挙動)。",
    html: '<pre><code class="language-javascript">function add(a, b) {\n  return a + b;\n}</code></pre>',
  },
  {
    id: "code_block_no_language",
    note: "言語クラスの無いコードブロック。",
    html: "<pre><code>plain text block\nline2</code></pre>",
  },
  {
    id: "inline_code",
    note: "段落内のインラインcode要素(識別子を含む)。",
    html: "<p>変数 <code>shrink_clamp_bellow_overlap_mm</code> を使う。</p>",
  },
  {
    id: "links_relative_and_absolute",
    note:
      "相対リンク(絶対URL化される)・絶対リンク(そのまま)・mailto:(対象外)・" +
      "#アンカー(対象外)の4種を1文書に混在させ、書き換え条件の境界を確認する。",
    html:
      '<p><a href="../reference/other.html">相対リンク</a> / ' +
      '<a href="https://example.com/abs">絶対リンク</a> / ' +
      '<a href="mailto:user@example.com">メールリンク</a> / ' +
      '<a href="#section2">アンカーリンク</a></p>',
  },
  {
    id: "images_relative_and_absolute",
    note: "相対src(絶対URL化)・絶対src(そのまま)・data:URI(対象外)の3種。",
    html:
      '<p><img src="./images/diagram.png" alt="図1">' +
      '<img src="https://example.com/abs.png" alt="図2">' +
      '<img src="data:image/png;base64,AAAA" alt="図3"></p>',
  },
  {
    id: "inline_emphasis_mix",
    note: "strong/em/b/i/s、およびstrong>emの入れ子。emDelimiter:'*'、strongDelimiter:'**'。",
    html: "<p><strong>太字</strong>と<em>斜体</em>と<b>Bタグ</b>と<i>Iタグ</i>と<s>取り消し線</s>の<strong><em>混在</em></strong>。</p>",
  },
  {
    id: "removed_elements",
    note:
      "nav/header/aside/.sidebar/.navigation/footerを丸ごと削除する仕様の確認。" +
      "header内のh1・aside内のテキストも中身ごと消えることを記録する" +
      "(要素単位の削除であり、中身を残して外側タグだけ剥がすのではない)。",
    html:
      "<nav>ナビゲーション</nav><header>ヘッダー内テキスト</header>" +
      "<main><h1>本文タイトル</h1><p>残る本文</p></main>" +
      '<aside>サイドバー</aside><div class="sidebar">旧サイドバー</div>' +
      '<div class="navigation">ナビ2</div><footer>フッター</footer>',
  },
  {
    id: "japanese_fullwidth_punctuation",
    note: "全角句読点・かぎ括弧・中点・長音記号を含む日本語本文。",
    html:
      "<h2>日本語の見出し「テスト」</h2>" +
      "<p>これは、全角句読点・記号(例:「」『』・ー〜)を含む本文です。CATIAの設定は次の通り。</p>",
  },
  {
    id: "definition_list",
    note: "dl/dt/dd構造。turndownに組み込みのdl専用ルールは無く、どう変換されるかを実測で記録する。",
    html: "<dl><dt>用語A</dt><dd>説明A</dd><dt>用語B</dt><dd>説明B1</dd><dd>説明B2</dd></dl>",
  },
  {
    id: "messy_realworld_page",
    note:
      "上記の要素(見出し・入れ子リスト・言語付きコードブロック・テーブル・" +
      "相対リンク・相対画像・除去対象要素・日本語全角記号)を1ページに組み合わせた" +
      "実サイト風の合成HTML。header内のh1・nav/aside/footerの内容は削除仕様により" +
      "消えることを期待する(removed_elementsケースと同じ仕様)。",
    html:
      "<!doctype html><html><head><title>ページタイトル</title></head><body>" +
      "<nav>サイト内ナビ</nav>" +
      "<header><h1>CATIAマクロ設定ガイド</h1></header>" +
      "<main><article>" +
      "<h2>概要</h2>" +
      '<p>この記事は<strong>CATIA</strong>の<em>マクロ設定</em>について説明します。' +
      '詳細は<a href="../reference/detail.html">詳細ページ</a>または' +
      '<a href="https://example.com/spec">仕様書</a>を参照してください。</p>' +
      "<h3>手順</h3>" +
      "<ol><li>準備<ul><li>環境変数を設定</li><li>権限を確認</li></ul></li><li>実行</li></ol>" +
      "<h3>設定例</h3>" +
      '<pre><code class="language-vbs">Sub Main()\n  MsgBox "こんにちは"\nEnd Sub</code></pre>' +
      "<h3>パラメータ表</h3>" +
      "<table><thead><tr><th>名前</th><th>既定値</th></tr></thead>" +
      "<tbody><tr><td>shrink_clamp_bellow_overlap_mm</td><td>0.5</td></tr></tbody></table>" +
      '<p><img src="./images/screenshot.png" alt="スクリーンショット"></p>' +
      "<p>備考: 全角記号(「」・〜)にも対応。</p>" +
      "</article></main>" +
      '<aside class="sidebar">関連リンク</aside>' +
      "<footer>© 2026 Example</footer>" +
      "</body></html>",
  },
];

const cases = CASES.map(({ id, note, html }) => {
  const markdown = htmlToMarkdown(html, BASE_URL);
  return {
    id,
    note,
    base_url: BASE_URL,
    input_html_b64: b64(html),
    output_markdown_b64: b64(markdown),
  };
});

writeDeterministicJson(join(OUT_DIR, "turndown-goldens.json"), {
  schema: 1,
  source: "download-web.js turndownService config (:37-43) + htmlToMarkdown (:126-161, logic reproduced, not exported)",
  comparison_policy:
    "advisory — unlike tests/fixtures/kernel/**(ビット互換の契約)、このフィクスチャは" +
    "M3のPython実装(markdownify等)との差分を事前に把握するための資料であり、" +
    "完全一致は要求しない。差分が見つかった場合は許容できる逸脱か設計レビューで判断する。",
  turndown_options: {
    headingStyle: "atx",
    codeBlockStyle: "fenced",
    bulletListMarker: "-",
    emDelimiter: "*",
    strongDelimiter: "**",
  },
  turndown_addRule_count: 0,
  cases,
});

assertReadOnly(); // 採取後も旧リポジトリが変化していないことを確認する

console.log(`capture-html.mjs: OK (tests/fixtures/html/turndown-goldens.json, ${cases.length} cases)`);
