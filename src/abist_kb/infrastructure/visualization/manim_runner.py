"""Python インタープリタの解決・サブプロセス実行・依存診断。

旧実装 `tools/lib/visualize-python.js` の移植。`tools/visualize/render_scene.py`
(Manim 0.19 に programmatic に render() する)と `tools/visualize/check_deps.py`
を子プロセスとして spawn し、UTF-8 を強制し、既定10分でタイムアウトさせ、
stdout の最終行を JSON としてパースする。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TAIL_LINES = 40
_TAIL_MAX_CHARS = 8000
_DEPS_TIMEOUT_SECONDS = 30.0

#: `tools/visualize/` はリポジトリルート直下(`<repo>/tools/visualize/render_scene.py`)。
_REPO_ROOT = Path(__file__).resolve().parents[4]


def default_timeout_seconds() -> float:
    raw = os.environ.get("KB_VISUALIZE_TIMEOUT_MS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value / 1000.0
        except ValueError:
            pass
    return 10 * 60.0


def tail(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    joined = "\n".join(lines[-_TAIL_LINES:])
    return joined[-_TAIL_MAX_CHARS:] if len(joined) > _TAIL_MAX_CHARS else joined


def resolve_python(*, root: Path | None = None, env: dict[str, str] | None = None) -> str | None:
    """解決順: KB_VISUALIZE_PYTHON → <root>/.venv-visualize の python → py ランチャー。

    見つからなければ None(呼び出し側で PYTHON_NOT_FOUND にする)。
    """
    root = root or _REPO_ROOT
    env = env if env is not None else os.environ
    override = env.get("KB_VISUALIZE_PYTHON")
    if override:
        return override
    venv_python = (
        root / ".venv-visualize" / "Scripts" / "python.exe"
        if os.name == "nt"
        else root / ".venv-visualize" / "bin" / "python"
    )
    if venv_python.exists():
        return str(venv_python)
    return "py" if os.name == "nt" else "python3"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


def run_python_process(
    *, python_path: str, args: list[str], timeout_seconds: float, cwd: Path | None = None
) -> ProcessResult:
    """Python スクリプトを実行して結果を返す。render_scene のテストでは DI で差し替える。"""
    cwd = cwd or _REPO_ROOT
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        completed = subprocess.run(
            [python_path, *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
        )
    except FileNotFoundError as exc:
        return ProcessResult(
            exit_code=None, stdout="", stderr=f"spawn failed: {exc}", timed_out=False
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return ProcessResult(exit_code=None, stdout=stdout, stderr=stderr, timed_out=True)
    return ProcessResult(
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        timed_out=False,
    )


def parse_last_json_line(stdout: str) -> dict[str, Any] | None:
    """stdout の最終 JSON 行をパースする(無ければ None)。"""
    lines = [line for line in stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def check_visualize_deps(
    *,
    root: Path | None = None,
    python_path: str | None = None,
    run_process: Any = run_python_process,
) -> dict[str, Any]:
    """Python / Manim / ffmpeg / フォントの依存診断。

    依存が欠けていても ok:true(診断自体は成功)。ready で描画可否を判定する。
    """
    root = root or _REPO_ROOT
    python = python_path if python_path is not None else resolve_python(root=root)
    setup_hint = (
        "セットアップ: py -3.11 -m venv .venv-visualize && "
        ".venv-visualize\\Scripts\\python.exe -m pip install -r requirements-visualize.txt"
    )

    if not python:
        return {
            "ok": True,
            "ready": False,
            "python": {"found": False},
            "manim": {"found": False},
            "ffmpeg": {"found": False},
            "messages": [
                "Python が見つかりません"
                f"(python は Store スタブのため py を導入してください)。{setup_hint}"
            ],
        }

    result = run_process(
        python_path=python,
        args=[str(root / "tools" / "visualize" / "check_deps.py")],
        timeout_seconds=_DEPS_TIMEOUT_SECONDS,
        cwd=root,
    )

    payload = parse_last_json_line(result.stdout)
    if not payload or result.exit_code != 0:
        return {
            "ok": True,
            "ready": False,
            "python": {"found": False, "path": python},
            "manim": {"found": False},
            "ffmpeg": {"found": False},
            "messages": [
                f"Python（{python}）で診断スクリプトを実行できませんでした: "
                f"{tail(result.stderr) or 'exit ' + str(result.exit_code)}",
                setup_hint,
            ],
        }

    messages: list[str] = []
    if not (payload.get("manim") or {}).get("found"):
        messages.append(f"Manim が見つかりません。{setup_hint}")
    if not (payload.get("ffmpeg") or {}).get("found"):
        messages.append(
            "ffmpeg が見つかりません。winget install Gyan.FFmpeg.Essentials 等で導入してください"
        )
    fonts = payload.get("fonts")
    if fonts and fonts.get("available") is False:
        messages.append(
            f"フォント「{fonts.get('requested')}」が見つかりません"
            "（Pango のフォールバックで描画は続行されます）"
        )

    python_info = payload.get("python") or {"found": False}
    manim_info = payload.get("manim") or {"found": False}
    ffmpeg_info = payload.get("ffmpeg") or {"found": False}
    ready = bool(python_info.get("found") and manim_info.get("found") and ffmpeg_info.get("found"))
    return {
        "ok": True,
        "ready": ready,
        "python": python_info,
        "manim": manim_info,
        "ffmpeg": ffmpeg_info,
        "fonts": fonts,
        "messages": messages,
    }


_UNSET = object()
_cached_ffmpeg_version: str | None | object = _UNSET


def ffmpeg_version() -> str | None:
    """ffmpeg のバージョン先頭行(取得できなければ None)。プロセス内でキャッシュする。"""
    global _cached_ffmpeg_version
    if _cached_ffmpeg_version is not _UNSET:
        return _cached_ffmpeg_version  # type: ignore[return-value]
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        _cached_ffmpeg_version = None
        return None
    try:
        completed = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            shell=False,
        )
        first_line = (completed.stdout or "").splitlines()
        _cached_ffmpeg_version = first_line[0] if first_line else None
    except (OSError, subprocess.SubprocessError):
        _cached_ffmpeg_version = None
    return _cached_ffmpeg_version  # type: ignore[return-value]


__all__ = [
    "ProcessResult",
    "check_visualize_deps",
    "default_timeout_seconds",
    "ffmpeg_version",
    "parse_last_json_line",
    "resolve_python",
    "run_python_process",
    "tail",
]
