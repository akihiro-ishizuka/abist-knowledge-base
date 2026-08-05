"""stdout 純度テスト(M5 task-1-brief Step1、赤から書く)。

`presentation/mcp/**` を import しただけで stdout に1バイトも出てはならない。
実プロセスで `mcp serve kb-search` を起動し `initialize` を送ると、
stdout には JSON-RPC 行だけが現れ、ログはすべて stderr に出ることを確認する。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_importing_server_modules_writes_nothing_to_stdout() -> None:
    """import 時点の stdout 純度(サブプロセスで検証し、pytest 自身の出力を汚さない)。"""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import abist_kb.presentation.mcp.server_core\n"
            "import abist_kb.presentation.mcp.kb_search\n"
            "import abist_kb.presentation.mcp.kb_admin\n"
            "import abist_kb.presentation.mcp.payloads\n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_stdio_server_initialize_emits_only_jsonrpc_on_stdout(tmp_path: Path) -> None:
    """実プロセスで `mcp serve kb-search` を起動し `initialize` を送る。

    stdout の全行が JSON-RPC としてパースできること(余計なバイトが混じらないこと)。
    """
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "data").mkdir(parents=True)

    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "replay-test", "version": "0.0.0"},
                },
            }
        )
        + "\n"
    )

    script = (
        "import sys\n"
        "from abist_kb.presentation.cli.app import app\n"
        f"sys.argv = ['abist-kb', '--root', {str(root)!r}, 'mcp', 'serve', 'kb-search']\n"
        "app()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=request,
        capture_output=True,
        text=True,
        timeout=30,
    )

    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)  # 1バイトでも JSON-RPC 以外が混ざっていれば失敗する
        assert "jsonrpc" in parsed


def test_stdio_all_server_initialize_emits_only_jsonrpc_on_stdout(tmp_path: Path) -> None:
    """M5 task-4: 結合サーバー `all`(15ツール+新規12ツール)でも stdout 純度は保たれる。

    kb-search/kb-download 単独サーバーと同じテスト形を `all` にも適用し、
    新規ジョブツールを import・組み込みしても stdout が JSON-RPC 専用のまま
    であることを確認する。
    """
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True)
    (root / "data").mkdir(parents=True)

    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "replay-test", "version": "0.0.0"},
                },
            }
        )
        + "\n"
    )

    script = (
        "import sys\n"
        "from abist_kb.presentation.cli.app import app\n"
        f"sys.argv = ['abist-kb', '--root', {str(root)!r}, 'mcp', 'serve', 'all']\n"
        "app()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=request,
        capture_output=True,
        text=True,
        timeout=30,
    )

    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        assert "jsonrpc" in parsed
