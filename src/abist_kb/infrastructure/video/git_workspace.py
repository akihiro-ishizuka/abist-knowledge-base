"""キャプチャ対象リポジトリの取得（**読み取り専用**）。

`resolved_commit_sha` を返すことがこの層の存在理由。「どの時点のアプリ画面か」を
後から追えないと、古い画面のまま配信し続ける事故になる。

**push / commit は絶対に行わない。** 許可するサブコマンドを allowlist で固定し、
それ以外は実行前に例外にする（テストがコマンド列を検査する）。
認証は既存の git 資格情報に委ね、トークンを spec にも manifest にもログにも書かない。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from abist_kb.infrastructure.visualization.manim_runner import run_python_process, tail

#: 実行を許可する git サブコマンド。**書き込み系は1つも入れない。**
ALLOWED_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"clone", "fetch", "checkout", "rev-parse", "config", "init", "remote"}
)
#: 明示的に拒否するサブコマンド（allowlist と二重で守る。意図の記録も兼ねる）。
FORBIDDEN_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"push", "commit", "add", "merge", "rebase", "tag", "am", "apply", "reset", "clean"}
)

#: 1コマンドあたりのタイムアウト（秒）。clone は大きいリポジトリを想定して長め。
DEFAULT_GIT_TIMEOUT_SECONDS = 10 * 60.0

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitCommandNotAllowedError(RuntimeError):
    """allowlist にないサブコマンドを実行しようとした。"""


@dataclass(frozen=True, slots=True)
class GitResult:
    ok: bool
    workspace: Path | None = None
    resolved_commit_sha: str | None = None
    code: str | None = None
    message: str | None = None
    commands: list[list[str]] | None = None


def git_path() -> str | None:
    return shutil.which("git")


def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
):
    """git を実行する（allowlist 検査つき）。

    `manim_runner.run_python_process` を使うのは、タイムアウト時に
    **プロセスツリーごと**終了させるため（git は子プロセスを持つ）。
    """
    if not args:
        raise GitCommandNotAllowedError("git のサブコマンドが指定されていません")
    subcommand = args[0]
    if subcommand in FORBIDDEN_GIT_SUBCOMMANDS or subcommand not in ALLOWED_GIT_SUBCOMMANDS:
        raise GitCommandNotAllowedError(
            f"git {subcommand} は実行できません（読み取り専用の allowlist: "
            f"{' / '.join(sorted(ALLOWED_GIT_SUBCOMMANDS))}）"
        )
    executable = git_path()
    if executable is None:
        raise FileNotFoundError("git が PATH にありません")
    return run_python_process(
        python_path=executable,
        args=args,
        timeout_seconds=timeout_seconds,
        cwd=cwd,
    )


def prepare(
    repo_url: str,
    ref: str,
    workspace: Path,
    *,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> GitResult:
    """`workspace` に repo を用意し、`ref` を checkout して commit SHA を返す。

    既存の作業ツリーがあれば `fetch` で更新する（毎回 clone し直さない）。
    失敗しても例外にせず `GitResult(ok=False, code=...)` を返す —— キャプチャは
    任意機能であり、ここで落として動画生成を止めてはいけない。
    """
    commands: list[list[str]] = []

    def record(args: list[str]) -> None:
        commands.append(list(args))

    if git_path() is None:
        return GitResult(
            ok=False,
            code="GIT_UNAVAILABLE",
            message="git が PATH にありません。キャプチャを省略して動画生成を続けます",
            commands=commands,
        )

    workspace = workspace.resolve()
    try:
        if (workspace / ".git").is_dir():
            args = ["fetch", "--depth", "1", "origin", ref]
            record(args)
            fetched = run_git(args, cwd=workspace, timeout_seconds=timeout_seconds)
            if fetched.exit_code != 0:
                return GitResult(
                    ok=False,
                    code="GIT_REF_NOT_FOUND",
                    message=f"ref '{ref}' を取得できませんでした",
                    commands=commands,
                )
            checkout = ["checkout", "--force", "FETCH_HEAD"]
        else:
            workspace.parent.mkdir(parents=True, exist_ok=True)
            args = ["clone", "--depth", "1", "--branch", ref, repo_url, str(workspace)]
            record(args)
            cloned = run_git(args, timeout_seconds=timeout_seconds)
            if cloned.exit_code != 0:
                # branch/tag ではなく commit SHA を渡された場合は --branch が効かない
                fallback = ["clone", repo_url, str(workspace)]
                record(fallback)
                cloned = run_git(fallback, timeout_seconds=timeout_seconds)
                if cloned.exit_code != 0:
                    return GitResult(
                        ok=False,
                        code="GIT_CLONE_FAILED",
                        message=f"clone に失敗しました: {tail(cloned.stderr)}",
                        commands=commands,
                    )
            checkout = ["checkout", "--force", ref]

        record(checkout)
        checked = run_git(checkout, cwd=workspace, timeout_seconds=timeout_seconds)
        if checked.exit_code != 0:
            return GitResult(
                ok=False,
                code="GIT_REF_NOT_FOUND",
                message=f"ref '{ref}' を checkout できませんでした",
                commands=commands,
            )

        args = ["rev-parse", "HEAD"]
        record(args)
        resolved = run_git(args, cwd=workspace, timeout_seconds=60.0)
        sha = (resolved.stdout or "").strip()
        if resolved.exit_code != 0 or not _SHA_RE.match(sha):
            return GitResult(
                ok=False,
                code="GIT_REF_NOT_FOUND",
                message="commit SHA を解決できませんでした",
                commands=commands,
            )
    except (GitCommandNotAllowedError, FileNotFoundError, OSError) as exc:
        return GitResult(ok=False, code="GIT_UNAVAILABLE", message=str(exc), commands=commands)

    return GitResult(ok=True, workspace=workspace, resolved_commit_sha=sha, commands=commands)


__all__ = [
    "ALLOWED_GIT_SUBCOMMANDS",
    "DEFAULT_GIT_TIMEOUT_SECONDS",
    "FORBIDDEN_GIT_SUBCOMMANDS",
    "GitCommandNotAllowedError",
    "GitResult",
    "git_path",
    "prepare",
    "run_git",
]
