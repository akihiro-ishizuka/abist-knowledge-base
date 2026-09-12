"""M3 task-6: 旧 Node 実装との E2E バイト同一検証(`task-6-brief.md`)。

esa・git は完全なバイト同一を要求する。web は HTML→Markdown 変換器が別物
(旧: turndown、新: `html_to_md.py`)なので不一致を許容するが、差分を分類して
記録し、変換器に依存しない不変条件(出力パス集合・front matter キーと順序・
date の初回取得日維持)だけを assert する。

**絶対条件**: `C:\\Temp\\multi-source-knowledge-base` には一切書き込まない。
`_old_node_sandbox.py` のサンドボックスコピー方式(旧スクリプト+`tools/lib/`を
OS一時領域へ複製、`node_modules`はジャンクション)で旧実装を実行する。
各テストは `old_repo_guard` フィクスチャで前後のフィンガープリントを比較し、
差異があれば例外で落ちる(§`_old_node_sandbox.assert_unchanged`)。

旧リポジトリが無い環境(CI等)ではこのモジュール全体をスキップする
(`pytestmark`)。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from abist_kb.domain.frontmatter import parse_frontmatter
from abist_kb.domain.sync_policy import (
    LocalState,
    RemoteState,
    SyncRecord,
    decide_sync_action,
)
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.sources.esa import EsaClient, EsaSyncRunner
from abist_kb.infrastructure.sources.web import WebSyncRunner

from . import _old_node_sandbox as old
from .conftest import FAKE_TOKEN, MockEsaServer
from .test_web import WebPageServer

pytestmark = pytest.mark.skipif(
    not old.old_repo_available(),
    reason=(
        "旧リポジトリ (multi-source-knowledge-base) が見つからないため "
        "E2E バイト同一検証をスキップします(KB_OLD_REPO で場所を指定できます)。"
    ),
)


@pytest.fixture(scope="module")
def old_repo_guard() -> Iterator[None]:
    """モジュール内の全テストの前後で旧リポジトリの無変更を検証する。"""
    before = old.fingerprint_old_repo()
    yield
    after = old.fingerprint_old_repo()
    old.assert_unchanged(before, after)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory, old_repo_guard: None) -> Path:
    dest = tmp_path_factory.mktemp("old-node-sandbox")
    return old.build_sandbox(dest)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _assert_trees_byte_identical(a: Path, b: Path, *, label: str) -> None:
    tree_a = _tree_bytes(a)
    tree_b = _tree_bytes(b)
    assert set(tree_a) == set(tree_b), (
        f"{label}: 出力ファイル集合が一致しない (Python限定={set(tree_a) - set(tree_b)}, "
        f"旧限定={set(tree_b) - set(tree_a)})"
    )
    mismatches = [path for path in tree_a if tree_a[path] != tree_b[path]]
    assert not mismatches, f"{label}: バイト不一致のファイル: {mismatches}"


# ===========================================================================
# esa: バイト同一(必須)
# ===========================================================================


def _esa_posts() -> list[dict]:
    """brief 必須の5境界: 日本語本文・CRLF本文・複数階層カテゴリ・タグ・
    サニタイズが必要なタイトル(Windows予約名)。

    すべて `tags` に非空の値を与える(`tags: []` にはしない)。空配列は
    `test_esa_empty_tags_diverges_from_old_node_known_finding` が単独で扱う、
    既知のバイト不一致(Python は空配列の `tags` 行自体を省略するが、旧
    `generateFrontMatter` は空配列でも `tags: []` を出力する)を踏むため、
    ここでは意図的に避けて他の4境界の検証をこの既知差分から独立させる。
    """
    return [
        {
            "number": 1,
            "name": "日本語のタイトルです",
            "created_at": "2026-01-05T09:00:00+09:00",
            "updated_at": "2026-01-06T10:30:00+09:00",
            "created_by": {"screen_name": "田中"},
            "updated_by": {"screen_name": "鈴木"},
            "category": "日本語カテゴリ",
            "tags": ["memo"],
            "url": "https://example.esa.io/posts/1",
            "body_md": "# 見出し\n\n日本語の本文です。改行とか句読点、記号も含みます。",
        },
        {
            "number": 2,
            "name": "CRLF本文の記事",
            "created_at": "2026-01-05T09:00:00+09:00",
            "updated_at": "2026-01-05T09:00:00+09:00",
            "created_by": {"screen_name": "tester"},
            "updated_by": {"screen_name": "tester"},
            "category": "",
            "tags": ["memo"],
            "url": "https://example.esa.io/posts/2",
            "body_md": "# 見出し\r\n\r\n本文1行目\r\n本文2行目\r\n",
        },
        {
            "number": 3,
            "name": "ネストカテゴリ記事",
            "created_at": "2026-01-05T09:00:00+09:00",
            "updated_at": "2026-01-05T09:00:00+09:00",
            "created_by": {"screen_name": "tester"},
            "updated_by": {"screen_name": "tester"},
            "category": "開発/CATIA/自動化/詳細設計",
            "tags": ["memo"],
            "url": "https://example.esa.io/posts/3",
            "body_md": "階層カテゴリの本文。",
        },
        {
            "number": 4,
            "name": "タグ付き記事",
            "created_at": "2026-01-05T09:00:00+09:00",
            "updated_at": "2026-01-05T09:00:00+09:00",
            "created_by": {"screen_name": "tester"},
            "updated_by": {"screen_name": "tester"},
            "category": "同期テスト",
            "tags": ["catia", "自動化", "spec"],
            "url": "https://example.esa.io/posts/4",
            "body_md": "タグ付きの本文。",
        },
        {
            "number": 5,
            "name": "CON",  # Windows予約名: サニタイズ対象
            "created_at": "2026-01-05T09:00:00+09:00",
            "updated_at": "2026-01-05T09:00:00+09:00",
            "created_by": {"screen_name": "tester"},
            "updated_by": {"screen_name": "tester"},
            "category": "",
            "tags": ["memo"],
            "url": "https://example.esa.io/posts/5",
            "body_md": "予約名タイトルの本文。",
        },
    ]


def test_esa_docs_output_is_byte_identical_to_old_node(
    documents: DocumentRepository,
    tmp_root: Path,
    esa_server: MockEsaServer,
    sandbox: Path,
) -> None:
    """esa: モックesa APIから取得した同一の記事群を Python/旧Node双方に保存させ、
    `docs/` をバイト同一で比較する(brief必須要件)。"""
    for post in _esa_posts():
        esa_server.add_post(post)

    # --- Python 側: モック esa API を実際に HTTP 経由で叩いて取得する ---
    async def _fetch_all() -> list[dict]:
        async with EsaClient(
            team=esa_server.team, access_token=FAKE_TOKEN, base_url=esa_server.base_url
        ) as client:
            return [await client.get_post(p["number"]) for p in _esa_posts()]

    fetched_posts = asyncio.run(_fetch_all())

    python_docs = tmp_root / "docs"
    python_docs.mkdir()
    runner = EsaSyncRunner(
        documents=documents, root_dir=tmp_root, docs_dir=python_docs, output_dir="docs"
    )
    for post in fetched_posts:
        item = runner.save_post(post)
        assert item.write, f"想定外にスキップされた: {item.path}"

    # --- 旧 Node 側: 同じ post JSON(HTTP経由で取得したものと同一)を savePost へ直接渡す ---
    old_root = sandbox / "esa-run"
    old_root.mkdir()
    posts_file = old_root / "posts.json"
    posts_file.write_text(json.dumps(fetched_posts, ensure_ascii=False), encoding="utf-8")
    results_file = old_root / "results.json"

    driver = old.write_esa_driver(sandbox)
    proc = old.run_node(
        sandbox,
        driver,
        [str(posts_file), "esa-run/docs", str(results_file)],
    )
    assert proc.returncode == 0, f"旧 savePost 実行に失敗: {proc.stdout}\n{proc.stderr}"
    old_results = json.loads(results_file.read_text(encoding="utf-8"))
    assert all(r["action"] in ("create", "update") for r in old_results), (
        f"旧側が想定外にスキップした: {old_results}"
    )

    _assert_trees_byte_identical(python_docs, old_root / "docs", label="esa")


def test_esa_empty_tags_is_byte_identical_to_old_node(
    documents: DocumentRepository, tmp_root: Path, esa_server: MockEsaServer, sandbox: Path
) -> None:
    """esa: `tags` が空配列の記事でも front matter がバイト同一であること(M3final)。

    旧 `generateFrontMatter`(download-article.js:124-133)は `value !== null &&
    value !== undefined && value !== ''` だけで判定するため、空配列は `''` と
    型が異なり弾かれず `tags: []` を常に出力する。Python 側 `esa._yaml_scalar_line`
    はかつて `isinstance(value, list) and not value` で空配列を明示的にスキップし
    `tags` 行自体を省略していたため1バイト不一致になっていたが、旧実装と同じ
    「空配列も出力する」規則に修正済み。"""
    post = {
        "number": 100,
        "name": "タグ無し記事",
        "created_at": "2026-01-05T09:00:00+09:00",
        "updated_at": "2026-01-05T09:00:00+09:00",
        "created_by": {"screen_name": "tester"},
        "updated_by": {"screen_name": "tester"},
        "category": "",
        "tags": [],
        "url": "https://example.esa.io/posts/100",
        "body_md": "本文。",
    }
    esa_server.add_post(post)

    async def _fetch() -> dict:
        async with EsaClient(
            team=esa_server.team, access_token=FAKE_TOKEN, base_url=esa_server.base_url
        ) as client:
            return await client.get_post(100)

    fetched = asyncio.run(_fetch())

    python_docs = tmp_root / "docs"
    python_docs.mkdir()
    runner = EsaSyncRunner(
        documents=documents, root_dir=tmp_root, docs_dir=python_docs, output_dir="docs"
    )
    runner.save_post(fetched)

    old_root = sandbox / "esa-tags-run"
    old_root.mkdir()
    posts_file = old_root / "posts.json"
    posts_file.write_text(json.dumps([fetched], ensure_ascii=False), encoding="utf-8")
    results_file = old_root / "results.json"
    driver = old.write_esa_driver(sandbox)
    proc = old.run_node(sandbox, driver, [str(posts_file), "esa-tags-run/docs", str(results_file)])
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    _assert_trees_byte_identical(python_docs, old_root / "docs", label="esa(空tags)")


# ===========================================================================
# web: バイト同一は要求しない。分類済み差分を記録し、変換器非依存の不変条件のみ assert
# ===========================================================================


@pytest.fixture
def web_site() -> Iterator[WebPageServer]:
    server = WebPageServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_web_output_diff_is_classified_and_converter_independent_invariants_hold(
    documents: DocumentRepository,
    tmp_root: Path,
    web_site: WebPageServer,
    sandbox: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """web: バイト同一は要求しない(HTML→MD変換器が別物、一度きりの変換器変更として
    許容する意図的な逸脱)。かわりに、変換器に依存しない不変条件だけを assert する:
    出力ファイルパス集合・front matterキーと順序・再取得時の date 維持。差分は
    分類してテスト出力へ記録する(レポートへ転記する)。"""
    base = web_site.base_url
    web_site.state.body = f'<p>トップページ</p><a href="{base}/child">子ページ</a>'
    web_site.state.links = {"/child": '<p>子ページの本文</p><a href="' + base + '/">戻る</a>'}
    web_site.state.title = "トップ"

    python_docs = tmp_root / "docs"
    python_docs.mkdir()
    runner = WebSyncRunner(
        documents=documents,
        root_dir=tmp_root,
        docs_dir=python_docs,
        output_dir="docs",
        base_domain=base,
        max_depth=1,
    )
    asyncio.run(runner.crawl(base + "/"))
    python_first_date = parse_frontmatter(
        (python_docs / "index.md").read_bytes().decode("utf-8")
    ).data["date"]

    old_root = sandbox / "web-run"
    old_root.mkdir()
    proc1 = old.run_node(
        sandbox,
        "download-web.js",
        [base + "/", "--output-dir", "web-run/docs", "--max-depth", "1", "--delay", "0"],
    )
    assert proc1.returncode == 0, f"旧 download-web.js 実行に失敗: {proc1.stdout}\n{proc1.stderr}"
    old_docs = sandbox / "web-run" / "docs"
    old_first_date = parse_frontmatter((old_docs / "index.md").read_bytes().decode("utf-8")).data[
        "date"
    ]

    # --- 不変条件1: 出力ファイルパス集合は変換器に依存しない ---
    python_paths = {p.relative_to(python_docs).as_posix() for p in python_docs.rglob("*.md")}
    old_paths = {p.relative_to(old_docs).as_posix() for p in old_docs.rglob("*.md")}
    assert python_paths == old_paths, f"出力パス集合が一致しない: {python_paths ^ old_paths}"

    # --- 不変条件2: front matter キーと順序 ---
    python_index = (python_docs / "index.md").read_bytes().decode("utf-8")
    old_index = (old_docs / "index.md").read_bytes().decode("utf-8")
    python_keys = list(parse_frontmatter(python_index).data.keys())
    old_keys = list(parse_frontmatter(old_index).data.keys())
    assert python_keys == old_keys, f"front matterキー順が一致しない: {python_keys} != {old_keys}"

    # --- 差分の分類(バイト同一は要求しない。本文=変換器差分として記録するだけ) ---
    classified = _classify_web_diff(python_index, old_index)
    assert classified["frontmatter_diff"] == [], (
        f"front matter自体に差分がある(変換器差分の範囲を超える): {classified['frontmatter_diff']}"
    )

    # --- 不変条件3: 再取得しても date(初回取得日)は維持される ---
    web_site.state.etag = '"v2"'
    web_site.state.body = f'<p>更新後のトップページ</p><a href="{base}/child">子ページ</a>'
    asyncio.run(runner.crawl(base + "/"))
    python_date_after = parse_frontmatter(
        (python_docs / "index.md").read_bytes().decode("utf-8")
    ).data["date"]
    assert python_date_after == python_first_date, "Python側: 再取得でdateが動いた"

    # 旧側も同じ出力先(`web-run/docs`)へ再実行する。`downloadPage` の date維持は
    # DBではなく「既存ファイルの front matter から読む」実装(`humanManagedFrom`
    # 相当)なので、同じ絶対パスへ2回書けばプロセスをまたいでも真の再取得になる。
    proc2 = old.run_node(
        sandbox,
        "download-web.js",
        [base + "/", "--output-dir", "web-run/docs", "--max-depth", "1", "--delay", "0"],
    )
    assert proc2.returncode == 0, proc2.stderr
    old_date_after = parse_frontmatter((old_docs / "index.md").read_bytes().decode("utf-8")).data[
        "date"
    ]
    assert old_date_after == old_first_date, "旧側: 再取得でdateが動いた"


def _classify_web_diff(python_content: str, old_content: str) -> dict[str, list[str]]:
    """front matter と本文を分けて差分を分類する(brief: 差分を記録し許容判断を明文化)。"""
    python_parsed = parse_frontmatter(python_content)
    old_parsed = parse_frontmatter(old_content)
    frontmatter_diff = [
        key
        for key in set(python_parsed.data) | set(old_parsed.data)
        if key != "date" and python_parsed.data.get(key) != old_parsed.data.get(key)
    ]
    return {
        "frontmatter_diff": frontmatter_diff,
        "body_differs": [str(python_parsed.body != old_parsed.body)],
    }


# ===========================================================================
# --output json 純度
# ===========================================================================

_ANSI_RE_SOURCE = r"\x1b\["


def test_sync_cli_output_json_is_pure_stdout_no_ansi(tmp_root: Path) -> None:
    """`--output json` は stdout に単一JSON文書のみを書き、ANSIを含まない。
    診断メッセージ(あれば)は stderr へ出す(brief必須要件)。実プロセス境界で
    検証する(`CliRunner` のインメモリキャプチャではなく本物の stdout/stderr分離)。
    """
    import re

    exe = _find_abist_kb_executable()
    proc = subprocess.run(
        [exe, "--root", str(tmp_root), "--output", "json", "source", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    stdout = proc.stdout
    assert not re.search(_ANSI_RE_SOURCE, stdout), f"stdoutにANSIが混入した: {stdout!r}"
    parsed = json.loads(stdout)  # 単一JSONとしてパースできること(末尾にゴミが無いこと)
    assert isinstance(parsed, (list, dict))
    assert not re.search(_ANSI_RE_SOURCE, proc.stderr), f"stderrにANSIが混入した: {proc.stderr!r}"


def _find_abist_kb_executable() -> str:
    exe = shutil.which("abist-kb")
    if exe:
        return exe
    venv_exe = Path(sys.prefix) / "Scripts" / "abist-kb.exe"
    if venv_exe.is_file():
        return str(venv_exe)
    pytest.skip("abist-kb 実行ファイルが見つかりません")


# ===========================================================================
# 実データ dry-run: 実 sync-state.sqlite の記録から (remote, record, local) を
# 再構成し、Python/旧の decide_sync_action が全件で一致することを確認する
# (brief step3)。実際の esa/web への到達性が無い環境のため、"取得元の最新状態"
# は各記録の source_content_hash/source_updated_at で代替する(=直近の同期が
# 成功した状態からの再判定)。docs/・data/ へは一切書き込まない(読み取りのみ)。
# ===========================================================================


def test_dry_run_plan_agrees_with_old_planner_for_real_recorded_documents(
    sandbox: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    root = old.old_repo_root()
    db_path = root / "data" / "sync-state.sqlite"
    if not db_path.is_file():
        pytest.skip("実 sync-state.sqlite が無いためスキップします。")

    import sqlite3

    # 実DBを直接 `mode=ro` で開いても、WAL ジャーナルモードだと SQLite が
    # `-shm`/`-wal` の副ファイルを新規作成することがある(`readonly:true` でも
    # -shm の mtime が動く、既知のM1での実測結果と同じ理由)。brief の
    # 「スナップショットをコピーする」指示どおり、DBを丸ごとコピーしてから
    # コピー側を通常モードで開くことで、旧リポジトリへは一切書き込まない。
    snapshot_dir = tmp_path_factory.mktemp("sync-state-snapshot")
    snapshot_db = snapshot_dir / "sync-state.sqlite"
    shutil.copy2(db_path, snapshot_db)
    for sidecar in ("-wal", "-shm"):
        sidecar_path = db_path.with_name(db_path.name + sidecar)
        if sidecar_path.is_file():
            shutil.copy2(sidecar_path, snapshot_db.with_name(snapshot_db.name + sidecar))

    conn = sqlite3.connect(str(snapshot_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT path, source, local_content_hash, source_content_hash, source_updated_at "
            "FROM documents WHERE local_content_hash IS NOT NULL "
            "ORDER BY path LIMIT 30"
        ).fetchall()
    finally:
        conn.close()

    assert rows, "実データからサンプルが取れなかった"

    cases = []
    for row in rows:
        cases.append(
            {
                "path": row["path"],
                "remote": {
                    "contentHash": row["source_content_hash"],
                    "updatedAt": row["source_updated_at"],
                },
                "record": {
                    "local_content_hash": row["local_content_hash"],
                    "source_content_hash": row["source_content_hash"],
                    "source_updated_at": row["source_updated_at"],
                },
                "local": {"exists": True, "bodyHash": row["local_content_hash"]},
            }
        )

    # --- Python 側 ---
    python_actions = {}
    for case in cases:
        record = case["record"]
        decision = decide_sync_action(
            remote=RemoteState(
                content_hash=case["remote"]["contentHash"], updated_at=case["remote"]["updatedAt"]
            ),
            record=None
            if record["local_content_hash"] is None
            else SyncRecord(
                local_content_hash=record["local_content_hash"],
                source_content_hash=record["source_content_hash"],
                source_updated_at=record["source_updated_at"],
            ),
            local=LocalState(exists=True, body_hash=case["local"]["bodyHash"]),
            force=False,
        )
        python_actions[case["path"]] = str(decision.action)

    # --- 旧 Node 側(サンドボックスコピー経由で sync-planner.js を直接呼ぶ) ---
    driver = sandbox / "plan_driver.mjs"
    driver.write_text(
        "import { decideSyncAction } from './tools/lib/sync-planner.js';\n"
        "import fs from 'fs/promises';\n"
        "const [, , inFile, outFile] = process.argv;\n"
        "const cases = JSON.parse(await fs.readFile(inFile, 'utf-8'));\n"
        "const out = {};\n"
        "for (const c of cases) {\n"
        "  const decision = decideSyncAction({\n"
        "    remote: { contentHash: c.remote.contentHash, updatedAt: c.remote.updatedAt },\n"
        "    record: c.record.local_content_hash === null ? null : {\n"
        "      local_content_hash: c.record.local_content_hash,\n"
        "      source_content_hash: c.record.source_content_hash,\n"
        "      source_updated_at: c.record.source_updated_at\n"
        "    },\n"
        "    local: { exists: true, bodyHash: c.local.bodyHash },\n"
        "    force: false\n"
        "  });\n"
        "  out[c.path] = decision.action;\n"
        "}\n"
        "await fs.writeFile(outFile, JSON.stringify(out), 'utf-8');\n",
        encoding="utf-8",
        newline="\n",
    )
    in_file = sandbox / "plan_cases.json"
    out_file = sandbox / "plan_out.json"
    in_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    proc = old.run_node(sandbox, "plan_driver.mjs", [str(in_file), str(out_file)])
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    old_actions = json.loads(out_file.read_text(encoding="utf-8"))

    disagreements = [
        {"path": path, "python": python_actions[path], "old": old_actions.get(path)}
        for path in python_actions
        if python_actions[path] != old_actions.get(path)
    ]
    assert disagreements == [], f"per-path判定の不一致: {disagreements}"
