"""`migrate run`(設計書 §11.1/§11.3)。

一時ビルドディレクトリへ構築し、検証成功後だけ正式データディレクトリへ
swap する。工程ごとに `migration-manifest.json` へ記録し、同じ入力
ハッシュで `completed` 済みの工程は再実行せずスキップする(再開可能)。
失敗時は移行先(一時ディレクトリ)を破棄すればよく、移行元には一切
書き込まない。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from abist_kb.application.batch_service import BatchService
from abist_kb.application.index_service import IndexService
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.infrastructure.db.sources_repo import SourceRepository
from abist_kb.migration.manifest import (
    Manifest,
    StepRecord,
    hash_inputs,
    now_iso,
    save_manifest,
)
from abist_kb.migration.plan import MigrationPlan
from abist_kb.migration.sandbox import copy_sqlite_to_sandbox

_DOCUMENTS_TEXT_COLUMNS = (
    "path",
    "uuid",
    "source",
    "managed_by",
    "document_type",
    "status",
    "title",
    "url",
    "category",
    "source_key",
    "source_updated_at",
    "sync_status",
    "source_content_hash",
    "local_content_hash",
    "downloaded_at",
    "last_checked_at",
    "etag",
    "last_modified",
    "missing_since",
    "sync_error",
    "indexed_at",
    "embedding_model",
    "created_at",
    "updated_at",
)
_DOCUMENTS_INT_COLUMNS = ("post_number", "embedding_dimensions", "missing_count")


def _copy_docs_step(plan: MigrationPlan, from_root: Path, build_dir: Path) -> StepRecord:
    copy_items = [item for item in plan.items if item.action == "copy"]
    input_hash = hash_inputs(*(f"{item.relative_path}" for item in copy_items))
    step = StepRecord(name="copy_docs", status="failed", input_hash=input_hash)
    step.started_at = now_iso()
    copied = 0
    for item in copy_items:
        src = from_root / item.relative_path
        dest = build_dir / item.relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        dest.write_bytes(data)
        import hashlib

        step.sha256[item.relative_path] = hashlib.sha256(data).hexdigest()
        copied += 1
    excluded = [
        {"path": item.relative_path, "reason": item.reason}
        for item in plan.items
        if item.action == "exclude"
    ]
    step.excluded = excluded
    step.counts = {"copied": copied, "excluded": len(excluded)}
    step.status = "completed"
    step.finished_at = now_iso()
    return step


def _import_batch_config_step(plan: MigrationPlan, from_root: Path, build_dir: Path) -> StepRecord:
    batch_items = [item for item in plan.items if item.relative_path == "data/batch-config.js"]
    action = batch_items[0].action if batch_items else "exclude"
    step = StepRecord(name="import_batch_config", status="failed", input_hash=hash_inputs(action))
    step.started_at = now_iso()
    if action != "convert":
        reason = batch_items[0].reason if batch_items else "batch-config.js が計画にない"
        step.excluded = [{"path": "data/batch-config.js", "reason": reason}]
        step.counts = {"imported": 0, "excluded": 1}
        step.status = "completed"
        step.finished_at = now_iso()
        return step

    config_path = from_root / "data" / "batch-config.js"
    conn = open_app_db(build_dir / "app.sqlite")
    try:
        service = BatchService(conn)
        try:
            result = service.import_from_old_config(config_path)
        except AppError as exc:
            step.warnings.append(str(exc))
            step.status = "failed"
            step.finished_at = now_iso()
            return step
        step.counts = {"imported": int(result.get("imported", 0))}
        step.status = "completed"
        step.finished_at = now_iso()
        return step
    finally:
        conn.close()


def _import_sync_state_step(
    plan: MigrationPlan, from_root: Path, build_dir: Path, sandbox_dir: Path
) -> StepRecord:
    sync_items = [item for item in plan.items if item.relative_path == "data/sync-state.sqlite"]
    if not sync_items or sync_items[0].action != "convert":
        step = StepRecord(
            name="import_sync_state", status="completed", input_hash=hash_inputs("absent")
        )
        step.counts = {"imported": 0}
        step.warnings.append("data/sync-state.sqlite が計画に無いためスキップ")
        return step

    source_db = from_root / "data" / "sync-state.sqlite"
    sandboxed = copy_sqlite_to_sandbox(source_db, sandbox_dir / "sync-state")
    step = StepRecord(
        name="import_sync_state",
        status="failed",
        input_hash=hash_inputs(str(sandboxed.stat().st_size)),
    )
    step.started_at = now_iso()

    src_conn = sqlite3.connect(f"file:{sandboxed.as_posix()}?mode=ro", uri=True)
    src_conn.row_factory = sqlite3.Row
    dest_conn = open_app_db(build_dir / "app.sqlite")
    try:
        rows = src_conn.execute("SELECT * FROM documents").fetchall()
        source_repo = SourceRepository(dest_conn)
        resolved_cache: dict[str, str] = {
            source["type"]: source["id"] for source in source_repo.list()
        }
        unresolved: list[dict[str, str]] = []
        imported = 0
        with dest_conn:
            for row in rows:
                record: dict[str, Any] = dict(row)
                record.setdefault("uuid", str(uuid.uuid4()))
                if not record.get("uuid"):
                    record["uuid"] = str(uuid.uuid4())

                source_value = (record.get("source") or "").strip()
                if not source_value:
                    unresolved.append(
                        {
                            "path": str(record.get("path", "?")),
                            "reason": "旧sync-state行のsource列が空のため"
                            " source_id を解決できません",
                        }
                    )
                    record["source_id"] = None
                else:
                    source_id = resolved_cache.get(source_value)
                    if source_id is None:
                        # `sources` にまだ存在しない旧sourceカテゴリ: 突合できる接続情報が
                        # 旧sync-state.sqliteには無いため、type/output_dirのみのプレース
                        # ホルダーとして最小限のエントリを作成する(運用者が後で
                        # connection を編集する前提。§11.1: 値は一切旧システムから
                        # 引き継がない)。
                        created = source_repo.create(
                            type=source_value,
                            display_name=source_value,
                            output_dir=f"docs/{source_value}",
                        )
                        source_id = created["id"]
                        resolved_cache[source_value] = source_id
                    record["source_id"] = source_id

                columns = (
                    [c for c in _DOCUMENTS_TEXT_COLUMNS if c in record]
                    + [c for c in _DOCUMENTS_INT_COLUMNS if c in record]
                    + ["source_id"]
                )
                if not columns:
                    continue
                placeholders = ", ".join(f":{c}" for c in columns)
                col_list = ", ".join(columns)
                dest_conn.execute(
                    f"INSERT OR REPLACE INTO documents ({col_list}) VALUES ({placeholders})",  # noqa: S608
                    {c: record.get(c) for c in columns},
                )
                imported += 1
        step.counts = {
            "imported": imported,
            "source_id_resolved": imported - len(unresolved),
            "source_id_unresolved": len(unresolved),
        }
        step.excluded = unresolved
        step.status = "completed"
        step.finished_at = now_iso()
        return step
    finally:
        src_conn.close()
        dest_conn.close()


def _redact_git_remote(url: str) -> str:
    """認証情報(PAT等)を含みうる remote URL からユーザ情報を落として記録用に整形する。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(解析不能な remote URL)"
    if not parts.netloc:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _copy_visualizations_step(from_root: Path, build_dir: Path) -> StepRecord:
    """`reports/visualizations/`(§11.2)。manifest と全出力の SHA-256 が一致した
    アーティファクトのみコピーする。不一致は copy せず finding として記録する
    (「とりあえずコピー」しない: 壊れた可視化を正として持ち込まない)。
    """
    viz_root = from_root / "reports" / "visualizations"
    step = StepRecord(name="copy_visualizations", status="failed", input_hash="")
    step.started_at = now_iso()
    if not viz_root.is_dir():
        step.input_hash = hash_inputs("absent")
        step.counts = {"copied": 0, "excluded": 0}
        step.status = "completed"
        step.finished_at = now_iso()
        return step

    entry_dirs = sorted(p for p in viz_root.iterdir() if p.is_dir())
    step.input_hash = hash_inputs(*(p.name for p in entry_dirs))
    copied = 0
    excluded: list[dict[str, str]] = []
    for entry in entry_dirs:
        manifest_path = entry / "manifest.json"
        if not manifest_path.is_file():
            excluded.append(
                {"path": entry.name, "reason": "manifest.json が無いため検証できません"}
            )
            continue
        try:
            artifact_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            excluded.append({"path": entry.name, "reason": f"manifest.json が壊れています: {exc}"})
            continue

        mismatches: list[str] = []
        for output in artifact_manifest.get("outputs", []):
            rel = output.get("path")
            expected_sha = output.get("sha256")
            if not rel or not expected_sha:
                mismatches.append(str(rel))
                continue
            output_path = entry / rel
            if not output_path.is_file() or _sha256_file(output_path) != expected_sha:
                mismatches.append(rel)
        if mismatches:
            excluded.append(
                {
                    "path": entry.name,
                    "reason": f"出力の SHA-256 が manifest と不一致(または欠落): {mismatches}",
                }
            )
            continue

        dest = build_dir / "reports" / "visualizations" / entry.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(entry, dest)
        copied += 1

    step.excluded = excluded
    step.counts = {"copied": copied, "excluded": len(excluded)}
    step.status = "completed"
    step.finished_at = now_iso()
    return step


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_capture(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _copy_git_cache_step(from_root: Path, build_dir: Path) -> StepRecord:
    """`data/git-cache/`(§11.2)。remote URL と HEAD がローカルに検証できた
    エントリのみコピーする。検証不能な場合は破損ミラーを持ち込まず discard し、
    「後で re-clone すること」を理由として記録する(壊れたミラー < 何も無い)。

    ここでの「検証」はネットワークへ問い合わせない(移行ツールは常に
    オフライン・read-onlyで完結させる方針、§11.1)。ローカルの `.git` が
    HEAD を解決でき、かつ `origin` の remote URL を持つことを以って
    「検証できた」とみなす。認証情報を含みうる remote URL はサニタイズして
    のみ manifest に記録する。
    """
    cache_root = from_root / "data" / "git-cache"
    step = StepRecord(name="copy_git_cache", status="failed", input_hash="")
    step.started_at = now_iso()
    if not cache_root.is_dir():
        step.input_hash = hash_inputs("absent")
        step.counts = {"copied": 0, "excluded": 0}
        step.status = "completed"
        step.finished_at = now_iso()
        return step

    entry_dirs = sorted(p for p in cache_root.iterdir() if p.is_dir())
    step.input_hash = hash_inputs(*(p.name for p in entry_dirs))
    copied = 0
    excluded: list[dict[str, str]] = []
    verified_remotes: dict[str, str] = {}
    for entry in entry_dirs:
        if not (entry / ".git").exists():
            excluded.append(
                {"path": entry.name, "reason": ".git が無く再クローン対象として discard"}
            )
            continue
        head = _git_capture(["rev-parse", "HEAD"], entry)
        remote_url = _git_capture(["remote", "get-url", "origin"], entry)
        if not head or not remote_url:
            excluded.append(
                {
                    "path": entry.name,
                    "reason": "remote URL/HEAD をローカルで検証できないため discard"
                    "(再クローンを推奨)",
                }
            )
            continue
        dest = build_dir / "data" / "git-cache" / entry.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(entry, dest)
        copied += 1
        verified_remotes[entry.name] = f"{_redact_git_remote(remote_url)}@{head}"

    step.excluded = excluded
    step.counts = {"copied": copied, "excluded": len(excluded)}
    step.details = {"verified_remotes": verified_remotes}
    step.status = "completed"
    step.finished_at = now_iso()
    return step


_ENV_KEY_PATTERN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _diagnose_env_step(from_root: Path, build_dir: Path) -> StepRecord:
    """`.env`(§11.1)。**キー名のみ**診断し、値は一切読み取り内容をコピー・
    ログしない(値そのものは manifest にも一切書かない)。運用者はレポートされた
    キー名を見て新システム側で自分の手で値を設定する。
    """
    env_path = from_root / ".env"
    step = StepRecord(name="diagnose_env", status="failed", input_hash="")
    step.started_at = now_iso()
    if not env_path.is_file():
        step.input_hash = hash_inputs("absent")
        step.counts = {"keys_found": 0}
        step.details = {"keys": []}
        step.status = "completed"
        step.finished_at = now_iso()
        return step

    keys: list[str] = []
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_KEY_PATTERN.match(line)
        if match:
            keys.append(match.group(1))

    step.input_hash = hash_inputs(*keys)
    step.counts = {"keys_found": len(keys)}
    step.details = {"keys": keys}
    step.warnings.append(
        "値は移行しません。新システム側で以下のキーの値を運用者自身が設定してください。"
    )
    step.status = "completed"
    step.finished_at = now_iso()
    return step


def _embed_step(
    build_dir: Path,
    docs_dir: Path,
    *,
    corpus: str = "work",
) -> StepRecord:
    """埋め込み再生成(§11.2)。`index build` → `index embed` を build_dir 上で
    直接実行する(`index_service.generate_embeddings` はバッチごとに commit する
    ため、途中で中断しても未処理チャンクだけが残り再実行で自然に再開する)。

    進捗はテキストの progress log(`logs/embedding-progress.log`)へも書き出す。
    100分規模の実行を操作者が「まだ動いている」か「静かに死んだ」かを、
    ログの追記(mtime)と最終行の完了/例外メッセージだけで判別できるようにする
    ためで、job基盤の内部状態を別途問い合わせなくても済むようにする。
    """
    step = StepRecord(name="embed_chunks", status="failed", input_hash=hash_inputs(corpus))
    step.started_at = now_iso()

    log_path = build_dir / "logs" / "embedding-progress.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {message}\n")

    _log(f"embed_chunks 開始 corpus={corpus}")

    service = IndexService(
        docs_dir=docs_dir,
        app_db_path=build_dir / "app.sqlite",
        work_index_path=build_dir / "work-index.sqlite",
        reference_index_path=build_dir / "reference-index.sqlite",
    )

    def _emit(**kwargs: Any) -> None:
        current = kwargs.get("current")
        total = kwargs.get("total")
        message = kwargs.get("message", "")
        if current is not None and total is not None:
            _log(f"phase={kwargs.get('phase')} {current}/{total} {message}")
        else:
            _log(f"phase={kwargs.get('phase')} {message}")

    try:
        build_summary = service.build(corpus, emit=_emit)
        embed_summary = service.embed(corpus, emit=_emit)
    except Exception as exc:  # noqa: BLE001 -- 進捗ログに終了時刻付きで残すため捕捉
        _log(f"embed_chunks 失敗: {exc!r}")
        step.warnings.append(str(exc))
        step.status = "failed"
        step.finished_at = now_iso()
        return step

    _log(
        "embed_chunks 完了 "
        f"generated={embed_summary['summary'].get('generated', 0)} "
        f"scanned={embed_summary['summary'].get('scanned', 0)}"
    )
    step.counts = {
        "documents_added": build_summary["summary"].get("documents_added", 0),
        "documents_updated": build_summary["summary"].get("documents_updated", 0),
        "embeddings_generated": embed_summary["summary"].get("generated", 0),
        "embeddings_scanned": embed_summary["summary"].get("scanned", 0),
        "embeddings_skipped": embed_summary["summary"].get("skipped", 0),
    }
    step.details = {"progress_log": str(log_path)}
    step.status = "completed"
    step.finished_at = now_iso()
    return step


def run_migration(
    plan: MigrationPlan,
    manifest: Manifest,
    manifest_path: Path,
    from_root: Path,
    build_dir: Path,
    sandbox_dir: Path,
    *,
    run_embeddings: bool = False,
) -> Manifest:
    """`plan` に従って `build_dir` へ移行を構築する。まだ `to_root` への swap は行わない。

    工程ごとに `manifest` を更新して都度 `manifest_path` へ保存する
    (途中で中断されても、次回呼び出しで完了済み工程をスキップして再開できる)。

    `run_embeddings=True` の場合のみ埋め込み再生成工程(`embed_chunks`)を実行する
    (§11.2、実測 8.8件/秒・約52,000チャンクで約100分。ローカル埋め込みモデルの
    読み込みを伴う重い工程のため既定では実行しない。CLI の `--with-embeddings`
    が明示的に True を渡す)。
    """
    build_dir.mkdir(parents=True, exist_ok=True)

    copy_input_hash = hash_inputs(
        *(item.relative_path for item in plan.items if item.action == "copy")
    )
    if not manifest.is_step_current("copy_docs", copy_input_hash):
        step = _copy_docs_step(plan, from_root, build_dir)
        manifest.record_step(step)
        save_manifest(manifest, manifest_path)

    batch_action_items = [
        item for item in plan.items if item.relative_path == "data/batch-config.js"
    ]
    batch_hash = hash_inputs(batch_action_items[0].action if batch_action_items else "absent")
    if not manifest.is_step_current("import_batch_config", batch_hash):
        step = _import_batch_config_step(plan, from_root, build_dir)
        manifest.record_step(step)
        save_manifest(manifest, manifest_path)

    sync_state_path = from_root / "data" / "sync-state.sqlite"
    sync_hash = hash_inputs(
        str(sync_state_path.stat().st_size) if sync_state_path.exists() else "absent"
    )
    if not manifest.is_step_current("import_sync_state", sync_hash):
        step = _import_sync_state_step(plan, from_root, build_dir, sandbox_dir)
        manifest.record_step(step)
        save_manifest(manifest, manifest_path)

    viz_root = from_root / "reports" / "visualizations"
    viz_hash = hash_inputs(
        *(sorted(p.name for p in viz_root.iterdir() if p.is_dir()) if viz_root.is_dir() else [])
    )
    if not manifest.is_step_current("copy_visualizations", viz_hash):
        step = _copy_visualizations_step(from_root, build_dir)
        manifest.record_step(step)
        save_manifest(manifest, manifest_path)

    git_cache_root = from_root / "data" / "git-cache"
    git_cache_hash = hash_inputs(
        *(
            sorted(p.name for p in git_cache_root.iterdir() if p.is_dir())
            if git_cache_root.is_dir()
            else []
        )
    )
    if not manifest.is_step_current("copy_git_cache", git_cache_hash):
        step = _copy_git_cache_step(from_root, build_dir)
        manifest.record_step(step)
        save_manifest(manifest, manifest_path)

    env_path = from_root / ".env"
    env_hash = hash_inputs(str(env_path.stat().st_mtime_ns) if env_path.exists() else "absent")
    if not manifest.is_step_current("diagnose_env", env_hash):
        step = _diagnose_env_step(from_root, build_dir)
        manifest.record_step(step)
        save_manifest(manifest, manifest_path)

    if run_embeddings:
        embed_hash = hash_inputs("work")
        if not manifest.is_step_current("embed_chunks", embed_hash):
            step = _embed_step(build_dir, build_dir / "docs")
            manifest.record_step(step)
            save_manifest(manifest, manifest_path)

    return manifest


def swap_into_place(build_dir: Path, to_root: Path) -> None:
    """検証成功後にだけ呼ぶ。`to_root` が既に存在する場合は拒否する。"""
    if to_root.exists() and any(to_root.iterdir()):
        raise AppError(
            ErrorCode.MIGRATION_FAILED,
            f"移行先 {to_root} は既に空でない状態で存在します。",
            hint="別のディレクトリを指定するか、既存内容を確認してから空にしてください。",
        )
    to_root.parent.mkdir(parents=True, exist_ok=True)
    if to_root.exists():
        shutil.rmtree(to_root)
    shutil.move(str(build_dir), str(to_root))
