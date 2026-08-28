"""Phase 7: 画面キャプチャ（プロファイルレジストリ / git 読み取り専用 / 後始末）。

このテストが守る性質は3つ:

1. **MCP/API から生の起動コマンドを受け取らない。** 入口はプロファイル名だけ
2. **既定で無効。** 環境変数を明示しない限り動かない
3. **失敗しても動画生成を止めない。** 例外ではなくコード付きの結果を返す
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from abist_kb.application.video import capture_planner
from abist_kb.domain.capture_spec import (
    is_capture_enabled,
    validate_capture_profile,
)
from abist_kb.infrastructure.video import git_workspace, screen_capture

ENABLED = {"ABIST_KB_VIDEO_CAPTURE_ENABLED": "1"}


def _profile_payload(**overrides) -> dict:
    payload = {
        "repo": {"url": "https://example.invalid/app.git", "ref": "main"},
        "launch": {
            "type": "web",
            "command": ["python", "-m", "http.server", "8765"],
            "url": "http://127.0.0.1:8765/",
            "ready_timeout_sec": 30,
        },
        "shots": [{"id": "home", "scene_id": "s04", "mask": ["#user-name"]}],
    }
    payload.update(overrides)
    return payload


def _write_registry(tmp_path: Path, profiles: dict) -> Path:
    path = tmp_path / "capture-profiles.json"
    path.write_text(json.dumps({"profiles": profiles}, ensure_ascii=False), encoding="utf-8")
    return path


class TestCaptureEnabledFlag:
    def test_disabled_by_default(self) -> None:
        assert is_capture_enabled({}) is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_explicit_opt_in(self, value) -> None:
        assert is_capture_enabled({"ABIST_KB_VIDEO_CAPTURE_ENABLED": value}) is True

    @pytest.mark.parametrize("value", ["", "0", "false", "maybe", " "])
    def test_anything_else_stays_off(self, value) -> None:
        """紛らわしい値は**無効に倒す**（誤って有効になる方が危険）。"""
        assert is_capture_enabled({"ABIST_KB_VIDEO_CAPTURE_ENABLED": value}) is False


class TestProfileValidation:
    def test_valid_profile(self) -> None:
        result = validate_capture_profile("pattern-editor", _profile_payload())
        assert result.ok, [e.to_dict() for e in result.errors]
        assert result.profile is not None
        assert result.profile.command == ("python", "-m", "http.server", "8765")

    def test_command_must_be_a_list_not_a_string(self) -> None:
        """文字列を許すとシェル解釈が必要になり、登録内容自体が注入面になる。"""
        payload = _profile_payload(
            launch={
                "type": "web",
                "command": "python -m http.server 8765",
                "url": "http://127.0.0.1:8765/",
            }
        )
        result = validate_capture_profile("x", payload)
        assert not result.ok
        assert any(e.path == "launch.command" for e in result.errors)

    def test_unknown_launch_type_is_rejected(self) -> None:
        payload = _profile_payload(
            launch={"type": "vnc", "command": ["a"], "url": "http://127.0.0.1/"}
        )
        result = validate_capture_profile("x", payload)
        assert not result.ok
        assert any(e.path == "launch.type" for e in result.errors)

    def test_non_https_repo_is_rejected(self) -> None:
        result = validate_capture_profile(
            "x", _profile_payload(repo={"url": "file:///etc", "ref": "main"})
        )
        assert not result.ok
        assert any(e.path == "repo.url" for e in result.errors)

    def test_dangerous_ref_is_rejected(self) -> None:
        result = validate_capture_profile(
            "x", _profile_payload(repo={"url": "https://e.invalid/a.git", "ref": "main; rm -rf /"})
        )
        assert not result.ok
        assert any(e.path == "repo.ref" for e in result.errors)

    def test_shots_are_required(self) -> None:
        result = validate_capture_profile("x", _profile_payload(shots=[]))
        assert not result.ok
        assert any(e.path == "shots" for e in result.errors)

    def test_duplicate_shot_id_is_rejected(self) -> None:
        payload = _profile_payload(shots=[{"id": "home"}, {"id": "home"}])
        result = validate_capture_profile("x", payload)
        assert not result.ok
        assert any(e.code == "duplicate_id" for e in result.errors)

    @pytest.mark.parametrize("name", ["../evil", "UPPER", "", "a" * 100])
    def test_bad_profile_names_are_rejected(self, name) -> None:
        assert not validate_capture_profile(name, _profile_payload()).ok


class TestProfileResolution:
    def test_disabled_is_reported_not_raised(self, tmp_path: Path) -> None:
        registry = _write_registry(tmp_path, {"app": _profile_payload()})
        plan = capture_planner.resolve_profile(
            "app", repo_root=tmp_path, env={}, profiles_file=registry
        )
        assert not plan.ok
        assert plan.code == "CAPTURE_DISABLED"

    def test_unknown_profile_lists_the_registered_ones(self, tmp_path: Path) -> None:
        registry = _write_registry(tmp_path, {"app": _profile_payload()})
        plan = capture_planner.resolve_profile(
            "other", repo_root=tmp_path, env=ENABLED, profiles_file=registry
        )
        assert plan.code == "CAPTURE_PROFILE_NOT_FOUND"
        assert "app" in (plan.message or "")

    def test_missing_registry_is_not_an_error_path(self, tmp_path: Path) -> None:
        plan = capture_planner.resolve_profile(
            "app", repo_root=tmp_path, env=ENABLED, profiles_file=tmp_path / "nope.json"
        )
        assert plan.code == "CAPTURE_PROFILE_NOT_FOUND"

    def test_resolved_profile_carries_the_registered_command(self, tmp_path: Path) -> None:
        registry = _write_registry(tmp_path, {"app": _profile_payload()})
        plan = capture_planner.resolve_profile(
            "app", repo_root=tmp_path, env=ENABLED, profiles_file=registry
        )
        assert plan.ok and plan.profile is not None
        assert plan.profile.command[0] == "python"

    def test_list_profiles_does_not_leak_commands(self, tmp_path: Path) -> None:
        registry = _write_registry(tmp_path, {"app": _profile_payload()})
        listed = capture_planner.list_profiles(tmp_path, profiles_file=registry)
        assert listed == ["app"]
        assert all(isinstance(name, str) for name in listed)

    def test_run_capture_without_a_plan_returns_a_warning(self, tmp_path: Path) -> None:
        plan = capture_planner.CapturePlan(ok=False, code="CAPTURE_DISABLED", message="無効です")
        outcome = capture_planner.run_capture(plan, tmp_path)
        assert not outcome.ok
        assert outcome.code == "CAPTURE_DISABLED"
        assert outcome.warnings


class TestGitWorkspaceIsReadOnly:
    @pytest.mark.parametrize("subcommand", sorted(git_workspace.FORBIDDEN_GIT_SUBCOMMANDS))
    def test_write_subcommands_are_refused(self, subcommand) -> None:
        with pytest.raises(git_workspace.GitCommandNotAllowedError):
            git_workspace.run_git([subcommand, "origin", "main"])

    def test_allowlist_and_forbidden_do_not_overlap(self) -> None:
        assert not (git_workspace.ALLOWED_GIT_SUBCOMMANDS & git_workspace.FORBIDDEN_GIT_SUBCOMMANDS)

    def test_prepare_only_issues_read_only_commands(self, tmp_path: Path) -> None:
        """実際に走らせて、記録されたコマンド列に書き込み系が無いこと。"""
        result = git_workspace.prepare(
            "https://example.invalid/does-not-exist.git", "main", tmp_path / "ws"
        )
        assert not result.ok, "存在しない repo が成功してはいけない"
        for command in result.commands or []:
            assert command[0] in git_workspace.ALLOWED_GIT_SUBCOMMANDS
            assert command[0] not in git_workspace.FORBIDDEN_GIT_SUBCOMMANDS

    def test_prepare_resolves_a_commit_sha_from_a_local_repo(self, tmp_path: Path) -> None:
        """`resolved_commit_sha` が実際に 40 桁 hex で返ること（実 git で確認）。"""
        if git_workspace.git_path() is None:
            pytest.skip("git が PATH にない")
        origin = tmp_path / "origin"
        origin.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": __import__("os").environ.get("PATH", ""),
        }
        (origin / "readme.md").write_text("hello", encoding="utf-8")
        for args in (
            ["init", "-b", "main"],
            ["add", "."],
            ["commit", "-m", "init", "--no-gpg-sign"],
        ):
            subprocess.run(  # noqa: S603
                ["git", *args],  # noqa: S607
                cwd=origin,
                env=env,
                capture_output=True,
                check=True,
            )
        head = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=origin,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        result = git_workspace.prepare(str(origin), "main", tmp_path / "ws")
        assert result.ok, result.message
        assert result.resolved_commit_sha == head
        assert len(result.resolved_commit_sha) == 40


class TestProcessCleanup:
    def test_launched_kills_the_whole_tree(self, tmp_path: Path) -> None:
        """撮影プロセスの**子孫**が残らないこと（purring 7-3 と同型の検証）。

        親 python が子 python を起動し、親を放置したまま `launched` を抜ける。
        木 kill をしていなければ孫が生き残る。
        """
        child = tmp_path / "child.py"
        child.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
        parent = tmp_path / "parent.py"
        parent.write_text(
            "import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, {str(child)!r}])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(120)\n",
            encoding="utf-8",
        )

        from abist_kb.domain.capture_spec import CaptureProfile, Shot

        profile = CaptureProfile(
            name="t",
            repo_url="https://example.invalid/a.git",
            ref="main",
            launch_type="web",
            command=(sys.executable, str(parent)),
            url="http://127.0.0.1:1/",
            shots=(Shot(id="a"),),
        )
        with screen_capture.launched(profile, tmp_path) as process:
            grandchild_pid = int(process.stdout.readline().strip())
            parent_pid = process.pid
            assert _pid_alive(grandchild_pid), "孫が起動していない（前提が崩れている）"

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and (
            _pid_alive(parent_pid) or _pid_alive(grandchild_pid)
        ):
            time.sleep(0.2)
        assert not _pid_alive(parent_pid), "親プロセスが残っている"
        assert not _pid_alive(grandchild_pid), "孫プロセスが残っている（木 kill が効いていない）"


def _pid_alive(pid: int) -> bool:
    import os

    if os.name == "nt":
        completed = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],  # noqa: S607
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return str(pid) in (completed.stdout or "")
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True
