"""Git リポジトリソースアダプター(旧 `download-git.js` / `test/sync-git.test.js` の移植)。

`tests/fixtures/PROVENANCE.md` が明示する通り `test/sync-git.test.js` は
fixture化されておらず、このファイルでの pytest 再実装(`tests/sources/test_git.py`、
一時ローカルGitリポジトリのみを使いネットワーク不要)が受け入れ基準そのものに
なる。

**この移植が守る旧実装の契約(すべて実際の不具合・設計原則から生まれたもの)**:

1. **shallow clone → shallow fetch + reset の差分更新。** 旧実装の Issue #2
   Stage 2「全削除→再クローンを廃止」に対応する。`update_git_cache` が
   キャッシュを最新にし、`mirror_cache_to_output` が出力先へ**バイト比較の
   増分コピー**で反映する。
2. **変更の無いファイルには触れない(mtime も変えない)。** 内容が同じなら
   書き込みをスキップする(`mirror_cache_to_output` のバイト比較)。
3. **上流から消えたファイルだけを削除し、空ディレクトリを剪定する。**
4. **fetch/clone の失敗は出力先を一切変更しない。** 呼び出し側
   (`GitSyncRunner.sync`)はキャッシュ更新が失敗したら `mirror_cache_to_output`
   自体を呼ばない。旧実装には「全削除→再クローン」を退行させないことを保証する
   ソースgrepテストがあった(`test/sync-git.test.js` 受入条件2)。
5. **破損キャッシュ(`.git` 欠落等)は作り直す。**
6. **Git由来ファイルには front matter を書かない。** 次回のクローンで
   上書きされるうえ上流と乖離するため、メタデータは DB のみで持つ。
   `source_key` は `git:<repo>#<relfile>`、`source_updated_at` はコミットSHA。
7. **`sync_status` は常に `synced`。** esa/web と異なり `decide_sync_action`
   を一切通さない(`tests/fixtures/kernel/sync-status-for.json` で確認済みの
   旧実装の実測挙動、`domain.sync_policy` を意図的に迂回する)。
8. **フックは絶対に実行しない。** 取得したコンテンツはデータとして扱う
   (`_run_git` が毎回 `-c core.hooksPath=<存在しないパス>` を渡す)。
9. **URLに埋め込まれた認証情報はログ・DB・レポートへ一切出さない。**
   `redact_credentials` で `scheme://user:pass@host/...` の `user:pass@` 部分を
   常に除去してから保存・記録する。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.domain.frontmatter import hash_body, sha256_hex
from abist_kb.domain.metadata_schema import classify_document, extract_repo_name
from abist_kb.domain.sync_policy import SyncAction, SyncStatus
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.sources.base import docs_relative_path, ensure_within_docs

logger = logging.getLogger(__name__)

WarnFn = Callable[[str], None]
CheckLeaseFn = Callable[[], None]
EmitFn = Callable[..., None]

_GIT_TIMEOUT_SECONDS = 120.0

#: 実在しないパスを `core.hooksPath` に指定し、フックを一切実行させない
#: (取得したリポジトリのフックを実行してはいけない、brief 契約8)。
#: git はフック実行時に `<hooksPath>/<hook-name>` の存在を確認するだけなので、
#: このディレクトリ自体が存在しなくても安全に「フック無し」として動作する。
_DISABLED_HOOKS_PATH = str(
    Path(tempfile.gettempdir()) / "abist-kb-disabled-git-hooks-do-not-create"
)

_CREDENTIALS_IN_URL_RE = re.compile(r"://[^/@\s]+@")

_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
)  # fmt: skip


def force_rmtree(path: Path) -> None:
    """`shutil.rmtree` を、Windows で git の読み取り専用オブジェクトファイルが
    残っても確実に削除できるようにしたラッパー。

    git はパック済みオブジェクト等を読み取り専用属性で作成する。Windows の
    `shutil.rmtree` は読み取り専用ファイルの削除に失敗する(`ignore_errors=True`
    を付けても「一部だけ消えて残骸が残る」という危険な部分的失敗になり、直後の
    `git clone` が「ディレクトリが空でない」で失敗する不具合を実測で確認した)。
    ここでは削除失敗時に読み取り専用属性を外してから再試行する。
    """

    def _on_error(func: Any, target: str, exc_info: Any) -> None:  # noqa: ANN401
        try:
            Path(target).chmod(stat.S_IWRITE)
            func(target)
        except OSError:
            pass  # 消せなかった残骸は呼び出し側(次のclone)がまとめて検知する

    shutil.rmtree(path, onerror=_on_error)


def redact_credentials(text: str) -> str:
    """URL中の `user:pass@`/`token@` を除去する(brief 契約9: 秘密情報を一切残さない)。

    git のエラーメッセージ(`stderr`)はしばしば操作対象のURLをそのままエコーする
    ため、保存・記録する前に必ずこれを通す。
    """
    return _CREDENTIALS_IN_URL_RE.sub("://", text)


def sanitize_folder_name(name: str) -> str:
    """git 由来のフォルダ名を安全にする(旧実装 `sanitizeFolderName` の移植)。

    `domain.metadata_schema.sanitize_file_name` と同じ4置換 + Windows予約名
    対策を持つが、予約名の接頭辞が `repo-`(`sanitize_file_name` は `file-`)で
    あり、255バイト切詰も行わない、という旧実装固有の違いがあるため独立させる
    (`metadata_schema.py` の「安易に統合しない」原則と同じ理由)。
    """
    if not name:
        return "untitled"
    sanitized = re.sub(r'[<>:"/\\|?*]', "-", name)
    sanitized = re.sub(r"\s+", "-", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized)
    sanitized = re.sub(r"^-|-$", "", sanitized)
    if not sanitized:
        return "untitled"
    upper = sanitized.upper()
    is_reserved = any(
        upper == reserved or upper.startswith(reserved + ".")
        for reserved in _WINDOWS_RESERVED_NAMES
    )
    if is_reserved:
        sanitized = "repo-" + sanitized
    return sanitized


def with_docs_prefix_loose(directory: str) -> str:
    """出力先に `docs/` を付与する(旧実装 `download-git.js` :418-422 の移植)。

    esa/web(`infrastructure.sources.base.with_docs_prefix`)は `docs` 完全一致
    または `docs/` 始まりだけを「既に docs 配下」とみなす厳密な判定に矯正したが、
    task-5-brief は git についてこの旧実装の判定(`startsWith('docs')`、
    `docsx` のような偽陽性も許してしまう緩さがある)をそのまま踏襲するよう
    指定しているため、ここでは意図的に統一しない(report参照)。
    """
    normalized = directory.replace("\\", "/")
    if normalized.startswith("docs"):
        return normalized
    return f"docs/{normalized}"


@dataclass(slots=True)
class GitCommandResult:
    ok: bool
    code: int | None
    stdout: str
    stderr: str


def _run_git(
    args: list[str], cwd: Path, *, timeout: float = _GIT_TIMEOUT_SECONDS
) -> GitCommandResult:
    """git コマンドを実行する(shell 無し、フック無効化、認証情報はマスクして返す)。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # 認証プロンプトで無限にハングしない
    full_args = ["git", "-c", f"core.hooksPath={_DISABLED_HOOKS_PATH}", *args]
    try:
        completed = subprocess.run(  # noqa: S603 - 引数はリストで組み立てておりshell解釈させない
            full_args,
            cwd=str(cwd),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        return GitCommandResult(
            ok=False, code=None, stdout="", stderr=f"git の実行に失敗しました: {exc}"
        )
    except subprocess.TimeoutExpired:
        return GitCommandResult(ok=False, code=None, stdout="", stderr="git がタイムアウトしました")
    return GitCommandResult(
        ok=completed.returncode == 0,
        code=completed.returncode,
        stdout=redact_credentials(completed.stdout),
        stderr=redact_credentials(completed.stderr),
    )


def _command_error(action_label: str, result: GitCommandResult) -> str:
    return f"git {action_label} に失敗しました (code {result.code}): {result.stderr.strip()}"


def is_git_repo(path: Path) -> bool:
    return _run_git(["rev-parse", "--git-dir"], path).ok


def current_commit(path: Path) -> str | None:
    result = _run_git(["rev-parse", "HEAD"], path)
    return result.stdout.strip() if result.ok else None


def default_branch(path: Path) -> str:
    result = _run_git(["symbolic-ref", "--short", "HEAD"], path)
    return result.stdout.strip() if result.ok else "HEAD"


@dataclass(slots=True)
class GitCacheResult:
    """`update_git_cache` の戻り値(旧実装 `updateGitCache` の戻り値と同じ形)。"""

    ok: bool
    cache_dir: Path
    before: str | None
    after: str | None
    cloned: bool
    fetched: bool = False
    error: str | None = None


def update_git_cache(
    repository: str, branch: str | None, cache_name: str, *, cache_root: Path
) -> GitCacheResult:
    """キャッシュを最新にする(無ければ shallow clone、あれば shallow fetch + reset)。

    旧実装 `updateGitCache` の移植。**フェッチ/クローンが失敗しても出力先には
    一切触れない**(この関数は `cache_root` 配下しか書き込まない)。
    """
    cache_dir = cache_root / cache_name
    cache_root.mkdir(parents=True, exist_ok=True)

    if not is_git_repo(cache_dir):
        # 壊れた残骸があれば掃除してから新規クローン(brief契約5)。
        if cache_dir.exists():
            force_rmtree(cache_dir)
        args = ["clone", "--depth", "1"]
        if branch:
            args += ["--branch", branch]
        args += [repository, str(cache_dir)]
        result = _run_git(args, cache_root)
        if not result.ok:
            return GitCacheResult(
                ok=False,
                cache_dir=cache_dir,
                before=None,
                after=None,
                cloned=True,
                error=_command_error("clone", result),
            )
        after = current_commit(cache_dir)
        return GitCacheResult(ok=True, cache_dir=cache_dir, before=None, after=after, cloned=True)

    # --- 既存キャッシュを差分更新する ---
    before = current_commit(cache_dir)
    target_branch = branch or default_branch(cache_dir)

    fetch_result = _run_git(["fetch", "--depth", "1", "origin", target_branch], cache_dir)
    if not fetch_result.ok:
        return GitCacheResult(
            ok=False,
            cache_dir=cache_dir,
            before=before,
            after=None,
            cloned=False,
            error=_command_error("fetch", fetch_result),
        )

    reset_result = _run_git(["reset", "--hard", "FETCH_HEAD"], cache_dir)
    if not reset_result.ok:
        return GitCacheResult(
            ok=False,
            cache_dir=cache_dir,
            before=before,
            after=None,
            cloned=False,
            error=_command_error("reset", reset_result),
        )
    _run_git(["clean", "-fdx"], cache_dir)  # fetchで残った不要ファイルを掃除(.gitは保持)

    after = current_commit(cache_dir)
    return GitCacheResult(
        ok=True, cache_dir=cache_dir, before=before, after=after, cloned=False, fetched=True
    )


@dataclass(slots=True)
class DiffFiles:
    """`diff_files` の戻り値。"""

    added: list[str]
    modified: list[str]
    deleted: list[str]


def diff_files(cache_dir: Path, before: str | None, after: str | None) -> DiffFiles | None:
    """2つのコミット間の変更ファイル一覧を返す(旧実装 `diffFiles` の移植)。

    shallow クローンで比較できない場合は `None`(=差分不明)を返す。
    """
    if not before or not after:
        return None
    if before == after:
        return DiffFiles(added=[], modified=[], deleted=[])

    result = _run_git(["diff", "--name-status", before, after], cache_dir)
    if not result.ok:
        return None

    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        file = parts[-1]
        if status.startswith("A"):
            added.append(file)
        elif status.startswith("D"):
            deleted.append(file)
        else:
            modified.append(file)
    return DiffFiles(added=added, modified=modified, deleted=deleted)


def _collect_files(root: Path) -> list[str]:
    """`.git` を除くファイルを相対 POSIX パスで再帰的に、決定的な順序で集める。"""
    if not root.is_dir():
        return []
    files = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    ]
    return sorted(files)


def _remove_empty_dirs(root: Path) -> None:
    """空になったディレクトリを片付ける(出力先直下=`root` は残す)。

    旧実装 `removeEmptyDirs` の移植。
    """
    if not root.is_dir():
        return
    dirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    )
    for directory in dirs:
        try:
            next(directory.iterdir())
        except StopIteration:
            directory.rmdir()
        except FileNotFoundError:
            pass


@dataclass(slots=True)
class MirrorResult:
    """`mirror_cache_to_output` の戻り値(旧実装 `mirrorCacheToOutput` と同じ形)。"""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: int = 0


def mirror_cache_to_output(
    cache_dir: Path, target_dir: Path, *, check_lease: CheckLeaseFn | None = None
) -> MirrorResult:
    """キャッシュの内容を出力先へ反映する(旧実装 `mirrorCacheToOutput` の移植)。

    「全削除→再クローン」をやめ、内容が同じファイルには触れない(mtimeも変え
    ない)。取得元から消えたファイルだけを削除し、空ディレクトリを剪定する。
    追加/更新/削除は1件ずつのファイルI/Oという副作用を繰り返すため、次の
    副作用の前に毎回 `check_lease()` を呼ぶ(`JobRunContext.check_lease` の契約、
    esa の書き込みループ・orphan削除ループと同じ理由)。
    """
    cache_files = _collect_files(cache_dir)
    target_files = set(_collect_files(target_dir))

    result = MirrorResult()
    for relative in cache_files:
        if check_lease is not None:
            check_lease()
        source_path = cache_dir / relative
        dest_path = target_dir / relative
        source_bytes = source_path.read_bytes()

        if not dest_path.exists():
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(source_bytes)
            result.added.append(relative)
        elif dest_path.read_bytes() != source_bytes:
            dest_path.write_bytes(source_bytes)
            result.updated.append(relative)
        else:
            result.unchanged += 1  # 内容が同じファイルには触れない(mtimeも変えない)
        target_files.discard(relative)

    # 取得元から消えたファイルを削除する。
    for relative in sorted(target_files):
        if check_lease is not None:
            check_lease()
        (target_dir / relative).unlink(missing_ok=True)
        result.deleted.append(relative)

    _remove_empty_dirs(target_dir)
    return result


# --------------------------------------------------------------------------
# 同期セッション: キャッシュ更新 + ミラー + DB記録
# --------------------------------------------------------------------------


def _default_warn(message: str) -> None:
    logger.warning(message)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class SyncItem:
    """1ファイルの同期結果(`application.sync_service` のレポート化で使う)。"""

    path: str
    file_path: str
    action: str
    reason: str = ""
    write: bool = True
    error: str | None = None


@dataclass(slots=True)
class GitSyncResult:
    """`GitSyncRunner.sync` の戻り値。"""

    items: list[SyncItem]
    unchanged_count: int
    full_sync_succeeded: bool
    repository: str
    commit_before: str | None
    commit_after: str | None
    cache_dir: str
    cloned: bool
    error: str | None = None


class GitSyncRunner:
    """1つのリポジトリ設定に対する Git 同期セッション。"""

    def __init__(
        self,
        *,
        documents: DocumentRepository,
        root_dir: Path,
        docs_dir: Path,
        output_dir: str,
        repository: str,
        branch: str | None = None,
        cache_root: Path | None = None,
        warn: WarnFn = _default_warn,
    ) -> None:
        self._documents = documents
        self._root_dir = root_dir
        self._docs_dir = docs_dir
        # brief: git は "docs" で始まらない場合だけ付与する旧実装の緩い判定を
        # そのまま踏襲する(web/esaの厳密版とは意図的に統一しない、report参照)。
        self._output_dir = with_docs_prefix_loose(output_dir)
        self._repository = repository
        self._branch = branch
        # 旧実装の `data/git-cache/` から brief 指定の `data/cache/git/` へ改名。
        self._cache_root = cache_root or (root_dir / "data" / "cache" / "git")
        self._warn = warn

    def _safe_upsert(self, record: dict[str, Any]) -> None:
        try:
            self._documents.upsert(record)
        except Exception as exc:  # noqa: BLE001 - esa/web と同じ契約: DB失敗で同期自体を失敗させない
            self._warn(f"sync-state への記録に失敗しました ({record.get('path')}): {exc}")

    def _cache_name(self) -> str:
        repo_name = sanitize_folder_name(extract_repo_name(self._repository))
        if self._branch:
            return f"{repo_name}@{sanitize_folder_name(self._branch)}"
        return repo_name

    def sync(
        self, *, check_lease: CheckLeaseFn | None = None, emit: EmitFn | None = None
    ) -> GitSyncResult:
        """1リポジトリを同期する(旧実装 `main` の移植)。"""
        cache_name = self._cache_name()
        target_dir = self._root_dir / self._output_dir
        # 出力先ディレクトリ名(リポジトリ名由来)が `..` 等を含んでいても
        # docs/ の外へ出ないことを最終防衛線として検証する(esa/web と同じ理由)。
        ensure_within_docs(target_dir, self._docs_dir)
        safe_repository = redact_credentials(self._repository)

        cache = update_git_cache(
            self._repository, self._branch, cache_name, cache_root=self._cache_root
        )
        if not cache.ok:
            # brief必須要件: fetch/clone失敗は出力先に一切触れない。
            error_message = redact_credentials(cache.error or "unknown error")
            return GitSyncResult(
                items=[
                    SyncItem(
                        path=self._output_dir,
                        file_path=str(target_dir),
                        action=str(SyncAction.ERROR),
                        reason=error_message,
                        write=False,
                        error=error_message,
                    )
                ],
                unchanged_count=0,
                full_sync_succeeded=False,
                repository=safe_repository,
                commit_before=cache.before,
                commit_after=None,
                cache_dir=str(cache.cache_dir),
                cloned=cache.cloned,
                error=error_message,
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        mirror = mirror_cache_to_output(cache.cache_dir, target_dir, check_lease=check_lease)

        items: list[SyncItem] = []
        total = len(mirror.added) + len(mirror.updated) + len(mirror.deleted)
        done = 0
        for relative in mirror.added:
            items.append(
                SyncItem(
                    path=f"{self._output_dir}/{relative}",
                    file_path=str(target_dir / relative),
                    action=str(SyncAction.CREATE),
                )
            )
            done += 1
            if emit is not None:
                emit(
                    phase="sync-git",
                    current=done,
                    total=total,
                    message=f"create: {relative}",
                    item=relative,
                )
        for relative in mirror.updated:
            items.append(
                SyncItem(
                    path=f"{self._output_dir}/{relative}",
                    file_path=str(target_dir / relative),
                    action=str(SyncAction.UPDATE),
                )
            )
            done += 1
            if emit is not None:
                emit(
                    phase="sync-git",
                    current=done,
                    total=total,
                    message=f"update: {relative}",
                    item=relative,
                )
        for relative in mirror.deleted:
            items.append(
                SyncItem(
                    path=f"{self._output_dir}/{relative}",
                    file_path=str(target_dir / relative),
                    action=str(SyncAction.MISSING),
                    reason="取得元のリポジトリから削除されました",
                )
            )
            done += 1
            if emit is not None:
                emit(
                    phase="sync-git",
                    current=done,
                    total=total,
                    message=f"missing: {relative}",
                    item=relative,
                )

        self._record_markdown_files(target_dir, commit=cache.after, check_lease=check_lease)

        return GitSyncResult(
            items=items,
            unchanged_count=mirror.unchanged,
            full_sync_succeeded=True,
            repository=safe_repository,
            commit_before=cache.before,
            commit_after=cache.after,
            cache_dir=str(cache.cache_dir),
            cloned=cache.cloned,
        )

    def _record_markdown_files(
        self, target_dir: Path, *, commit: str | None, check_lease: CheckLeaseFn | None
    ) -> None:
        """出力先の Markdown を sync-state に記録する(旧実装 `recordMarkdownFiles` の移植)。

        brief契約6・7: front matter は書かない(メタデータはDBのみ)。
        `sync_status` は常に `synced`(`decide_sync_action` を通さない)。
        """
        relative_root = docs_relative_path(target_dir, self._docs_dir)
        if relative_root is None:
            return

        safe_repository = redact_credentials(self._repository)
        now = _now_iso()
        for relative in _collect_files(target_dir):
            if not relative.lower().endswith(".md"):
                continue
            if check_lease is not None:
                check_lease()

            path = f"{relative_root}/{relative}" if relative_root else relative
            content_bytes = (target_dir / relative).read_bytes()
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue

            classification = classify_document(
                relative_path=path, frontmatter={}, git_output_dirs=[f"docs/{relative_root}"]
            )
            self._safe_upsert(
                {
                    "path": path,
                    "source": "git",
                    "managed_by": "git-sync",
                    "document_type": classification.document_type,
                    "status": classification.status,
                    "url": safe_repository,
                    "source_key": f"git:{safe_repository}#{relative}",
                    "source_updated_at": commit,
                    "source_content_hash": sha256_hex(content),
                    "local_content_hash": hash_body(content),
                    "downloaded_at": now,
                    "last_checked_at": now,
                    "sync_status": str(SyncStatus.SYNCED),
                    "sync_error": None,
                }
            )


__all__ = [
    "DiffFiles",
    "GitCacheResult",
    "GitSyncResult",
    "GitSyncRunner",
    "MirrorResult",
    "SyncItem",
    "current_commit",
    "default_branch",
    "diff_files",
    "is_git_repo",
    "mirror_cache_to_output",
    "redact_credentials",
    "sanitize_folder_name",
    "update_git_cache",
    "with_docs_prefix_loose",
]
