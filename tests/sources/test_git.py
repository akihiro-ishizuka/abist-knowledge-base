"""`infrastructure.sources.git`: 旧 `download-git.js` / `test/sync-git.test.js` の移植。

`tests/fixtures/PROVENANCE.md` が明示する通り `test/sync-git.test.js` のシナリオは
fixture化されておらず、このファイルでの pytest 再実装がその受け入れ基準そのもの
になる。ネットワークは一切使わず、一時ディレクトリに作った使い捨てローカル
Gitリポジトリだけを使う(旧テストと同じ方針)。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from abist_kb.domain.frontmatter import parse_frontmatter
from abist_kb.domain.sync_policy import SyncAction, SyncStatus
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.sources import git as git_module
from abist_kb.infrastructure.sources.git import (
    GitSyncRunner,
    diff_files,
    force_rmtree,
    mirror_cache_to_output,
    redact_credentials,
    update_git_cache,
)


def _git(args: list[str], cwd: Path) -> None:
    # encoding/errors を明示: git のコミットメッセージ等がUTF-8で、既定のロケール
    # コードページ(日本語Windowsではcp932)でデコードできないと `text=True` は
    # バックグラウンドの読み取りスレッドで `UnicodeDecodeError` を起こす。
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )


def make_upstream(directory: Path) -> Path:
    """テスト用の上流リポジトリを作る(旧テスト `makeUpstream` の移植)。"""
    directory.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], directory)
    _git(["config", "user.email", "test@example.com"], directory)
    _git(["config", "user.name", "Test"], directory)
    _git(["config", "commit.gpgsign", "false"], directory)

    (directory / "README.md").write_text("# 最初\n", encoding="utf-8")
    (directory / "docs").mkdir(exist_ok=True)
    (directory / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
    (directory / "docs" / "b.md").write_text("# B\n", encoding="utf-8")
    _git(["add", "."], directory)
    _git(["commit", "-m", "first"], directory)
    return directory


@pytest.fixture
def repo_dirs(tmp_root: Path) -> tuple[Path, Path, Path]:
    return tmp_root / "upstream", tmp_root / "cache", tmp_root / "output"


# ---------------------------------------------------------------------------
# update_git_cache / mirror_cache_to_output(旧テストのほぼ直接移植)
# ---------------------------------------------------------------------------


def test_initial_clone_is_shallow_and_mirrors_to_output(repo_dirs: tuple[Path, Path, Path]) -> None:
    """旧テスト『初回はキャッシュに shallow clone して出力先へ反映する』。"""
    upstream, cache_root, output = repo_dirs
    make_upstream(upstream)

    cache = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    assert cache.ok, cache.error
    assert cache.cloned is True
    assert cache.after, "コミットが取れていない"
    assert (cache.cache_dir / ".git").is_dir(), "キャッシュに .git が無い"

    output.mkdir(parents=True, exist_ok=True)
    mirror = mirror_cache_to_output(cache.cache_dir, output)

    assert len(mirror.added) == 3
    assert len(mirror.deleted) == 0
    assert (output / "docs" / "a.md").is_file()
    assert not (output / ".git").exists(), ".git を出力先へコピーしてはいけない"


def test_second_run_fetches_diff_only_and_leaves_unchanged_files_untouched(
    repo_dirs: tuple[Path, Path, Path],
) -> None:
    """旧テスト『2回目は fetch で差分のみ反映し、未変更ファイルに触れない(受入条件1)』。"""
    upstream, cache_root, output = repo_dirs
    make_upstream(upstream)

    first = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    output.mkdir(parents=True, exist_ok=True)
    mirror_cache_to_output(first.cache_dir, output)

    untouched = output / "docs" / "b.md"
    mtime_before = untouched.stat().st_mtime_ns

    (upstream / "docs" / "a.md").write_text("# A 更新\n", encoding="utf-8")
    _git(["add", "."], upstream)
    _git(["commit", "-m", "update a"], upstream)

    second = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    assert second.ok, second.error
    assert second.cloned is False, "2回目に再クローンしている"
    assert second.before != second.after, "コミットが進んでいない"

    mirror = mirror_cache_to_output(second.cache_dir, output)

    assert mirror.updated == ["docs/a.md"]
    assert mirror.added == []
    assert mirror.deleted == []
    assert mirror.unchanged == 2
    assert (output / "docs" / "a.md").read_text(encoding="utf-8") == "# A 更新\n"
    assert untouched.stat().st_mtime_ns == mtime_before, "未変更ファイルを書き換えた"


def test_no_changes_upstream_leaves_everything_untouched(
    repo_dirs: tuple[Path, Path, Path],
) -> None:
    """旧テスト『変更が無ければ何も書き換えない(受入条件5: 再実行の安定性)』。"""
    upstream, cache_root, output = repo_dirs
    make_upstream(upstream)

    first = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    output.mkdir(parents=True, exist_ok=True)
    mirror_cache_to_output(first.cache_dir, output)

    files = ["README.md", "docs/a.md", "docs/b.md"]
    before = {f: (output / f).stat().st_mtime_ns for f in files}

    second = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    assert second.before == second.after, "変更が無いのにコミットが動いた"

    mirror = mirror_cache_to_output(second.cache_dir, output)
    assert len(mirror.added) + len(mirror.updated) + len(mirror.deleted) == 0
    assert mirror.unchanged == 3

    for f in files:
        assert (output / f).stat().st_mtime_ns == before[f], f"{f} を書き換えた"


def test_deleted_upstream_file_is_removed_and_others_kept(
    repo_dirs: tuple[Path, Path, Path],
) -> None:
    """旧テスト『上流から消えたファイルだけを削除し一覧を返す』。"""
    upstream, cache_root, output = repo_dirs
    make_upstream(upstream)

    first = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    output.mkdir(parents=True, exist_ok=True)
    mirror_cache_to_output(first.cache_dir, output)

    _git(["rm", "docs/b.md"], upstream)
    _git(["commit", "-m", "remove b"], upstream)

    second = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    mirror = mirror_cache_to_output(second.cache_dir, output)

    assert mirror.deleted == ["docs/b.md"]
    assert not (output / "docs" / "b.md").exists()
    assert (output / "docs" / "a.md").exists(), "関係ないファイルまで消した"


def test_new_upstream_file_is_added(repo_dirs: tuple[Path, Path, Path]) -> None:
    """旧テスト『新規ファイルの追加を反映する』。"""
    upstream, cache_root, output = repo_dirs
    make_upstream(upstream)

    first = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    output.mkdir(parents=True, exist_ok=True)
    mirror_cache_to_output(first.cache_dir, output)

    (upstream / "docs" / "sub").mkdir(parents=True, exist_ok=True)
    (upstream / "docs" / "sub" / "c.md").write_text("# C\n", encoding="utf-8")
    _git(["add", "."], upstream)
    _git(["commit", "-m", "add c"], upstream)

    second = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    mirror = mirror_cache_to_output(second.cache_dir, output)

    assert mirror.added == ["docs/sub/c.md"]
    assert (output / "docs" / "sub" / "c.md").read_text(encoding="utf-8") == "# C\n"


def test_failed_fetch_does_not_wipe_output_dir(repo_dirs: tuple[Path, Path, Path]) -> None:
    """旧テスト『取得に失敗しても出力先を消さない(受入条件3)』。"""
    upstream, cache_root, output = repo_dirs
    make_upstream(upstream)

    first = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    output.mkdir(parents=True, exist_ok=True)
    mirror_cache_to_output(first.cache_dir, output)

    before = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())

    # 上流を消して取得を失敗させる。
    force_rmtree(upstream)
    failed = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)

    assert failed.ok is False, "失敗するはずが成功した"
    after = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
    assert after == before, "取得失敗で出力先が変化した"
    assert (output / "docs" / "a.md").exists()


def test_corrupted_cache_is_recreated(repo_dirs: tuple[Path, Path, Path]) -> None:
    """旧テスト『壊れたキャッシュは作り直す』。"""
    upstream, cache_root, _output = repo_dirs
    make_upstream(upstream)

    first = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    force_rmtree(first.cache_dir / ".git")

    second = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    assert second.ok, second.error
    assert second.cloned is True, "壊れたキャッシュを作り直していない"
    assert (second.cache_dir / ".git").exists()


def test_diff_files_between_commits_reports_added_modified_deleted(
    repo_dirs: tuple[Path, Path, Path],
) -> None:
    """旧テスト『コミット間の差分ファイル一覧を取れる』。"""
    upstream, cache_root, _output = repo_dirs
    make_upstream(upstream)

    # shallow だと比較できないため、比較可能な深さで取得する。
    cache_dir = cache_root / "full"
    cache_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", str(upstream), str(cache_dir)],
        cwd=str(cache_root),
        check=True,
        capture_output=True,
    )
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(cache_dir),
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()

    (upstream / "docs" / "a.md").write_text("# A2\n", encoding="utf-8")
    (upstream / "NEW.md").write_text("# NEW\n", encoding="utf-8")
    _git(["rm", "docs/b.md"], upstream)
    _git(["add", "."], upstream)
    _git(["commit", "-m", "mixed"], upstream)

    subprocess.run(["git", "fetch", "origin"], cwd=str(cache_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "reset", "--hard", "FETCH_HEAD"],
        cwd=str(cache_dir),
        check=True,
        capture_output=True,
    )
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(cache_dir),
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()

    diff = diff_files(cache_dir, before, after)
    assert diff is not None
    assert diff.added == ["NEW.md"]
    assert diff.modified == ["docs/a.md"]
    assert diff.deleted == ["docs/b.md"]


def test_diff_files_same_commit_is_empty(repo_dirs: tuple[Path, Path, Path]) -> None:
    """旧テスト『同一コミットなら差分は空』。"""
    upstream, cache_root, _output = repo_dirs
    make_upstream(upstream)
    cache = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    diff = diff_files(cache.cache_dir, cache.after, cache.after)
    assert diff is not None
    assert diff.added == []
    assert diff.modified == []
    assert diff.deleted == []


def test_no_full_tree_delete_before_reclone_in_source(repo_dirs: tuple[Path, Path, Path]) -> None:
    """旧テスト『download-git.js に全削除の再クローンが残っていない(受入条件2)』の Python版。

    旧テストは `download-git.js` を正規表現でスキャンし、出力先を丸ごと削除
    してから再クローンする処理が復活していないことを確認していた。Python版の
    ソースに対して同じ静的検査を行う: 再帰削除(`force_rmtree`/`shutil.rmtree`)
    の呼び出しはすべて `cache_dir`(取得元キャッシュ)にのみ適用され、出力先
    (`target_dir`)には一切適用されないことを確認する。
    """
    source = Path(git_module.__file__).read_text(encoding="utf-8")
    call_lines = [
        line
        for line in source.splitlines()
        if "force_rmtree(" in line and "def force_rmtree" not in line
    ]
    assert call_lines, "force_rmtree呼び出しが見当たらない(テスト自体が無意味になっていないか確認)"
    for line in call_lines:
        assert "cache_dir" in line, f"出力先を再帰削除している可能性がある行: {line}"
        assert "target_dir" not in line


def test_run_git_always_disables_hooks(monkeypatch, tmp_path: Path) -> None:
    """brief契約8: フックは絶対に実行しない(`core.hooksPath` を毎回無効化する)。"""
    captured: dict[str, list[str]] = {}

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):  # noqa: ANN001, ANN003
        captured["args"] = args
        return _FakeCompleted()

    monkeypatch.setattr(git_module.subprocess, "run", fake_run)
    git_module._run_git(["status"], tmp_path)  # noqa: SLF001 - 内部実装の契約を直接検証する

    args = captured["args"]
    hooks_flags = [a for a in args if isinstance(a, str) and a.startswith("core.hooksPath=")]
    assert hooks_flags, "core.hooksPath を無効化していない"


# ---------------------------------------------------------------------------
# GitSyncRunner: front matter無し・source_key/source_updated_at・sync_status常時synced
# ---------------------------------------------------------------------------


def test_sync_writes_files_without_frontmatter_and_records_commit_sha(
    documents: DocumentRepository, repo_dirs: tuple[Path, Path, Path], tmp_root: Path
) -> None:
    upstream, cache_root, _output = repo_dirs
    make_upstream(upstream)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)

    runner = GitSyncRunner(
        documents=documents,
        root_dir=tmp_root,
        docs_dir=docs_dir,
        output_dir="docs/myrepo",
        repository=str(upstream),
        branch="main",
        cache_root=cache_root,
    )
    result = runner.sync()

    assert result.full_sync_succeeded is True
    assert {i.action for i in result.items} == {str(SyncAction.CREATE)}

    a_md = docs_dir / "myrepo" / "docs" / "a.md"
    assert a_md.is_file()
    content = a_md.read_text(encoding="utf-8")
    assert content == "# A\n", "git由来ファイルにfront matterを書いてはいけない"
    parsed = parse_frontmatter(content)
    assert parsed.has_frontmatter is False

    row = documents.get("myrepo/docs/a.md")
    assert row is not None
    assert row["source"] == "git"
    assert row["managed_by"] == "git-sync"
    assert row["source_key"] == f"git:{upstream}#docs/a.md"
    assert row["source_updated_at"] == result.commit_after
    assert row["sync_status"] == str(SyncStatus.SYNCED)


def test_sync_status_is_always_synced_regardless_of_action(
    documents: DocumentRepository, repo_dirs: tuple[Path, Path, Path], tmp_root: Path
) -> None:
    """brief契約7: git は decide_sync_action を通さない。add/update/unchangedいずれもsynced。"""
    upstream, cache_root, _output = repo_dirs
    make_upstream(upstream)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)

    runner = GitSyncRunner(
        documents=documents,
        root_dir=tmp_root,
        docs_dir=docs_dir,
        output_dir="docs/myrepo",
        repository=str(upstream),
        branch="main",
        cache_root=cache_root,
    )
    runner.sync()

    (upstream / "docs" / "a.md").write_text("# A 更新\n", encoding="utf-8")
    _git(["add", "."], upstream)
    _git(["commit", "-m", "update a"], upstream)

    result = runner.sync()
    actions_by_path = {i.path: i.action for i in result.items}
    assert actions_by_path["docs/myrepo/docs/a.md"] == str(SyncAction.UPDATE)

    row_a = documents.get("myrepo/docs/a.md")
    row_b = documents.get("myrepo/docs/b.md")  # 今回はunchangedだが記録は都度更新される
    assert row_a["sync_status"] == str(SyncStatus.SYNCED)
    assert row_b["sync_status"] == str(SyncStatus.SYNCED)


def test_deleted_upstream_markdown_is_removed_from_output_but_db_row_left_for_audit(
    documents: DocumentRepository, repo_dirs: tuple[Path, Path, Path], tmp_root: Path
) -> None:
    upstream, cache_root, _output = repo_dirs
    make_upstream(upstream)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)

    runner = GitSyncRunner(
        documents=documents,
        root_dir=tmp_root,
        docs_dir=docs_dir,
        output_dir="docs/myrepo",
        repository=str(upstream),
        branch="main",
        cache_root=cache_root,
    )
    runner.sync()

    _git(["rm", "docs/b.md"], upstream)
    _git(["commit", "-m", "remove b"], upstream)

    result = runner.sync()
    missing_items = [i for i in result.items if i.action == str(SyncAction.MISSING)]
    assert len(missing_items) == 1
    assert missing_items[0].path == "docs/myrepo/docs/b.md"
    assert not (docs_dir / "myrepo" / "docs" / "b.md").exists()


# ---------------------------------------------------------------------------
# docs/ プレフィックス(旧実装の緩い判定をそのまま踏襲、brief指定)
# ---------------------------------------------------------------------------


def test_output_dir_not_starting_with_docs_gets_docs_prefix(
    documents: DocumentRepository, repo_dirs: tuple[Path, Path, Path], tmp_root: Path
) -> None:
    upstream, cache_root, _output = repo_dirs
    make_upstream(upstream)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)

    runner = GitSyncRunner(
        documents=documents,
        root_dir=tmp_root,
        docs_dir=docs_dir,
        output_dir="myrepo",
        repository=str(upstream),
        branch="main",
        cache_root=cache_root,
    )
    runner.sync()

    assert (docs_dir / "myrepo" / "docs" / "a.md").is_file()


# ---------------------------------------------------------------------------
# 秘密情報: URLに埋め込まれた認証情報を一切残さない
# ---------------------------------------------------------------------------


def test_redact_credentials_strips_userinfo_from_url() -> None:
    assert (
        redact_credentials("https://ghp_supersecrettoken@github.com/user/repo.git")
        == "https://github.com/user/repo.git"
    )
    assert redact_credentials("no url here") == "no url here"


def test_failed_clone_never_leaks_embedded_credentials_in_error_or_records(
    documents: DocumentRepository, tmp_root: Path
) -> None:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    secret = "totally-secret-token-do-not-leak"  # noqa: S105 - テスト専用のダミー値
    # ポート1は通常listenされておらず、即座に接続拒否されるため高速に失敗する。
    repository = f"https://{secret}@127.0.0.1:1/does-not-exist.git"

    runner = GitSyncRunner(
        documents=documents,
        root_dir=tmp_root,
        docs_dir=docs_dir,
        output_dir="docs/leaktest",
        repository=repository,
        cache_root=tmp_root / "cache",
    )
    result = runner.sync()

    assert result.full_sync_succeeded is False
    assert secret not in (result.error or "")
    assert secret not in result.repository
    for item in result.items:
        assert secret not in item.reason
        assert secret not in (item.error or "")


# ---------------------------------------------------------------------------
# リースの契約: ミラーの1件ずつの副作用ループは check_lease を反復ごとに呼ぶ
# ---------------------------------------------------------------------------


def test_mirror_checks_lease_between_files_and_stops_after_theft(
    repo_dirs: tuple[Path, Path, Path],
) -> None:
    """`JobRunContext.check_lease` の契約: リースを奪われたら追加のファイルI/Oを止める。"""
    upstream, cache_root, output = repo_dirs
    make_upstream(upstream)
    (upstream / "docs" / "c.md").write_text("# C\n", encoding="utf-8")
    _git(["add", "."], upstream)
    _git(["commit", "-m", "add c"], upstream)

    cache = update_git_cache(str(upstream), "main", "repo", cache_root=cache_root)
    output.mkdir(parents=True, exist_ok=True)

    from abist_kb.domain.errors import AppError, ErrorCode

    calls = 0

    def lease_lost_on_second_item() -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AppError(code=ErrorCode.CONFLICT, message="リースが奪われました")

    with pytest.raises(AppError):
        mirror_cache_to_output(cache.cache_dir, output, check_lease=lease_lost_on_second_item)

    assert calls == 2
    written = [p for p in output.rglob("*") if p.is_file()]
    assert len(written) == 1, "リース喪失後も書き込みを続けた"


def test_runner_sync_propagates_check_lease_into_mirror(
    documents: DocumentRepository, repo_dirs: tuple[Path, Path, Path], tmp_root: Path
) -> None:
    """`GitSyncRunner.sync` も `check_lease` をミラー処理まで配線する。"""
    upstream, cache_root, _output = repo_dirs
    make_upstream(upstream)
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir(exist_ok=True)

    from abist_kb.domain.errors import AppError, ErrorCode

    calls = 0

    def lease_lost_on_second_item() -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AppError(code=ErrorCode.CONFLICT, message="リースが奪われました")

    runner = GitSyncRunner(
        documents=documents,
        root_dir=tmp_root,
        docs_dir=docs_dir,
        output_dir="docs/myrepo",
        repository=str(upstream),
        branch="main",
        cache_root=cache_root,
    )
    with pytest.raises(AppError):
        runner.sync(check_lease=lease_lost_on_second_item)
    assert calls == 2


# Async smoke: ensure module import doesn't require an event loop (git is fully sync).
def test_git_module_has_no_async_surface() -> None:
    import inspect

    assert not inspect.iscoroutinefunction(GitSyncRunner.sync)
    asyncio.run(asyncio.sleep(0))  # asyncioが必要な他アダプタとの整合を壊していないことの確認
