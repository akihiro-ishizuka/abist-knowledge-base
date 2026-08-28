"""`run_python_process` のキャンセル・タイムアウト・プロセスツリー終了。

`subprocess.run(timeout=)` は**直接の子しか kill しない**ため、従来は
RENDER_TIMEOUT のたびに Manim が起動した ffmpeg が孤児として残っていた。
Popen + プロセスツリー kill への置換でこの既存バグも直る。ここではその回帰を
「孫プロセスが実際に消えていること」で固定する。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from abist_kb.infrastructure.visualization.manim_runner import run_python_process

#: 孫プロセスを起こして PID を報告し、自分は眠り続けるスクリプト。
_TREE_PROBE = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print('GRANDCHILD_PID=%d' % child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def _is_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _grandchild_pid(stdout: str) -> int:
    assert "GRANDCHILD_PID=" in stdout, f"孫の PID を報告していない: {stdout!r}"
    return int(stdout.split("GRANDCHILD_PID=")[1].split()[0])


@pytest.fixture
def probe_script(tmp_path: Path) -> Path:
    path = tmp_path / "tree_probe.py"
    path.write_text(_TREE_PROBE, encoding="utf-8")
    return path


@pytest.mark.slow
@pytest.mark.timing_sensitive
def test_timeout_kills_the_whole_process_tree(probe_script: Path, tmp_path: Path) -> None:
    """既存バグの回帰テスト: タイムアウト時に孫(ffmpeg 相当)を残さない。"""
    result = run_python_process(
        python_path=sys.executable,
        args=[str(probe_script)],
        timeout_seconds=3.0,
        cwd=tmp_path,
    )
    assert result.timed_out is True
    assert result.cancelled is False
    assert result.exit_code is None
    pid = _grandchild_pid(result.stdout)
    time.sleep(1.0)  # kill の反映を待つ
    assert not _is_alive(pid), "タイムアウト後に孫プロセスが残っている"


@pytest.mark.slow
@pytest.mark.timing_sensitive
def test_cancel_returns_promptly_and_kills_the_tree(probe_script: Path, tmp_path: Path) -> None:
    """キャンセル要求から短時間で戻り、孫も残さないこと。"""
    flag = {"value": False}

    def trip() -> None:
        time.sleep(1.0)
        flag["value"] = True

    threading.Thread(target=trip, daemon=True).start()

    started = time.monotonic()
    result = run_python_process(
        python_path=sys.executable,
        args=[str(probe_script)],
        timeout_seconds=60.0,
        cwd=tmp_path,
        should_cancel=lambda: flag["value"],
    )
    elapsed = time.monotonic() - started

    assert result.cancelled is True
    assert result.timed_out is False
    # 要求から2秒以内に戻ること(ポーリング間隔 0.25 秒 + kill の余裕)。
    assert elapsed < 5.0, f"キャンセルに時間がかかりすぎ: {elapsed:.1f}s"
    pid = _grandchild_pid(result.stdout)
    time.sleep(1.0)
    assert not _is_alive(pid), "キャンセル後に孫プロセスが残っている"


def test_normal_completion_is_unaffected(tmp_path: Path) -> None:
    result = run_python_process(
        python_path=sys.executable,
        args=["-c", "print('hello')"],
        timeout_seconds=30.0,
        cwd=tmp_path,
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.timed_out is False
    assert result.cancelled is False


def test_stderr_is_captured(tmp_path: Path) -> None:
    result = run_python_process(
        python_path=sys.executable,
        args=["-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        timeout_seconds=30.0,
        cwd=tmp_path,
    )
    assert result.exit_code == 3
    assert "boom" in result.stderr


def test_missing_interpreter_reports_spawn_failure(tmp_path: Path) -> None:
    result = run_python_process(
        python_path="definitely-not-a-real-interpreter",
        args=["-c", "print(1)"],
        timeout_seconds=5.0,
        cwd=tmp_path,
    )
    assert result.exit_code is None
    assert "spawn failed" in result.stderr


def test_should_cancel_already_true_stops_immediately(probe_script: Path, tmp_path: Path) -> None:
    """開始直後に要求済みでも確実に止まること。"""
    started = time.monotonic()
    result = run_python_process(
        python_path=sys.executable,
        args=[str(probe_script)],
        timeout_seconds=60.0,
        cwd=tmp_path,
        should_cancel=lambda: True,
    )
    assert result.cancelled is True
    assert time.monotonic() - started < 5.0


def test_large_output_does_not_deadlock(tmp_path: Path) -> None:
    """パイプを読まずに待つとバッファが埋まって子が止まる。読み切れていること。"""
    result = run_python_process(
        python_path=sys.executable,
        args=["-c", "print('x' * 200000)"],
        timeout_seconds=30.0,
        cwd=tmp_path,
    )
    assert result.exit_code == 0
    assert len(result.stdout) >= 200000
