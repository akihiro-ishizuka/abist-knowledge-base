"""Markdown → サニタイズ済み HTML(設計書 §12「Markdown内HTMLはWeb表示時にサニタイズする」)。

文書は esa・Web・Git 等の外部ソースから取得された未信頼入力であるため、
Markdown 内に埋め込まれた生 HTML(`<script>`、`onerror=` 等)をそのまま
レンダリングしてはならない。`markdown` でHTML化した後、`bleach` で
許可リスト方式にサニタイズする。
"""

from __future__ import annotations

import bleach
import markdown as _markdown

#: 文書表示に必要な最小限のタグのみ許可する(許可リスト方式)。
#: `script`/`style`/`iframe`/`on*` イベント属性/`javascript:` URL は
#: 常に除去される(bleach の既定動作)。
ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "strong",
        "em",
        "b",
        "i",
        "code",
        "pre",
        "blockquote",
        "ul",
        "ol",
        "li",
        "a",
        "img",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "span",
        "div",
    }
)

ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
    "*": ["class"],
}

ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto"})


def render_markdown_safe(text: str) -> str:
    """Markdown 本文を安全な HTML 文字列へ変換する。

    1. `markdown` で HTML 化(拡張: fenced_code, tables)。
    2. `bleach.clean` で許可リスト方式のサニタイズ(不明タグ・属性・
       危険プロトコルの URL を除去する)。
    """
    html = _markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )


__all__ = ["render_markdown_safe"]
