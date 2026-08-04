"""索引済み B32doc ページを curated/procedures へ昇格(ドラフト生成)する。

旧実装 `tools/knowledge-curator/promote.py` の移植。`procedures-index.jsonl` から
`id` に一致する行を探し、原本 MD を読んで curated テンプレートを埋めたドラフトを
書き出す。人が本文を整形し `status: draft` → `reviewed` にして完成させる想定。
**原本は編集しない**(このモジュールはドラフトの書き込み以外、一切書き込まない)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from abist_kb.application.curation import module_dictionary

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "curated-procedure.md"

_SECTION_HEADERS = ("## Summary Keys", "## Extracted Content", "## Structured Data", "## Links")
_MAX_LINKS = 30

_LIST_RE = re.compile(r"^\s*-\s+(.*)$")
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
_LINK_RE = re.compile(r"^\s*-\s*\((.*?)\)\s*->\s*(.+\S)\s*$")


class RecordNotFoundError(Exception):
    """索引に `id` が存在しない。"""


class SourceMissingError(Exception):
    """索引レコードが指す原本 MD が存在しない。"""


def _unquote(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    return val


def parse_front_matter(text: str) -> tuple[dict[str, object], str]:
    """先頭の `---` ブロックを平坦な dict として取り出し、本文を返す。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    meta: dict[str, object] = {}
    cur_key: str | None = None
    for line in lines[1:end]:
        if not line.strip():
            continue
        m = _LIST_RE.match(line)
        if m and cur_key is not None and isinstance(meta.get(cur_key), list):
            meta[cur_key].append(_unquote(m.group(1)))  # type: ignore[union-attr]
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in ("", "[]"):
            meta[key] = []
            cur_key = key
        else:
            meta[key] = _unquote(val)
            cur_key = key

    body = "\n".join(lines[end + 1 :])
    return meta, body


def split_sections(body: str) -> dict[str, str]:
    """本文を4つの固定セクションへ分割する(列0の完全一致で検出)。"""
    lines = body.split("\n")
    positions: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in _SECTION_HEADERS:
            positions.append((i, stripped))
    sections = dict.fromkeys(_SECTION_HEADERS, "")
    for idx, (i, header) in enumerate(positions):
        start = i + 1
        stop = positions[idx + 1][0] if idx + 1 < len(positions) else len(lines)
        sections[header] = "\n".join(lines[start:stop]).strip("\n")
    return sections


def rewrite_link(raw: str, cur_id: str) -> dict[str, str | None] | None:
    """本文リンク先(旧ルート絶対パス)を `target_id` へ写像する。"""
    base = raw.strip()
    anchor: str | None = None
    if "#" in base:
        base, anchor = base.split("#", 1)
        anchor = anchor.strip() or None
    norm = base.replace("\\", "/")
    m = re.search(r"online/Japanese/([A-Za-z0-9]+_C2)/(.*)$", norm)
    if not m:
        if anchor:
            return {"target_id": cur_id, "anchor": anchor}
        return None
    mod, tail = m.group(1), m.group(2)
    low = tail.lower()
    if "images/" in low or "icons_c2/" in low or "samples/" in low:
        return None
    fname = tail.split("/")[-1]
    if not fname.lower().endswith(".htm"):
        if anchor:
            return {"target_id": cur_id, "anchor": anchor}
        return None
    name = fname[:-4]
    mod_short = mod[:-3] if mod.endswith("_C2") else mod
    return {"target_id": f"{mod_short}/{name}", "anchor": anchor}


def parse_page_links(links_section: str, cur_id: str) -> list[dict[str, str | None]]:
    """`## Links` からページ間リンクだけを `{text, target_id, anchor}` で返す。"""
    out: list[dict[str, str | None]] = []
    for line in links_section.split("\n"):
        m = _LINK_RE.match(line)
        if not m:
            continue
        text, raw = m.group(1).strip(), m.group(2)
        ref = rewrite_link(raw, cur_id)
        if ref:
            out.append({"text": text, **ref})
            if len(out) >= _MAX_LINKS:
                break
    return out


def find_record(index_path: Path, target_id: str) -> dict[str, object] | None:
    """索引を stream 走査して id 一致レコードを返す。"""
    with index_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id") == target_id:
                return rec
    return None


def _render_tags(tags: list[str]) -> str:
    if not tags:
        return "[]"
    return "[" + ", ".join(f'"{t}"' for t in tags) + "]"


def _render_related(links: list[dict[str, str | None]]) -> str:
    if not links:
        return "- （なし）"
    out = []
    for link in links:
        text = link.get("text") or link.get("target_id", "")
        target = link.get("target_id", "")
        anchor = link.get("anchor")
        suffix = f"#{anchor}" if anchor else ""
        out.append(f"- {text} → `{target}{suffix}`")
    return "\n".join(out)


@dataclass(frozen=True, slots=True)
class Draft:
    """`build_draft` の結果: ドラフト本文と出力先パス(curated ルートからの相対)。"""

    content: str
    relative_path: Path


def build_draft(
    rec: dict[str, object], *, repo_root: Path, template_path: Path | None = None
) -> Draft:
    """索引レコードから昇格ドラフトを組み立てる。"""
    md_path = repo_root / str(rec["path"])
    if not md_path.exists():
        raise SourceMissingError(f"原本が見つかりません: {rec['path']}")
    meta, body = parse_front_matter(md_path.read_text(encoding="utf-8"))
    sections = split_sections(body)
    extracted = sections["## Extracted Content"].strip() or "（原本に本文なし）"
    links = parse_page_links(sections["## Links"], str(rec["id"]))

    template = (template_path or _TEMPLATE_PATH).read_text(encoding="utf-8")
    workbench = rec.get("workbench") or module_dictionary.display_name(rec["module"])
    replacements = {
        "{{title}}": str(rec.get("title", rec["id"])),
        "{{workbench}}": str(workbench),
        "{{module}}": str(rec["module"]),
        "{{source_id}}": str(rec["id"]),
        "{{source_path}}": str(rec["path"]),
        "{{original_language}}": str(rec.get("language", "")),
        "{{tags}}": _render_tags(list(rec.get("tags", []) or [])),  # type: ignore[arg-type]
        "{{created}}": date.today().isoformat(),
        "{{extracted}}": extracted,
        "{{related}}": _render_related(links),
    }
    draft = template
    for key, value in replacements.items():
        draft = draft.replace(key, value)

    slug = module_dictionary.workbench_slug(rec["module"])
    name = str(rec["source_name"])
    for ext in (".htm", ".html"):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    relative_path = Path(slug) / f"{name}.md"
    return Draft(content=draft, relative_path=relative_path)


DEFAULT_INDEX_RELATIVE_PATH = Path("docs/knowledge/generated/b32doc/catalog/procedures-index.jsonl")
DEFAULT_CURATED_RELATIVE_DIR = Path("docs/knowledge/curated/procedures")


@dataclass(frozen=True, slots=True)
class PromoteResult:
    """`promote` の結果。`written` は `dry_run=True` のときは False。"""

    record_id: str
    output_path: Path
    content: str
    written: bool


def promote(
    *,
    record_id: str,
    repo_root: Path,
    index_path: Path | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> PromoteResult:
    """procedures-index.jsonl から `record_id` を昇格する(status: draft で書き出し)。

    `dry_run=True` の場合はファイルを一切作成/上書きしない(プレビューのみ)。
    出力先が既存かつ `force=False` の場合は `FileExistsError` を送出する。
    """
    resolved_index = index_path or (repo_root / DEFAULT_INDEX_RELATIVE_PATH)
    if not resolved_index.is_file():
        raise FileNotFoundError(f"索引が見つかりません: {resolved_index}")

    rec = find_record(resolved_index, record_id)
    if rec is None:
        raise RecordNotFoundError(f"id が索引にありません: {record_id}")

    draft = build_draft(rec, repo_root=repo_root)
    curated_dir = output_dir or (repo_root / DEFAULT_CURATED_RELATIVE_DIR)
    out_path = curated_dir / draft.relative_path

    if dry_run:
        return PromoteResult(
            record_id=record_id, output_path=out_path, content=draft.content, written=False
        )

    if out_path.exists() and not force:
        raise FileExistsError(f"既に存在します（force で上書き）: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(draft.content, encoding="utf-8")
    return PromoteResult(
        record_id=record_id, output_path=out_path, content=draft.content, written=True
    )


__all__ = [
    "DEFAULT_CURATED_RELATIVE_DIR",
    "DEFAULT_INDEX_RELATIVE_PATH",
    "Draft",
    "PromoteResult",
    "RecordNotFoundError",
    "SourceMissingError",
    "build_draft",
    "find_record",
    "parse_front_matter",
    "parse_page_links",
    "promote",
    "rewrite_link",
    "split_sections",
]
