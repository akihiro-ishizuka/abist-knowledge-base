"""HTML→Markdown 変換(旧 `download-web.js` の `htmlToMarkdown`/`extractLinks` の移植)。

**この移植は advisory 契約である**(`tests/fixtures/PROVENANCE.md` §1、
`tests/fixtures/html/turndown-goldens.json` の `comparison_policy`)。旧実装は
Node の `turndown`(GFM プラグイン無し)、この実装は Python の `markdownify`
(BeautifulSoup 上に構築)であり、ライブラリが違う以上ビット互換は要求しない。
差分は golden 15件と1件ずつ突き合わせ、許容可否を `task-4-5-report.md` に記録した。

再現している旧実装の契約:

1. **前処理は変換前に行う**: 相対 `href`/`src` を絶対URL化(`http` 接頭辞・
   `mailto:`・`#`・`data:` は対象外)、`script/style/nav/header/footer/aside/
   .sidebar/.navigation` を要素ごと削除。
2. **GFM 無し**: 旧 `package.json`/`node_modules` に `turndown-plugin-gfm` が
   存在しないことを確認済み(`turndown-goldens.json` の `source` 注記)。
   よって `table`/`dl` はパイプテーブル・コロン付き定義リストにはならず、
   ブロック単位でプレーンテキストへ平坦化される(`_BlockFlatten` 参照)。
3. **リンク抽出は変換後の Markdown からも行える**(304 応答時、本文が返らない
   ため保存済み Markdown からリンクを再抽出してクロールを継続する契約、
   `extract_links_from_markdown` 参照)。
"""

from __future__ import annotations

import contextlib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import ATX, MarkdownConverter

_REMOVE_SELECTOR = "script, style, nav, header, footer, aside, .sidebar, .navigation"
_LANGUAGE_CLASS_RE = re.compile(r"^language-(\S+)$")
_MARKDOWN_LINK_RE = re.compile(r"\]\((https?://[^\s)]+)\)|<(https?://[^\s>]+)>")


def _code_language(el: object) -> str | None:
    """turndown 組み込みルール(`turndown.cjs.js` の fenced code block ルール)と
    同じく、子 `<code class="language-xxx">` から言語名を抽出する。
    """
    code = el.find("code")  # type: ignore[attr-defined]
    if code is None:
        return None
    for cls in code.get("class") or []:
        match = _LANGUAGE_CLASS_RE.match(cls)
        if match:
            return match.group(1)
    return None


class _WebMarkdownConverter(MarkdownConverter):
    """`download-web.js` の TurndownService 設定(:37-43)を再現する。"""

    class Options(MarkdownConverter.DefaultOptions):
        heading_style = ATX
        bullets = "-"
        strong_em_symbol = "*"
        code_language_callback = staticmethod(_code_language)

    def _flatten_to_block(self, el: object, text: str, parent_tags: set[str]) -> str:
        """table/dl 系要素をブロック単位のプレーンテキストへ平坦化する。

        vanilla turndown(GFMプラグイン無し)には table/tr/td/th/thead/tbody/
        dl/dt/dd 専用のルールが無く、既定のブロック要素ルール(中身をそのまま
        `\\n\\n` で区切るだけ)にフォールバックする。`markdownify` の既定実装は
        GFM パイプテーブルを生成してしまうため、ここで turndown の「ルール無し
        ブロック要素」相当の挙動に差し替える。
        """
        if "_inline" in parent_tags:
            return " " + text.strip() + " "
        text = text.strip()
        return f"\n\n{text}\n\n" if text else ""

    convert_table = _flatten_to_block
    convert_thead = _flatten_to_block
    convert_tbody = _flatten_to_block
    convert_tfoot = _flatten_to_block
    convert_tr = _flatten_to_block
    convert_td = _flatten_to_block
    convert_th = _flatten_to_block
    convert_dl = _flatten_to_block
    convert_dt = _flatten_to_block
    convert_dd = _flatten_to_block
    convert_caption = _flatten_to_block

    def _pass_through(self, el: object, text: str, parent_tags: set[str]) -> str:
        """vanilla turndown には `<s>`/`<del>`(取り消し線)のルールが無い。

        `turndown-plugin-gfm` の `strikethrough` ルールが無いと、これらの要素は
        turndown の既定フォールバック(inline 要素は中身をそのまま通す)に
        落ちて `~~...~~` は付かない。`markdownify` は既定でGFM同等の `~~`
        マークアップを付けてしまうため、ここで無効化する。
        """
        return text

    convert_s = _pass_through
    convert_del = _pass_through

    def convert_li(self, el: object, text: str, parent_tags: set[str]) -> str:
        """turndown の `listItem` ルール(`turndown.cjs.js` 該当箇所)を再現する。

        turndown はインデント幅を prefix の実際の長さに合わせず、常に固定4
        スペースを使う(`content.replace(/\\n/gm, '\\n    ')`)。`markdownify`
        の既定実装は prefix の長さ分だけインデントするため、番号付きリストの
        桁が変わる場合などに幅がずれる。ここでは turndown と同じ「常に4
        スペース固定」に合わせる。
        """
        text = (text or "").strip("\n")
        if not text:
            return "\n"
        text = text.rstrip("\n")
        text = re.sub(r"\n", "\n    ", text)

        parent = el.parent  # type: ignore[attr-defined]
        if parent is not None and parent.name == "ol":
            start_attr = parent.get("start")
            try:
                start = int(start_attr) if start_attr else 1
            except ValueError:
                start = 1
            index = len(el.find_previous_siblings("li"))  # type: ignore[attr-defined]
            prefix = f"{start + index}.  "
        else:
            prefix = self.options["bullets"][0] + "   "

        has_next_sibling = el.find_next_sibling() is not None  # type: ignore[attr-defined]
        tail = "\n" if has_next_sibling and not text.endswith("\n") else ""
        return prefix + text + tail


def _absolutize_urls(soup: BeautifulSoup, base_url: str) -> None:
    """`href`/`src` を絶対URL化する(旧実装 `htmlToMarkdown` :129-153 と同じ除外条件)。"""
    for el in soup.select("a[href]"):
        href = el.get("href")
        if (
            href
            and not href.startswith("http")
            and not href.startswith("mailto:")
            and not href.startswith("#")
        ):
            # URL変換エラーは無視する(旧実装と同じ)。
            with contextlib.suppress(ValueError):
                el["href"] = urljoin(base_url, href)

    for el in soup.select("img[src]"):
        src = el.get("src")
        if src and not src.startswith("http") and not src.startswith("data:"):
            with contextlib.suppress(ValueError):
                el["src"] = urljoin(base_url, src)


def html_to_markdown(html: str, base_url: str) -> str:
    """HTMLページ本文をMarkdownへ変換する(旧 `htmlToMarkdown` の移植、advisory)。"""
    soup = BeautifulSoup(html, "html.parser")
    _absolutize_urls(soup, base_url)
    for el in soup.select(_REMOVE_SELECTOR):
        el.decompose()
    return _WebMarkdownConverter().convert(str(soup))


def extract_page_title(html: str) -> str | None:
    """`<title>` → `<h1>` の順でページタイトルを取り出す(旧実装 :353-357)。

    `htmlToMarkdown` とは別の cheerio インスタンスで raw html を読む旧実装の
    通り、要素削除(`nav`/`header` 等)の影響を受けない生 HTML から取る。
    """
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find("title")
    if title_el is not None:
        text = title_el.get_text().strip()
        if text:
            return text
    h1_el = soup.find("h1")
    if h1_el is not None:
        text = h1_el.get_text().strip()
        if text:
            return text
    return None


def extract_links(html: str, base_url: str, base_domain: str) -> list[str]:
    """ページ内の同一ドメインリンクを抽出する(旧実装 `extractLinks` の移植)。

    フラグメントは除去し、順序は出現順(重複除去は呼び出し側の責務にせず、
    ここで `dict` の挿入順を使って行う。旧実装は `Set` を使っており同じ意味)。
    """
    soup = BeautifulSoup(html, "html.parser")
    domain_host = urlparse(base_domain).hostname
    links: dict[str, None] = {}
    for el in soup.select("a[href]"):
        href = el.get("href")
        if not href:
            continue
        try:
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
        except ValueError:
            continue
        if parsed.hostname != domain_host:
            continue
        cleaned = parsed._replace(fragment="").geturl()
        links.setdefault(cleaned, None)
    return list(links)


def extract_links_from_markdown(markdown: str, base_domain: str) -> list[str]:
    """保存済み Markdown から同一ドメインのリンクを抽出する(旧実装の移植)。

    304 応答では本文が返らないため、クロールを継続するにはローカルの内容から
    辿る必要がある(モジュール docstring 契約3)。保存時に相対URLは絶対URLへ
    変換済みなので、この正規表現ベースの抽出で足りる。
    """
    domain_host = urlparse(base_domain).hostname
    links: dict[str, None] = {}
    for match in _MARKDOWN_LINK_RE.finditer(markdown):
        raw = match.group(1) or match.group(2)
        try:
            parsed = urlparse(raw)
        except ValueError:
            continue
        if parsed.hostname != domain_host:
            continue
        cleaned = parsed._replace(fragment="").geturl()
        links.setdefault(cleaned, None)
    return list(links)


__all__ = [
    "extract_links",
    "extract_links_from_markdown",
    "extract_page_title",
    "html_to_markdown",
]
