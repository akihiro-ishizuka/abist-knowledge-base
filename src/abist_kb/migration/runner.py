"""`migrate run`(設計書 §11.1/§11.3)。

一時ビルドディレクトリへ構築し、検証成功後だけ正式データディレクトリへ
swap する。工程ごとに `migration-manifest.json` へ記録し、同じ入力
ハッシュで `completed` 済みの工程は再実行せずスキップする(再開可能)。
失敗時は移行先(一時ディレクトリ)を破棄すればよく、移行元には一切
書き込まない。
"""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from abist_kb.application.batch_service import BatchService
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db.schema import open_app_db
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
        imported = 0
        with dest_conn:
            for row in rows:
                record: dict[str, Any] = dict(row)
                record.setdefault("uuid", str(uuid.uuid4()))
                if not record.get("uuid"):
                    record["uuid"] = str(uuid.uuid4())
                columns = [c for c in _DOCUMENTS_TEXT_COLUMNS if c in record] + [
                    c for c in _DOCUMENTS_INT_COLUMNS if c in record
                ]
                if not columns:
                    continue
                placeholders = ", ".join(f":{c}" for c in columns)
                col_list = ", ".join(columns)
                dest_conn.execute(
                    f"INSERT OR REPLACE INTO documents ({col_list}) VALUES ({placeholders})",  # noqa: S608
                    {c: record.get(c) for c in columns},
                )
                imported += 1
        step.counts = {"imported": imported}
        step.status = "completed"
        step.finished_at = now_iso()
        return step
    finally:
        src_conn.close()
        dest_conn.close()


def run_migration(
    plan: MigrationPlan,
    manifest: Manifest,
    manifest_path: Path,
    from_root: Path,
    build_dir: Path,
    sandbox_dir: Path,
) -> Manifest:
    """`plan` に従って `build_dir` へ移行を構築する。まだ `to_root` への swap は行わない。

    工程ごとに `manifest` を更新して都度 `manifest_path` へ保存する
    (途中で中断されても、次回呼び出しで完了済み工程をスキップして再開できる)。
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
