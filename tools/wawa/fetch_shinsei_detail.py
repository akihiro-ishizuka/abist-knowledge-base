#!/usr/bin/env python3
r"""WaWaOffice の申請詳細を取得してテキスト化する。

一覧取得(fetch_shinsei_history.py)で得た _shinsei_id / _syoshiki_id を使い、
detailSubmit() と同じ POST で詳細ページを開く。

使い方:
    .venv\Scripts\python.exe tools\wawa\fetch_shinsei_detail.py --id 47589
    .venv\Scripts\python.exe tools\wawa\fetch_shinsei_detail.py --all
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from fetch_shinsei_history import (  # noqa: E402
    BASE,
    LIST_URL,
    UA,
    collect_form_fields,
    fetch_first_page,
    login,
)


def fetch_detail(
    session: requests.Session, fields: dict[str, str], fs_id: str, syoshiki_id: str
) -> str:
    """detailSubmit と同じ POST で申請詳細ページを取得する。"""
    payload = dict(fields)
    payload["module"] = "flow"
    payload["act"] = "FlowShinseiMyDetail"
    payload["id"] = fs_id
    payload["fs_id"] = fs_id
    payload["f_syoshiki_id"] = syoshiki_id
    res = session.post(LIST_URL, data=payload, timeout=30)
    res.raise_for_status()
    res.encoding = res.apparent_encoding or "utf-8"
    return res.text


def detail_to_text(html: str) -> str:
    """詳細ページを読みやすいテキストに落とす。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    lines: list[str] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        texts = [c.get_text(" ", strip=True) for c in cells]
        texts = [t for t in texts if t]
        if texts:
            lines.append(" | ".join(texts))

    # 重複行を潰しつつ順序は保つ
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="WaWaOffice 申請詳細を取得する")
    parser.add_argument("--userid", default=os.environ.get("WAWA_USERID"))
    parser.add_argument("--out", default="_esa_work/wawa/detail", help="出力ディレクトリ")
    parser.add_argument("--id", action="append", help="取得する申請ID(複数指定可)")
    parser.add_argument("--all", action="store_true", help="一覧の全件を取得する")
    parser.add_argument(
        "--history",
        default="_esa_work/wawa/shinsei_history.json",
        help="一覧取得の JSON",
    )
    parser.add_argument("--dump-html", action="store_true", help="生 HTML も保存する")
    args = parser.parse_args()

    history = json.loads(Path(args.history).read_text(encoding="utf-8"))
    targets = [
        r for r in history if args.all or (args.id and r.get("_shinsei_id") in args.id)
    ]
    if not targets:
        print("対象がありません。--id か --all を指定してください。", file=sys.stderr)
        return 1

    userid = args.userid or input("ユーザーID: ").strip()
    password = os.environ.get("WAWA_PASSWORD") or getpass.getpass("パスワード: ")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    login(session, userid, password)
    print("ログインしました。")

    fields = collect_form_fields(fetch_first_page(session))

    for row in targets:
        fs_id = row.get("_shinsei_id", "")
        syoshiki_id = row.get("_syoshiki_id", "")
        if not fs_id:
            continue

        html = fetch_detail(session, fields, fs_id, syoshiki_id)
        if args.dump_html:
            (out_dir / f"{fs_id}.html").write_text(html, encoding="utf-8")

        text = detail_to_text(html)
        header = f"# {row.get('件名', '')}\n\n申請日: {row.get('申請日▼', '')}\n識別名: {row.get('申請識別名', '')}\n書式: {row.get('書式', '')}\n\n---\n\n"
        (out_dir / f"{fs_id}.txt").write_text(header + text, encoding="utf-8")
        print(f"{fs_id}: {len(text)} 文字 - {row.get('件名', '')[:40]}")
        time.sleep(1)

    print(f"出力先: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
