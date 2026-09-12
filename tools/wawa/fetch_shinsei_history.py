#!/usr/bin/env python3
r"""WaWaOffice の申請履歴一覧を取得して CSV / JSON に落とす。

使い方:
    set WAWA_USERID=04085
    set WAWA_PASSWORD=xxxxxxxx
    .venv\Scripts\python.exe tools\wawa\fetch_shinsei_history.py --out _esa_work/wawa

パスワードはスクリプトに書かず、環境変数か --password-stdin で渡すこと。
--dump-html を付けると取得した生 HTML も保存する(解析調整用)。
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://asp06.wawa.ne.jp/abist/"
LOGIN_URL = urljoin(BASE, "index.html")
LIST_URL = urljoin(BASE, "index.html")
LIST_PARAMS = {
    "module": "flow",
    "act": "flow-shinsei-history-list",
    "para": "",
}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def login(session: requests.Session, userid: str, password: str) -> None:
    """ログインフォームを POST し、セッション Cookie を確立する。"""
    # 先に GET して Cookie(セッションID)を受け取っておく
    session.get(BASE, timeout=30)

    payload = {
        "login_from": "0",
        "module": "",
        "act": "",
        "sessions_temp_clear_no": "1",
        "userid": userid,
        "password": password,
        "mail_check_flg": "1",
        "submit": "ログイン",
    }
    res = session.post(LOGIN_URL, data=payload, timeout=30)
    res.raise_for_status()
    res.encoding = res.apparent_encoding or "utf-8"

    # ログインページが返ってきた = 失敗。フォームの有無で判定する。
    soup = BeautifulSoup(res.text, "html.parser")
    if soup.find("form", {"name": "loginForm"}):
        msg = soup.get_text(" ", strip=True)[:300]
        raise SystemExit(f"ログインに失敗しました。ID/パスワードを確認してください。\n応答: {msg}")


def fetch_first_page(session: requests.Session) -> str:
    """申請履歴一覧の1ページ目を GET で取得する。"""
    res = session.get(LIST_URL, params=LIST_PARAMS, timeout=30)
    res.raise_for_status()
    res.encoding = res.apparent_encoding or "utf-8"
    return res.text


def collect_form_fields(html: str) -> dict[str, str]:
    """一覧ページの form1 から hidden などの入力値を集める。

    ページ送りは JavaScript の pagingSubmit() が form1 の page に値を入れて
    POST する作りなので、フォームの現在値をそのまま引き継いで送る。
    """
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", {"name": "form1"})
    if form is None:
        return {}

    fields: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name or name == "cb[]":  # 行のチェックボックスは送らない
            continue
        itype = (inp.get("type") or "text").lower()
        if itype in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        fields[name] = inp.get("value", "")

    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        fields[name] = opt.get("value", "") if opt else ""

    return fields


def fetch_page(session: requests.Session, fields: dict[str, str], page: int) -> str:
    """pagingSubmit と同じ POST でページを取得する。"""
    payload = dict(fields)
    payload["module"] = "flow"
    payload["act"] = "FlowShinseiHistoryList"
    payload["page"] = str(page)
    res = session.post(LIST_URL, data=payload, timeout=30)
    res.raise_for_status()
    res.encoding = res.apparent_encoding or "utf-8"
    return res.text


def _cell_text(cell) -> str:
    """セルのテキストを取り出す。"""
    return cell.get_text(" ", strip=True)


def _parse_display_cell(cell) -> tuple[str, str]:
    """「表示項目」セルを (ラベル, 値) に分解する。

    このセルは <font size="-2">件名:</font><span>本文</span> という形。
    """
    label_el = cell.find("font")
    value_el = cell.find("span")
    label = label_el.get_text(" ", strip=True).rstrip(":：") if label_el else ""
    value = value_el.get_text(" ", strip=True) if value_el else ""
    if not label and not value:
        return "", _cell_text(cell)
    return label, value


def parse_rows(html: str) -> list[dict[str, str]]:
    """申請履歴テーブルを行の辞書リストに変換する。

    ヘッダの「表示項目」は colspan=8 の可変領域で、書式ごとに
    <td class="separate"> の数と colspan が変わる。そのため単純な
    位置合わせではなく colspan を数えながら列を対応付ける。
    表示項目内の各セルは「件名」「作業希望日」のようなラベルを
    自前で持つので、それをそのままキーにする。
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="flowListTable")
    if table is None:
        # クラス名が変わった場合の保険として行数最多の table を使う
        tables = soup.find_all("table")
        if not tables:
            return []
        table = max(tables, key=lambda t: len(t.find_all("tr")))

    head_tr = table.find("thead").find("tr") if table.find("thead") else None
    if head_tr is None:
        return []

    # ヘッダを (列名, 占有カラム数) の並びとして読む
    headers: list[tuple[str, int]] = []
    for th in head_tr.find_all("th"):
        name = th.get_text(" ", strip=True)
        span = int(th.get("colspan", 1))
        headers.append((name, span))

    rows: list[dict[str, str]] = []
    # このテーブルは行ごとに <tbody> が分かれているので table 全体から拾う
    for tr in table.find_all("tr", class_="dataCont"):
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue

        row: dict[str, str] = {}
        col = 0  # いま何カラム目にいるか
        # ヘッダの列境界を作る: 列名 -> (開始カラム, 終了カラム)
        bounds: list[tuple[str, int, int]] = []
        pos = 0
        for name, span in headers:
            bounds.append((name, pos, pos + span))
            pos += span

        for cell in cells:
            span = int(cell.get("colspan", 1))
            # このセルが属するヘッダ列を探す
            header_name = ""
            for name, start, end in bounds:
                if start <= col < end:
                    header_name = name
                    break

            if header_name == "表示項目":
                label, value = _parse_display_cell(cell)
                key = label or "表示項目"
                # 同じラベルが複数あれば連番を付ける
                if key in row:
                    n = 2
                    while f"{key}{n}" in row:
                        n += 1
                    key = f"{key}{n}"
                if label or value:
                    row[key] = value
            elif header_name:
                text = _cell_text(cell)
                if header_name not in row:
                    row[header_name] = text
                elif text:
                    row[header_name] = text
            col += span

        # 詳細リンクから申請ID・書式IDを取り出す
        link = tr.find(
            "a", href=re.compile(r"detailSubmit\(")
        )
        if link:
            m = re.search(
                r"detailSubmit\([^,]*,\s*'([^']*)',\s*'([^']*)',\s*[^,]*,\s*'([^']*)',\s*'([^']*)'\)",
                link["href"],
            )
            if m:
                row["_shinsei_id"] = m.group(3)
                row["_syoshiki_id"] = m.group(4)

        # 先頭のチェックボックス列は空なので落とす
        row.pop("", None)
        rows.append(row)

    return rows


def parse_paging(html: str) -> tuple[int, int, int]:
    """件数表示から (表示開始, 表示終了, 総件数) を読む。

    一覧の下部に「1 - 15 / 15件」という li.count がある。
    読めなければ (0, 0, 0) を返す。
    """
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find("li", class_="count")
    if el is None:
        return (0, 0, 0)
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*/\s*(\d+)", el.get_text(" ", strip=True))
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def write_outputs(rows: list[dict[str, str]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "shinsei_history.json"
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # CSV は全行のキーの和集合を列にする(行ごとに列数が違う場合の保険)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    csv_path = out_dir / "shinsei_history.csv"
    # Excel で開くため BOM 付き UTF-8
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"{len(rows)} 件を書き出しました:")
    print(f"  {json_path}")
    print(f"  {csv_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="WaWaOffice 申請履歴を取得する")
    parser.add_argument("--userid", default=os.environ.get("WAWA_USERID"))
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="パスワードを標準入力から読む(環境変数を使わない場合)",
    )
    parser.add_argument("--out", default="_esa_work/wawa", help="出力ディレクトリ")
    parser.add_argument("--max-pages", type=int, default=50, help="取得する最大ページ数")
    parser.add_argument(
        "--dump-html", action="store_true", help="取得した生 HTML も保存する"
    )
    args = parser.parse_args()

    userid = args.userid
    if not userid:
        userid = input("ユーザーID: ").strip()

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = os.environ.get("WAWA_PASSWORD") or getpass.getpass("パスワード: ")
    if not password:
        print("パスワードが空です。", file=sys.stderr)
        return 1

    out_dir = Path(args.out)

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    login(session, userid, password)
    print("ログインしました。")

    out_dir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, str]] = []

    html = fetch_first_page(session)
    fields = collect_form_fields(html)

    for page in range(1, args.max_pages + 1):
        if page > 1:
            html = fetch_page(session, fields, page)

        if args.dump_html:
            (out_dir / f"page{page:03d}.html").write_text(html, encoding="utf-8")

        rows = parse_rows(html)
        start, end, total = parse_paging(html)
        if total:
            print(f"page {page}: {len(rows)} 行 ({start}-{end} / {total}件)")
        else:
            print(f"page {page}: {len(rows)} 行")

        if not rows:
            break
        all_rows.extend(rows)

        # 件数表示が読めたらそれで終端を判定する
        if total and end >= total:
            break
        if not total:
            # 件数が読めない場合は取得できなくなるまで進む
            pass
        time.sleep(1)  # サーバに負荷をかけない

    if not all_rows:
        print("行が取れませんでした。--dump-html で HTML を保存して構造を確認してください。")
        return 1

    write_outputs(all_rows, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
