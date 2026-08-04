"""旧 Node 実装(`multi-source-knowledge-base`)をサンドボックスコピーで
読み取り専用に実行するための共通基盤(task-6: E2E バイト同一検証)。

**絶対条件**: `C:\\Temp\\multi-source-knowledge-base` には一切書き込まない。
旧スクリプト(`download-article.js` / `download-git.js` / `download-web.js`)と
`tools/lib/` 依存だけを OS 一時領域へ複製し、`node_modules` はジャンクション
(読み取り専用の意図、Windows でシンボリックリンクの管理者権限を避けるため
junction を使う)、出力先もすべて一時領域にする。`npm install` は絶対に
実行しない(ロックファイルを変更しないため、複製先には既存の `node_modules`
をそのまま繋ぐ)。

これらのスクリプトは `__dirname`/`ROOT`(= 自分自身のファイル位置から相対的に
計算される)を基準に全ての相対パス I/O を行う(`download-article.js` の
`DOCS_DIR = path.join(__dirname, 'docs')`、`tools/lib/doc-record.js` の
`ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..')`
など)。そのためファイルを丸ごと複製するだけで、複製先だけで完結する
(旧リポジトリの `docs/`/`data/` には一切触れない)。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OLD_REPO_ROOT = Path(r"C:\Temp\multi-source-knowledge-base")

#: サンドボックスへ複製する旧スクリプト本体(ルート直下)。
_TOP_LEVEL_SCRIPTS = ("download-article.js", "download-git.js", "download-web.js")

#: 上記スクリプトが `tools/lib/` から import する依存(推移的に洗い出し済み、
#: `download-article.js`/`download-git.js`/`download-web.js` の import 文と
#: `tools/lib/doc-record.js` が import する `sync-state.js` を確認して確定)。
_LIB_FILES = (
    "tools/lib/frontmatter.js",
    "tools/lib/metadata-schema.js",
    "tools/lib/doc-record.js",
    "tools/lib/sync-planner.js",
    "tools/lib/sync-report.js",
    "tools/lib/sync-state.js",
)


def old_repo_root() -> Path:
    return Path(os.environ.get("KB_OLD_REPO", str(DEFAULT_OLD_REPO_ROOT)))


def old_repo_available() -> bool:
    root = old_repo_root()
    return (root / "tools" / "lib" / "frontmatter.js").is_file() and (
        root / "node_modules"
    ).is_dir()


# ---------------------------------------------------------------------------
# 旧リポジトリ無変更ガード(`tests/fixtures/capture/_shared.mjs` の
# `assertReadOnly()` の Python 移植。同じ理由でファイル単位の (size, mtime)
# フィンガープリントを取る: docs/・data/ は git 管理外のため `git status` だけ
# では検知できない)。
# ---------------------------------------------------------------------------

_FINGERPRINT_DIRS = ("docs", "data")
_FINGERPRINT_FILES = ("batch-config.js",)


def _git_status(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _walk_fingerprint(root: Path, sub: str) -> dict[str, str]:
    base = root / sub
    out: dict[str, str] = {}
    if not base.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            rel = full.relative_to(root).as_posix()
            out[rel] = f"{st.st_size}:{round(st.st_mtime * 1000)}"
    return out


@dataclass(slots=True)
class RepoFingerprint:
    status: str
    files: dict[str, str]


def fingerprint_old_repo() -> RepoFingerprint:
    root = old_repo_root()
    files: dict[str, str] = {}
    for sub in _FINGERPRINT_DIRS:
        files.update(_walk_fingerprint(root, sub))
    for rel in _FINGERPRINT_FILES:
        full = root / rel
        if full.is_file():
            st = full.stat()
            files[rel] = f"{st.st_size}:{round(st.st_mtime * 1000)}"
    return RepoFingerprint(status=_git_status(root), files=files)


def assert_unchanged(before: RepoFingerprint, after: RepoFingerprint) -> None:
    """旧リポジトリが採取前後で変化していないことを検証する(差分があれば例外)。"""
    if before.status != after.status:
        raise AssertionError(
            "旧リポジトリの git status が変化しました:\n"
            f"--- 前 ---\n{before.status}\n--- 後 ---\n{after.status}"
        )
    added = sorted(set(after.files) - set(before.files))
    removed = sorted(set(before.files) - set(after.files))
    changed = sorted(
        k for k in after.files if k in before.files and before.files[k] != after.files[k]
    )
    if added or removed or changed:
        raise AssertionError(
            "旧リポジトリの docs/・data/・batch-config.js が変化しました: "
            f"追加={added[:10]} 削除={removed[:10]} 変更={changed[:10]}"
        )


# ---------------------------------------------------------------------------
# サンドボックス構築
# ---------------------------------------------------------------------------


def _make_junction(link: Path, target: Path) -> None:
    """Windows のディレクトリジャンクションを作る(管理者権限不要、読み取り専用の
    意図。ジャンクション自体は書き込みも技術的には可能だが、サンドボックス側の
    スクリプトは `node_modules` へ書き込む処理を一切持たないため実害はない)。
    """
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )


def build_sandbox(dest: Path) -> Path:
    """旧スクリプト・依存を `dest` へ複製し、`node_modules` をジャンクションで
    繋いだサンドボックスを作る。旧リポジトリへは読み取りしか行わない。
    """
    root = old_repo_root()
    dest.mkdir(parents=True, exist_ok=True)

    for rel in _TOP_LEVEL_SCRIPTS:
        shutil.copy2(root / rel, dest / rel)
    for rel in _LIB_FILES:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, target)

    # `"type": "module"` だけの最小 package.json(ロックファイルは複製しない
    # =npm installを絶対に誘発しない)。
    (dest / "package.json").write_text('{"type": "module"}\n', encoding="utf-8")

    _make_junction(dest / "node_modules", root / "node_modules")

    return dest


def run_node(
    sandbox: Path,
    script_rel: str,
    args: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    """サンドボックス内で `node <script_rel> <args...>` を実行する。

    `cwd=sandbox` にすることで dotenv.config() が旧リポジトリの `.env` を
    誤って読むことも無い(サンドボックスには `.env` を複製しないため)。
    """
    env = dict(os.environ)
    env.pop("ESA_TEAM_NAME", None)
    env.pop("ESA_ACCESS_TOKEN", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["node", script_rel, *args],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=timeout,
        check=False,
    )


#: サンドボックス内に置く小さな駆動スクリプト。旧 `download-article.js` の CLI
#: (`main()`)は esa API ベースURLをハードコードしており(brief記載の理由で)
#: モックサーバーへ向けられないため、export 済みの `savePost` を直接呼ぶ
#: (旧 `test/sync-esa.test.js` 自身がネットワークを介さず同じことをしている)。
_ESA_DRIVER_SOURCE = """\
import { savePost } from './download-article.js';
import fs from 'fs/promises';

const [, , postsFile, outputDir, resultsFile] = process.argv;
const posts = JSON.parse(await fs.readFile(postsFile, 'utf-8'));
const results = [];
for (const post of posts) {
  const item = await savePost(post, outputDir, {});
  results.push(item);
}
await fs.writeFile(resultsFile, JSON.stringify(results), 'utf-8');
"""


def write_esa_driver(sandbox: Path) -> str:
    """`esa_driver.mjs` をサンドボックスへ書き、相対パスを返す。"""
    path = sandbox / "esa_driver.mjs"
    path.write_text(_ESA_DRIVER_SOURCE, encoding="utf-8", newline="\n")
    return "esa_driver.mjs"


#: 旧 `download-git.js` の CLI(`main()`)は `extractRepoName` がURLとしてしか
#: 解釈できず(`new URL()` が失敗した場合の fallback が `split('/')` のみで、
#: Windows のバックスラッシュ区切りパスを1つの巨大な名前として扱ってしまい、
#: サニタイズ後も `MAX_PATH` を超えて `git clone` が失敗する)、ローカル
#: フォルダを直接指定するテストと相性が悪い。旧 `test/sync-git.test.js` 自身も
#: これを避けて `updateGitCache`/`mirrorCacheToOutput` を直接呼んでいるため、
#: ここでも同じ関数を直接呼ぶ(cacheName・出力先を明示することで、この
#: CLIパース由来の差異を比較対象から除外し、Python側の `GitSyncRunner` と
#: 同じ内部関数どうしを比較する)。
_GIT_DRIVER_SOURCE = """\
import { updateGitCache, mirrorCacheToOutput } from './download-git.js';
import fs from 'fs/promises';

const [, , repository, branch, cacheRoot, cacheName, targetDir] = process.argv;
const cache = await updateGitCache(repository, branch || null, cacheName, { cacheRoot });
if (!cache.ok) {
  process.stderr.write(cache.error || 'unknown error');
  process.exit(1);
}
await fs.mkdir(targetDir, { recursive: true });
const mirror = await mirrorCacheToOutput(cache.cacheDir, targetDir);
process.stdout.write(JSON.stringify({ before: cache.before, after: cache.after, mirror }));
"""


def write_git_driver(sandbox: Path) -> str:
    path = sandbox / "git_driver.mjs"
    path.write_text(_GIT_DRIVER_SOURCE, encoding="utf-8", newline="\n")
    return "git_driver.mjs"


__all__ = [
    "DEFAULT_OLD_REPO_ROOT",
    "RepoFingerprint",
    "assert_unchanged",
    "build_sandbox",
    "fingerprint_old_repo",
    "old_repo_available",
    "old_repo_root",
    "run_node",
    "write_esa_driver",
    "write_git_driver",
]
