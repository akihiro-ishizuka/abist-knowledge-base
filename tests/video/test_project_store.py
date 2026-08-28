"""Phase 1 統合: 入力解決 → プロジェクト作成 → 保存 → カタログ → 再構築。

**正本はディスク（`project-spec.json`）、DB は索引**という原則の回帰テスト。
purring の `visualizations` と同じ扱いであることを固定する。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from abist_kb.application.video import catalog
from abist_kb.application.video.input_resolver import resolve_inputs
from abist_kb.application.video.project_store import (
    STATE_DRAFT,
    STATE_SUCCEEDED,
    check_input_usage,
    collection_selectors,
    create_project,
    load_inputs_manifest,
    load_project,
    primary_paths,
    read_state,
    write_state,
)
from abist_kb.infrastructure.db.schema import open_app_db
from abist_kb.infrastructure.db.video_projects_repo import VideoProjectRepository
from abist_kb.infrastructure.video.artifact_store import (
    create_video_dir,
    scene_dir,
    videos_dir,
)


@pytest.fixture
def env(tmp_path: Path):
    docs = tmp_path / "docs"
    (docs / "manuals").mkdir(parents=True)
    (docs / "decisions").mkdir(parents=True)
    (docs / "requirements.md").write_text("# 要件\n本文\n", encoding="utf-8")
    for i in range(4):
        (docs / "manuals" / f"手順{i}.md").write_text(f"# 手順{i}\n", encoding="utf-8")
    (docs / "decisions" / "d1.md").write_text("# 決定\n", encoding="utf-8")
    reports = tmp_path / "reports"
    conn = open_app_db(tmp_path / "app.sqlite")
    yield tmp_path, docs, reports, conn
    conn.close()


def _resolve(docs: Path, **inputs):
    return resolve_inputs(inputs, docs_dir=docs, max_docs_per_directory=2)


class TestArtifactStore:
    def test_video_dir_naming_matches_purring(self, tmp_path: Path) -> None:
        created = create_video_dir(videos_dir(tmp_path), "蛇腹の説明")
        assert ":" not in created.video_id, "Windows で使えない文字を含めない"
        assert created.dir.is_dir()
        assert (created.dir / "scenes").is_dir()

    def test_unique_ids_for_same_title(self, tmp_path: Path) -> None:
        root = videos_dir(tmp_path)
        first = create_video_dir(root, "同じ題")
        second = create_video_dir(root, "同じ題")
        assert first.video_id != second.video_id

    def test_scene_dir_cannot_escape(self, tmp_path: Path) -> None:
        created = create_video_dir(videos_dir(tmp_path), "t")
        target = scene_dir(created.dir, "../../evil")
        assert created.dir in target.parents


class TestCreateProject:
    def test_round_trip(self, env) -> None:
        _root, docs, reports, _conn = env
        resolved = _resolve(docs, kb_paths=["requirements.md"], kb_directories=["manuals"])
        result = create_project(
            {"title": "社内説明動画", "purpose": "新人向け"}, resolved, reports_dir=reports
        )
        assert result.ok, result.errors
        project = result.project
        assert project is not None

        # ディスクに全部揃う
        for name in ("project-spec.json", "inputs-manifest.json", "citations.json", "state.json"):
            assert (project.dir / name).is_file(), name

        spec = load_project(project.dir)
        assert spec["video_id"] == project.video_id
        assert spec["distribution"]["classification"] == "internal"

        # sources は解決経路から組み立てられる（利用者の手書きを信用しない）
        selections = {s["selection"] for s in spec["sources"]}
        assert "explicit_primary" in selections
        assert "collection_candidate" in selections

        manifest = load_inputs_manifest(project.dir)
        assert manifest["collections"][0]["selector"] == "manuals"
        assert read_state(project.dir)["state"] == STATE_DRAFT

    def test_unresolvable_input_fails_before_creating_anything(self, env) -> None:
        _root, docs, reports, _conn = env
        resolved = _resolve(docs, kb_paths=["missing.md"])
        result = create_project({"title": "失敗"}, resolved, reports_dir=reports)
        assert not result.ok
        assert result.code == "NO_RESOLVABLE_INPUT"
        assert not videos_dir(reports).exists(), "失敗時にディレクトリを作ってはいけない"

    def test_invalid_spec_fails(self, env) -> None:
        _root, docs, reports, _conn = env
        resolved = _resolve(docs, kb_paths=["requirements.md"])
        result = create_project({"title": ""}, resolved, reports_dir=reports)
        assert not result.ok
        assert result.code == "INVALID_VIDEO_SPEC"


class TestInputUsageCheck:
    """⚠ ディレクトリは全件ではなく「1件以上」で判定する。"""

    def _project(self, env):
        _root, docs, reports, _conn = env
        resolved = _resolve(docs, kb_paths=["requirements.md"], kb_directories=["manuals"])
        result = create_project({"title": "検証"}, resolved, reports_dir=reports)
        assert result.ok
        return result.project.spec

    def test_all_used_is_clean(self, env) -> None:
        spec = self._project(env)
        used = {s["path"] for s in spec["sources"]}
        assert check_input_usage(spec, used) == []

    def test_directory_needs_only_one_document(self, env) -> None:
        """ディレクトリ由来は1件使えば足りる（全件は要求しない）。"""
        spec = self._project(env)
        directory_paths = [
            s["path"] for s in spec["sources"] if s["origin"]["type"] == "kb_directory"
        ]
        assert len(directory_paths) >= 2, "前提: 複数件が選抜されている"
        used = {"requirements.md", directory_paths[0]}
        assert check_input_usage(spec, used) == [], "1件で足りるはずが未使用扱いされている"

    def test_unused_explicit_path_is_reported(self, env) -> None:
        spec = self._project(env)
        directory_paths = [
            s["path"] for s in spec["sources"] if s["origin"]["type"] == "kb_directory"
        ]
        errors = check_input_usage(spec, {directory_paths[0]})
        assert [e.code for e in errors] == ["PRIMARY_INPUT_UNUSED"]
        assert "requirements.md" in errors[0].message

    def test_unused_directory_is_reported(self, env) -> None:
        spec = self._project(env)
        errors = check_input_usage(spec, {"requirements.md"})
        assert [e.code for e in errors] == ["PRIMARY_COLLECTION_UNUSED"]
        assert "manuals" in errors[0].message

    def test_helpers(self, env) -> None:
        spec = self._project(env)
        assert primary_paths(spec) == ["requirements.md"]
        assert collection_selectors(spec) == ["manuals"]


class TestCatalog:
    def _create(self, env, title: str = "カタログ検証"):
        _root, docs, reports, conn = env
        resolved = _resolve(docs, kb_paths=["requirements.md"], kb_directories=["manuals"])
        result = create_project({"title": title}, resolved, reports_dir=reports)
        assert result.ok
        return result.project

    def test_record_and_read_back(self, env) -> None:
        root, _docs, reports, conn = env
        project = self._create(env)
        catalog.record_project(conn, project.dir, root_dir=root)

        repo = VideoProjectRepository(conn)
        record = repo.get(project.video_id)
        assert record is not None
        assert record["state"] == STATE_DRAFT
        assert record["source"] == "render"
        assert record["classification"] == "internal"
        assert not Path(record["project_dir"]).is_absolute(), "相対パスで保存する"

        inputs = repo.get_inputs(project.video_id)
        assert any(i["selection"] == "explicit_primary" and i["require_usage"] for i in inputs)
        assert any(
            i["selection"] == "collection_candidate" and not i["require_usage"] for i in inputs
        )
        assert any(i["origin_selector"] == "manuals" for i in inputs)

    def test_state_change_is_reflected(self, env) -> None:
        root, _docs, reports, conn = env
        project = self._create(env)
        write_state(project.dir, state=STATE_SUCCEEDED)
        catalog.record_project(conn, project.dir, root_dir=root)
        assert VideoProjectRepository(conn).get(project.video_id)["state"] == STATE_SUCCEEDED

    def test_reconcile_from_disk_rebuilds(self, env) -> None:
        """DB を消してもディスクから完全に再構築できる。"""
        root, _docs, reports, conn = env
        project = self._create(env)
        catalog.record_project(conn, project.dir, root_dir=root)
        repo = VideoProjectRepository(conn)
        repo.delete(project.video_id)
        assert repo.list()[1] == 0

        stats = catalog.reconcile_from_disk(conn, videos_dir(reports), root_dir=root)
        assert stats["upserted"] == 1
        rebuilt = repo.get(project.video_id)
        assert rebuilt is not None
        assert rebuilt["source"] == "disk"
        assert repo.get_inputs(project.video_id), "入力も再構築される"

    def test_removed_project_orphans_the_row(self, env) -> None:
        root, _docs, reports, conn = env
        project = self._create(env)
        catalog.record_project(conn, project.dir, root_dir=root)
        shutil.rmtree(project.dir)
        stats = catalog.reconcile_from_disk(conn, videos_dir(reports), root_dir=root)
        assert stats["orphaned"] == 1
        assert VideoProjectRepository(conn).list()[1] == 0

    def test_reconcile_is_idempotent(self, env) -> None:
        root, _docs, reports, conn = env
        self._create(env)
        catalog.reconcile_from_disk(conn, videos_dir(reports), root_dir=root)
        catalog.reconcile_from_disk(conn, videos_dir(reports), root_dir=root)
        assert VideoProjectRepository(conn).list()[1] == 1

    def test_spec_drift_detection(self, env) -> None:
        root, _docs, reports, conn = env
        project = self._create(env)
        catalog.record_project(conn, project.dir, root_dir=root)
        record = VideoProjectRepository(conn).get(project.video_id)

        drifted, spec = catalog.spec_drifted(record, root)
        assert drifted is False
        assert spec is not None

        (project.dir / "project-spec.json").write_text('{"changed": true}', encoding="utf-8")
        drifted, _ = catalog.spec_drifted(record, root)
        assert drifted is True

    def test_filters(self, env) -> None:
        root, _docs, reports, conn = env
        self._create(env, title="操作方法の説明")
        self._create(env, title="設計の注意点")
        catalog.reconcile_from_disk(conn, videos_dir(reports), root_dir=root)
        repo = VideoProjectRepository(conn)
        assert repo.list()[1] == 2
        assert repo.list(query="操作")[1] == 1
        assert repo.list(state=STATE_DRAFT)[1] == 2
        assert repo.list(source_path="manuals")[1] == 2
        assert repo.list(source_path="無関係")[1] == 0

    def test_mark_used(self, env) -> None:
        root, _docs, reports, conn = env
        project = self._create(env)
        catalog.record_project(conn, project.dir, root_dir=root)
        repo = VideoProjectRepository(conn)
        repo.mark_used(project.video_id, {"requirements.md"})
        inputs = {i["path"]: i["used"] for i in repo.get_inputs(project.video_id)}
        assert inputs["requirements.md"] is True
        assert all(v is False for k, v in inputs.items() if k != "requirements.md")

    def test_broken_spec_is_skipped(self, env) -> None:
        root, _docs, reports, conn = env
        broken = videos_dir(reports) / "20260101T000000Z-x-0000"
        broken.mkdir(parents=True)
        (broken / "project-spec.json").write_text("{ not json", encoding="utf-8")
        stats = catalog.reconcile_from_disk(conn, videos_dir(reports), root_dir=root)
        assert stats["scanned"] == 1
        assert stats["upserted"] == 0


class TestJobLifetime:
    def test_job_deletion_keeps_the_project(self, env) -> None:
        """ジョブ行が整理されても成果物の記録は残る（ON DELETE SET NULL）。"""
        root, _docs, reports, conn = env
        from abist_kb.infrastructure.jobs.repository import JobRepository

        resolved = _resolve(docs=_docs_of(env), kb_paths=["requirements.md"])
        result = create_project({"title": "ジョブ寿命"}, resolved, reports_dir=reports)
        job = JobRepository(conn).submit("render_video", {})
        catalog.record_project(conn, result.project.dir, root_dir=root, job_id=job.id)
        conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
        conn.commit()
        record = VideoProjectRepository(conn).get(result.project.video_id)
        assert record is not None
        assert record["job_id"] is None


def _docs_of(env) -> Path:
    return env[1]


def test_inputs_manifest_is_valid_json(env) -> None:
    _root, docs, reports, _conn = env
    resolved = _resolve(docs, kb_directories=["manuals"])
    result = create_project({"title": "manifest"}, resolved, reports_dir=reports)
    payload = json.loads((result.project.dir / "inputs-manifest.json").read_text(encoding="utf-8"))
    assert set(payload) == {"inputs", "collections", "warnings"}
