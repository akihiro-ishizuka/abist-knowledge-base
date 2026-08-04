"""読み取り専用サンドボックスへの SQLite コピー(設計書 §11.1)。

`tests/fixtures/PROVENANCE.md` §5 の実測により、better-sqlite3 を
`readonly:true` 無しで開くと `-shm` の mtime が動くことが分かっている。
Python の `sqlite3` でも同種の懸念(WAL チェックポイント、ファイル変更
カウンタ更新)を避けるため、移行元の SQLite ファイルは常にこの関数で
使い捨てサンドボックスへコピーしたものだけを開く。移行元そのものは一度も
開かない。
"""

from __future__ import annotations

import shutil
from pathlib import Path

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def copy_sqlite_to_sandbox(source_db: Path, sandbox_dir: Path) -> Path:
    """`source_db`(と存在すれば WAL/SHM/journal サイドカー)を `sandbox_dir` へコピーする。

    移行元は読み取り専用でしか触れない(`shutil.copy2` は読み取りのみ)。
    戻り値はサンドボックス側のコピーのパスで、以後の SQLite アクセスは
    必ずこのパスに対して行う。
    """
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    dest = sandbox_dir / source_db.name
    shutil.copy2(source_db, dest)
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = source_db.with_name(source_db.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, sandbox_dir / sidecar.name)
    return dest
