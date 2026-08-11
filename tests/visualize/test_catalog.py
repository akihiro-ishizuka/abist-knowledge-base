"""可視化カタログ（`visualizations` テーブル）の記録とディスクからの再構築。

設計原則の回帰テスト:
- manifest.json が正本、DB は索引（DB を消しても再構築できる）
- 出力ディレクトリを作る前に失敗したものは「可視化」として記録しない
- 旧 Node 実装の manifest（`Z` 表記 / `generator.node`）も取り込める
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from abist_kb.application.visualization import catalog
from abist_kb.application.visualization.renderer import RenderOutcome
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.infrastructure.db.visualizations_repo import VisualizationRepository
from abist_kb.infrastructure.jobs.repository import JobRepository


@pytest.fixture
def env(tmp_path: Path):
    conn = open_app_db(tmp_path / "app.sqlite")
    viz = tmp_path / "reports" / "visualizations"
    viz.mkdir(parents=True)
    yield tmp_path, viz, conn, VisualizationRepository(conn)
    conn.close()


def _write_manifest(
    viz: Path,
    vid: str,
    *,
    created_at: str = "2026-08-01T01:44:22.396Z",
    exit_code: int = 0,
    with_output: bool = True,
    legacy_node: bool = True,
    sources: list[dict] | None = None,
) -> Path:
    d = viz / vid
    d.mkdir(parents=True, exist_ok=True)
    if with_output:
        (d / "output.png").write_bytes(b"png-bytes")
    generator = {"python": "3.11.9", "manim": "0.19.0"}
    if legacy_node:
        generator["node"] = "v22.18.0"
    manifest = {
        "schema_version": "1.0",
        "visualization_id": vid,
        "query": "蛇腹パターン自動化",
        "scene_kind": "flow",
        "template": "data_flow_v1",
        "output_format": "png",
        "created_at": created_at,
        "generator": generator,
        "spec": {
            "schema_version": "1.0",
            "scene_kind": "flow",
            "template": "data_flow_v1",
            "output_format": "png",
            "title": "タイトル",
            "query": "蛇腹パターン自動化",
        },
        "outputs": [{"path": "output.png", "sha256": "abc", "size_bytes": 9}]
        if with_output
        else [],
        "sources": sources
        if sources is not None
        else [
            {
                "id": "s1",
                "path": "a/b.md",
                "start_line": 1,
                "end_line": 2,
                "content_hash": "h" * 64,
            }
        ],
        "warnings": [],
        "render": {"exit_code": exit_code, "duration_ms": 4639},
    }
    path = d / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


class TestReconcileFromDisk:
    def test_imports_legacy_node_manifest(self, env) -> None:
        """旧 Node 実装の manifest（Z 表記・generator.node）も取り込めること。"""
        root, viz, conn, repo = env
        _write_manifest(viz, "20260801T014417Z-scene-b314")
        stats = catalog.reconcile_from_disk(conn, viz, root_dir=root)
        assert stats["upserted"] == 1
        rows, total = repo.list()
        assert total == 1
        assert rows[0]["state"] == "succeeded"
        assert rows[0]["source"] == "disk"
        # Z 表記が +00:00 へ正規化されること
        assert rows[0]["created_at"] == "2026-08-01T01:44:22.396+00:00"

    def test_failed_state_when_exit_code_nonzero(self, env) -> None:
        root, viz, conn, repo = env
        _write_manifest(viz, "20260101T000000Z-a-0001", exit_code=1)
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        assert repo.list()[0][0]["state"] == "failed"

    def test_failed_state_when_output_missing(self, env) -> None:
        """exit_code が 0 でも出力ファイルが無ければ failed。"""
        root, viz, conn, repo = env
        _write_manifest(viz, "20260101T000000Z-b-0002", with_output=False)
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        assert repo.list()[0][0]["state"] == "failed"

    def test_is_idempotent(self, env) -> None:
        root, viz, conn, repo = env
        _write_manifest(viz, "20260101T000000Z-c-0003")
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        assert repo.list()[1] == 1

    def test_removed_directory_orphans_the_row(self, env) -> None:
        """成果物ディレクトリを消したら行も消えること（ディスクが真）。"""
        root, viz, conn, repo = env
        _write_manifest(viz, "20260101T000000Z-d-0004")
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        shutil.rmtree(viz / "20260101T000000Z-d-0004")
        stats = catalog.reconcile_from_disk(conn, viz, root_dir=root)
        assert stats["orphaned"] == 1
        assert repo.list()[1] == 0

    def test_sources_are_recorded_as_unknown_on_import(self, env) -> None:
        """取り込み時に出典を再検証しない（当時の判定は残っていない）。"""
        root, viz, conn, repo = env
        _write_manifest(viz, "20260101T000000Z-e-0005")
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        sources = repo.get_sources("20260101T000000Z-e-0005")
        assert [s["status"] for s in sources] == ["unknown"]
        assert sources[0]["path"] == "a/b.md"

    def test_broken_manifest_is_skipped(self, env) -> None:
        root, viz, conn, repo = env
        d = viz / "20260101T000000Z-f-0006"
        d.mkdir()
        (d / "manifest.json").write_text("{ not json", encoding="utf-8")
        stats = catalog.reconcile_from_disk(conn, viz, root_dir=root)
        assert stats["scanned"] == 1
        assert stats["upserted"] == 0

    def test_missing_directory_clears_the_table(self, env) -> None:
        root, viz, conn, repo = env
        _write_manifest(viz, "20260101T000000Z-g-0007")
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        shutil.rmtree(viz)
        stats = catalog.reconcile_from_disk(conn, viz, root_dir=root)
        assert stats["orphaned"] == 1
        assert repo.list()[1] == 0


class TestRecordRender:
    def test_records_a_successful_render(self, env) -> None:
        root, viz, conn, repo = env
        manifest_path = _write_manifest(viz, "20260101T000000Z-h-0008", legacy_node=False)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        outcome = RenderOutcome(
            ok=True,
            visualization_id="20260101T000000Z-h-0008",
            output_dir=manifest_path.parent,
            manifest_path=manifest_path,
            duration_ms=1234.5,
            warnings=["注意"],
            source_status={"s1": "ok"},
            sources=manifest["sources"],
            manifest=manifest,
            manifest_sha256="deadbeef",
        )
        # job_id は jobs への外部キーなので実在するジョブを使う
        # (実運用では run.job.id を渡すので必ず実在する)。
        job = JobRepository(conn).submit("render_scene", {})
        catalog.record_render(conn, outcome, root_dir=root, job_id=job.id)
        record = repo.get("20260101T000000Z-h-0008")
        assert record is not None
        assert record["state"] == "succeeded"
        assert record["source"] == "render"
        assert record["job_id"] == job.id
        assert record["warnings"] == ["注意"]
        assert record["duration_ms"] == 1234.5
        # 相対パスで保存される（環境をまたいでも壊れないように）
        assert not Path(record["output_dir"]).is_absolute()
        assert repo.get_sources("20260101T000000Z-h-0008")[0]["status"] == "ok"

    def test_records_a_failed_render_with_code(self, env) -> None:
        root, viz, conn, repo = env
        manifest_path = _write_manifest(viz, "20260101T000000Z-i-0009", with_output=False)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        outcome = RenderOutcome(
            ok=False,
            code="RENDER_CANCELLED",
            visualization_id="20260101T000000Z-i-0009",
            output_dir=manifest_path.parent,
            manifest_path=manifest_path,
            manifest=manifest,
            manifest_sha256="cafe",
            sources=manifest["sources"],
            source_status={"s1": "hash_mismatch"},
        )
        catalog.record_render(conn, outcome, root_dir=root)
        record = repo.get("20260101T000000Z-i-0009")
        assert record["state"] == "failed"
        assert record["code"] == "RENDER_CANCELLED"
        assert repo.get_sources("20260101T000000Z-i-0009")[0]["status"] == "hash_mismatch"

    def test_does_not_record_when_no_output_dir_was_created(self, env) -> None:
        """INVALID_SCENE_SPEC 等は「可視化」として成立していないので記録しない。

        これは manifest.json が存在するかどうかと一致する。
        """
        _root, _viz, conn, repo = env
        catalog.record_render(
            conn,
            RenderOutcome(ok=False, code="INVALID_SCENE_SPEC"),
            root_dir=Path.cwd(),
        )
        assert repo.list()[1] == 0

    def test_upsert_overwrites_the_same_id(self, env) -> None:
        root, viz, conn, repo = env
        manifest_path = _write_manifest(viz, "20260101T000000Z-j-0010")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        base = RenderOutcome(
            ok=False,
            code="RENDER_FAILED",
            visualization_id="20260101T000000Z-j-0010",
            output_dir=manifest_path.parent,
            manifest_path=manifest_path,
            manifest=manifest,
            sources=manifest["sources"],
        )
        catalog.record_render(conn, base, root_dir=root)
        catalog.record_render(
            conn,
            RenderOutcome(
                ok=True,
                visualization_id="20260101T000000Z-j-0010",
                output_dir=manifest_path.parent,
                manifest_path=manifest_path,
                manifest=manifest,
                sources=manifest["sources"],
            ),
            root_dir=root,
        )
        assert repo.list()[1] == 1
        assert repo.get("20260101T000000Z-j-0010")["state"] == "succeeded"


class TestManifestDrift:
    def test_detects_changed_manifest(self, env) -> None:
        root, viz, conn, repo = env
        _write_manifest(viz, "20260101T000000Z-k-0011")
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        record = repo.get("20260101T000000Z-k-0011")
        drifted, manifest = catalog.manifest_drifted(record, root)
        assert drifted is False
        assert manifest is not None

        (viz / "20260101T000000Z-k-0011" / "manifest.json").write_text(
            '{"changed": true}', encoding="utf-8"
        )
        drifted, _ = catalog.manifest_drifted(record, root)
        assert drifted is True

    def test_missing_manifest_counts_as_drift(self, env) -> None:
        root, viz, conn, repo = env
        _write_manifest(viz, "20260101T000000Z-l-0012")
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        record = repo.get("20260101T000000Z-l-0012")
        (viz / "20260101T000000Z-l-0012" / "manifest.json").unlink()
        drifted, manifest = catalog.manifest_drifted(record, root)
        assert drifted is True
        assert manifest is None


class TestRepositoryFilters:
    def test_filters_and_pagination(self, env) -> None:
        root, viz, conn, repo = env
        for i in range(5):
            _write_manifest(viz, f"2026010{i}T000000Z-m-001{i}")
        catalog.reconcile_from_disk(conn, viz, root_dir=root)

        rows, total = repo.list(limit=2)
        assert total == 5
        assert len(rows) == 2
        # 新しい順
        assert rows[0]["created_at"] >= rows[1]["created_at"]

        assert repo.list(state="succeeded")[1] == 5
        assert repo.list(state="failed")[1] == 0
        assert repo.list(scene_kind="flow")[1] == 5
        assert repo.list(scene_kind="timeline")[1] == 0
        assert repo.list(output_format="png")[1] == 5
        assert repo.list(query="蛇腹")[1] == 5
        assert repo.list(query="存在しない語")[1] == 0
        assert repo.list(source_path="a/b.md")[1] == 5
        assert repo.list(source_path="無関係")[1] == 0

    def test_source_counts(self, env) -> None:
        root, viz, conn, repo = env
        _write_manifest(
            viz,
            "20260101T000000Z-n-0020",
            sources=[
                {"id": "ok1", "path": "a.md", "start_line": 1, "end_line": 1, "content_hash": "h"},
                {"id": "ok2", "path": "b.md", "start_line": 1, "end_line": 1, "content_hash": "h"},
            ],
        )
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        total, bad = repo.source_counts("20260101T000000Z-n-0020")
        assert total == 2
        # 取り込みは unknown なので「不良」には数えない
        assert bad == 0

    def test_delete_cascades_to_sources(self, env) -> None:
        root, viz, conn, repo = env
        _write_manifest(viz, "20260101T000000Z-o-0021")
        catalog.reconcile_from_disk(conn, viz, root_dir=root)
        assert repo.delete("20260101T000000Z-o-0021") is True
        assert repo.get_sources("20260101T000000Z-o-0021") == []

    def test_disk_ids(self, env) -> None:
        _root, viz, _conn, _repo = env
        _write_manifest(viz, "20260101T000000Z-p-0022")
        (viz / "manifest-less-dir").mkdir()
        assert catalog.disk_ids(viz) == {"20260101T000000Z-p-0022"}


def test_job_deletion_keeps_the_visualization_row(env) -> None:
    """ジョブ行が整理されても成果物の記録は残る（ON DELETE SET NULL）。

    ジョブは運用ログ、成果物はディスク上の実体なので寿命が違う。
    """
    root, viz, conn, repo = env
    manifest_path = _write_manifest(viz, "20260101T000000Z-q-0030")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    job = JobRepository(conn).submit("render_scene", {})
    catalog.record_render(
        conn,
        RenderOutcome(
            ok=True,
            visualization_id="20260101T000000Z-q-0030",
            output_dir=manifest_path.parent,
            manifest_path=manifest_path,
            manifest=manifest,
            sources=manifest["sources"],
        ),
        root_dir=root,
        job_id=job.id,
    )
    conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
    conn.commit()
    record = repo.get("20260101T000000Z-q-0030")
    assert record is not None, "ジョブ削除で成果物の記録まで消えている"
    assert record["job_id"] is None
