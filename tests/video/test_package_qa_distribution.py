"""Phase 8〜12: メタデータ・サムネイル・QA・承認・配布・GC・利用量。

このファイルが守る性質:

- **QA FAIL でも成果物は消えない**（承認だけがブロックされる）
- **承認は成果物ハッシュにバインドされ、変更で自動的に無効になる**
- **公開候補は既定 false・降下のみ**（機密ソースがあれば true を要求しても false）
- **GC は既定 dry-run**、承認済みは保護される
- QA レポートに秘密情報の**値**が載らない
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from abist_kb.application.video import cost_report, gc
from abist_kb.application.video.approval import (
    APPROVAL_FILE,
    approve,
    invalidate,
    verify_approval,
)
from abist_kb.application.video.distribution import (
    MANUAL_PACK_FILES,
    build_manual_publish_pack,
    can_transition,
    evaluate,
    evaluate_public_candidate,
    request_public_review,
    write_manual_publish_note,
)
from abist_kb.application.video.qa import (
    STATUS_FAIL,
    STATUS_PASS,
    run_qa,
    scan_secrets,
    write_report,
)
from abist_kb.application.video.resume import (
    plan_resume,
    record_scene_digest,
    savings,
    scene_spec_digest,
)
from abist_kb.application.video.thumbnail import (
    MIN_HEIGHT,
    MIN_WIDTH,
    build_thumbnail,
)
from abist_kb.application.video.video_metadata import (
    INTERNAL_ONLY_NOTICE,
    MAX_TITLE_CHARS,
    build_chapters,
    build_metadata,
)


def _spec(**overrides) -> dict:
    spec = {
        "schema_version": "1.0",
        "video_id": "20260101T000000Z-video-abcd",
        "title": "社内説明動画",
        "purpose": "新人向けの操作説明",
        "language": "ja",
        "format": {
            "aspect_ratio": "16:9",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "target_duration_sec": {"min": 120, "max": 180},
        },
        "distribution": {
            "classification": "internal",
            "audience": [],
            "allowed_groups": [],
            "public_candidate": False,
            "public_review_status": "not_requested",
        },
        "sources": [
            {
                "id": "src1",
                "path": "a/b.md",
                "selection": "explicit_primary",
                "sensitivity": "internal",
            }
        ],
        "scenes": [
            {"id": "s01", "kind": "title", "title": "表紙", "scene_spec": {"beats": []}},
            {
                "id": "s02",
                "kind": "chapter",
                "title": "第1章のねらい",
                "chapter_index": 1,
                "scene_spec": {"beats": []},
            },
            {
                "id": "s03",
                "kind": "key_points",
                "title": "要点",
                "narration": {"text": "要点を説明します。"},
                "scene_spec": {
                    "beats": [{"type": "statement", "text": "要点", "source_refs": ["s1"]}],
                    "sources": [{"path": "a/b.md"}],
                },
            },
            {
                "id": "s04",
                "kind": "chapter",
                "title": "第2章のねらい",
                "chapter_index": 2,
                "scene_spec": {"beats": []},
            },
        ],
    }
    spec.update(overrides)
    return spec


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """QA が pass する最小のプロジェクト（動画そのものは無い）。"""
    root = tmp_path / "project"
    (root / "subtitles").mkdir(parents=True)
    (root / "audio").mkdir(parents=True)
    (root / "project-spec.json").write_text(
        json.dumps(_spec(), ensure_ascii=False), encoding="utf-8"
    )
    (root / "citations.json").write_text(
        json.dumps(
            {
                "sources": [{"id": "src1", "path": "a/b.md"}],
                "sound_attributions": [
                    {"sound_id": "x", "license": "in-house", "sha256": "0" * 64}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "audio" / "sound-cues.json").write_text(
        json.dumps({"cues": [{"scene_id": "s03", "t_sec": 1.0, "sha256": "0" * 64}]}),
        encoding="utf-8",
    )
    (root / "subtitles" / "narration.srt").write_text(
        "1\n00:00:00,000 --> 00:00:03,000\n要点\n", encoding="utf-8"
    )
    return root


class TestChaptersAndMetadata:
    def test_chapters_start_at_zero(self) -> None:
        scenes = _spec()["scenes"]
        chapters = build_chapters(scenes, {"s02": 8.0, "s04": 60.0}, title="社内説明動画")
        assert chapters[0].start_sec == 0.0
        assert [c.start_sec for c in chapters] == sorted(c.start_sec for c in chapters)

    def test_timestamps_are_formatted_for_manual_upload(self) -> None:
        chapters = build_chapters(_spec()["scenes"], {"s02": 65.0, "s04": 3725.0}, title="t")
        stamps = [c.timestamp for c in chapters]
        assert stamps[0] == "00:00"
        assert "01:05" in stamps
        assert "1:02:05" in stamps

    def test_internal_notice_is_added_when_not_public(self, project: Path) -> None:
        result = build_metadata(_spec(), offsets={"s02": 8.0, "s04": 60.0}, duration_sec=132.0)
        assert result.ok
        assert INTERNAL_ONLY_NOTICE in result.metadata["description"]
        assert result.metadata["suggested_visibility_note"] == "manual_review_required"
        assert result.metadata["upload"]["method"] == "manual"

    def test_description_carries_chapters_and_sources(self) -> None:
        result = build_metadata(_spec(), offsets={"s02": 8.0, "s04": 60.0}, duration_sec=132.0)
        assert "【チャプター】" in result.metadata["description"]
        assert "a/b.md" in result.metadata["description"]

    def test_capture_commit_sha_is_transcribed(self) -> None:
        result = build_metadata(
            _spec(),
            offsets={"s02": 8.0, "s04": 60.0},
            duration_sec=132.0,
            capture_commit_sha="a" * 40,
        )
        assert "a" * 40 in result.metadata["description"]
        assert result.metadata["capture"]["resolved_commit_sha"] == "a" * 40

    def test_too_long_title_is_invalid_metadata(self) -> None:
        result = build_metadata(
            _spec(title="あ" * (MAX_TITLE_CHARS + 1)), offsets={}, duration_sec=10.0
        )
        assert not result.ok
        assert result.code == "INVALID_METADATA"

    def test_metadata_is_deterministic(self) -> None:
        first = build_metadata(_spec(), offsets={"s02": 8.0}, duration_sec=132.0)
        second = build_metadata(_spec(), offsets={"s02": 8.0}, duration_sec=132.0)
        assert first.metadata == second.metadata


class TestThumbnail:
    def test_fallback_always_produces_a_file(self, tmp_path: Path) -> None:
        """動画が無くても**必ず**サムネイルを残す。"""
        result = build_thumbnail(None, tmp_path)
        assert result.ok and result.fallback
        assert result.path.is_file()
        assert (result.width, result.height) == (MIN_WIDTH, MIN_HEIGHT)
        assert result.path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_missing_video_file_also_falls_back(self, tmp_path: Path) -> None:
        result = build_thumbnail(tmp_path / "nope.mp4", tmp_path)
        assert result.ok and result.fallback


class TestQa:
    def test_secret_scan_does_not_record_values(self) -> None:
        findings = scan_secrets("token=sk-abcdefghijklmnopqrstuvwxyz0123")
        assert findings
        assert all(set(f) == {"kind", "offset", "length"} for f in findings)
        assert all("sk-" not in str(v) for f in findings for v in f.values())

    def test_secret_scan_flags_the_report_as_fail(self, project: Path) -> None:
        (project / "citations.json").write_text(
            json.dumps({"sources": [{"path": "a/b.md"}], "note": "sk-abcdefghijklmnopqrst0123"}),
            encoding="utf-8",
        )
        report = run_qa(project, spec=_spec())
        secret = next(c for c in report.checks if c.id == "secret_scan")
        assert secret.status == STATUS_FAIL
        assert "sk-" not in secret.detail, "レポートに値が漏れている"

    def test_missing_output_fails_playable_but_keeps_artifacts(self, project: Path) -> None:
        report = run_qa(project, spec=_spec())
        assert not report.ok
        assert any(c.id == "playable" and c.status == STATUS_FAIL for c in report.checks)
        # 成果物は消えない
        assert (project / "citations.json").is_file()
        assert (project / "subtitles" / "narration.srt").is_file()

    def test_citations_and_sound_checks_pass(self, project: Path) -> None:
        report = run_qa(project, spec=_spec())
        by_id = {c.id: c for c in report.checks}
        assert by_id["citations_complete"].status == STATUS_PASS
        assert by_id["sfx_source_integrity"].status == STATUS_PASS
        assert by_id["sfx_attribution"].status == STATUS_PASS

    def test_human_items_are_never_auto_passed(self, project: Path) -> None:
        report = run_qa(project, spec=_spec())
        assert "preview_approval" in report.human_required
        assert "public_candidate_review" in report.human_required

    def test_report_round_trips(self, project: Path) -> None:
        report = run_qa(project, spec=_spec())
        path = write_report(report, project)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["ok"] == report.ok
        assert len(payload["checks"]) == len(report.checks)


class TestApproval:
    def _ready(self, project: Path) -> None:
        (project / "output.mp4").write_bytes(b"fake-mp4")
        (project / "video-metadata.json").write_text("{}", encoding="utf-8")
        (project / "qa-report.json").write_text(json.dumps({"ok": True}), encoding="utf-8")

    def test_requires_all_artifacts(self, project: Path) -> None:
        result = approve(project, approver="me@example.invalid")
        assert not result.ok
        assert result.code == "MANUAL_PACK_INCOMPLETE"

    def test_blocked_while_qa_fails(self, project: Path) -> None:
        self._ready(project)
        (project / "qa-report.json").write_text(json.dumps({"ok": False}), encoding="utf-8")
        result = approve(project, approver="me@example.invalid")
        assert not result.ok
        assert result.code == "QA_FAILED"

    def test_approval_binds_to_artifact_hashes(self, project: Path) -> None:
        self._ready(project)
        result = approve(project, approver="me@example.invalid")
        assert result.ok
        assert len(result.record["output_mp4_sha256"]) == 64
        assert verify_approval(project).ok

    def test_rerender_invalidates_the_approval(self, project: Path) -> None:
        """再レンダーしたら**自動的に**無効になる（人が消す必要がない）。"""
        self._ready(project)
        approve(project, approver="me@example.invalid")
        (project / "output.mp4").write_bytes(b"different-mp4")
        verified = verify_approval(project)
        assert not verified.ok
        assert verified.code == "APPROVAL_STALE"
        assert "output_mp4_sha256" in verified.mismatched

    def test_metadata_edit_invalidates_the_approval(self, project: Path) -> None:
        self._ready(project)
        approve(project, approver="me@example.invalid")
        (project / "video-metadata.json").write_text('{"title": "変更"}', encoding="utf-8")
        assert verify_approval(project).code == "APPROVAL_STALE"

    def test_distribution_change_invalidates_the_approval(self, project: Path) -> None:
        self._ready(project)
        approve(project, approver="me@example.invalid")
        spec = _spec()
        spec["distribution"]["audience"] = ["team-a"]
        (project / "project-spec.json").write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8"
        )
        assert verify_approval(project).code == "APPROVAL_STALE"

    def test_expired_approval_is_invalid(self, project: Path) -> None:
        self._ready(project)
        past = datetime.now(UTC) - timedelta(days=30)
        approve(project, approver="me@example.invalid", now=past, valid_days=7)
        assert verify_approval(project).code == "APPROVAL_STALE"

    def test_invalidate_records_the_reason(self, project: Path) -> None:
        self._ready(project)
        approve(project, approver="me@example.invalid")
        assert invalidate(project, "シーンを再生成したため")
        record = json.loads((project / APPROVAL_FILE).read_text(encoding="utf-8"))
        assert record["revoked"] is True
        assert record["revoked_reason"]


class TestDistribution:
    def test_default_is_internal_and_not_public(self) -> None:
        candidate, reasons = evaluate_public_candidate(_spec())
        assert candidate is False
        assert reasons[0].code == "DEFAULT_INTERNAL"

    def test_confidential_source_forces_false(self) -> None:
        spec = _spec()
        spec["sources"][0]["sensitivity"] = "confidential"
        candidate, reasons = evaluate_public_candidate(spec, requested=True)
        assert candidate is False
        assert any(r.code == "SOURCE_SENSITIVITY" for r in reasons)

    def test_secret_scan_failure_forces_false(self) -> None:
        qa = {"checks": [{"id": "secret_scan", "status": "fail", "detail": "検出"}]}
        candidate, reasons = evaluate_public_candidate(_spec(), qa=qa, requested=True)
        assert candidate is False
        assert any(r.code == "SECRET_SCAN" for r in reasons)

    def test_missing_license_forces_false(self) -> None:
        citations = {"sound_attributions": [{"sound_id": "x", "license": ""}]}
        candidate, reasons = evaluate_public_candidate(_spec(), citations=citations, requested=True)
        assert candidate is False
        assert any(r.code == "MISSING_LICENSE" for r in reasons)

    def test_clean_project_is_only_eligible_for_review(self) -> None:
        candidate, reasons = evaluate_public_candidate(_spec(), requested=True)
        assert candidate is True
        assert reasons[0].code == "ELIGIBLE_FOR_REVIEW"
        assert "公開可否は人が判断" in reasons[0].detail

    def test_manual_pack_reports_missing_files(self, project: Path) -> None:
        present, missing = build_manual_publish_pack(project)
        assert "citations.json" in present
        assert "output.mp4" in missing
        assert set(missing) <= set(MANUAL_PACK_FILES)

    def test_report_says_public_candidate_is_not_permission(self, project: Path) -> None:
        decision = evaluate(_spec(), project)
        payload = decision.to_dict()
        assert payload["public_candidate"] is False
        assert "公開可否ではない" in payload["public_candidate_meaning"]
        assert payload["manual_publish_checklist"]

    def test_public_review_is_denied_for_confidential(self, project: Path) -> None:
        spec = _spec()
        spec["sources"][0]["sensitivity"] = "confidential"
        decision = request_public_review(spec, project)
        assert decision.code == "PUBLIC_CANDIDATE_DENIED"
        assert decision.public_review_status == "not_requested"

    def test_review_transitions(self) -> None:
        assert can_transition("not_requested", "submitted")
        assert not can_transition("not_requested", "approved_for_manual_publish")
        assert can_transition("submitted", "rejected")

    def test_manual_publish_note_is_written(self, project: Path) -> None:
        decision = evaluate(_spec(), project)
        path = write_manual_publish_note(decision, project)
        text = path.read_text(encoding="utf-8")
        assert "手動アップロード" in text
        assert "外部へ送信しません" in text

    def test_no_youtube_module_exists(self) -> None:
        """将来バックログ（YouTube API / 自動投稿）が実装されていないこと。"""
        root = Path(__file__).resolve().parents[2] / "src"
        offenders = [
            p
            for p in root.rglob("*.py")
            if "youtube" in p.name.lower() or "oauth" in p.name.lower()
        ]
        assert offenders == [], f"YouTube 関連モジュールが追加されている: {offenders}"


class TestResume:
    def test_missing_state_falls_back_to_full_rerun(self, project: Path) -> None:
        plan = plan_resume(project)
        assert plan.ok and plan.full_rerun

    def test_unchanged_scene_is_reusable(self, project: Path) -> None:
        (project / "state.json").write_text(json.dumps({"state": "succeeded"}), encoding="utf-8")
        scene_spec = {"beats": [], "title": "表紙"}
        spec = _spec()
        spec["scenes"] = [{"id": "s01", "kind": "title", "scene_spec": scene_spec}]
        (project / "project-spec.json").write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8"
        )
        target = project / "scenes" / "s01"
        target.mkdir(parents=True)
        (target / "output.mp4").write_bytes(b"x")
        record_scene_digest(project, "s01", scene_spec)

        plan = plan_resume(project)
        assert plan.reusable_ids == ["s01"]
        assert savings(plan)["reuse_ratio"] == 1.0

    def test_changed_scene_is_rerendered(self, project: Path) -> None:
        (project / "state.json").write_text(json.dumps({"state": "succeeded"}), encoding="utf-8")
        spec = _spec()
        spec["scenes"] = [{"id": "s01", "kind": "title", "scene_spec": {"beats": [], "v": 2}}]
        (project / "project-spec.json").write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8"
        )
        target = project / "scenes" / "s01"
        target.mkdir(parents=True)
        (target / "output.mp4").write_bytes(b"x")
        record_scene_digest(project, "s01", {"beats": [], "v": 1})

        plan = plan_resume(project)
        assert plan.rerender_ids == ["s01"]

    def test_forced_scene_is_rerendered(self, project: Path) -> None:
        (project / "state.json").write_text(json.dumps({"state": "succeeded"}), encoding="utf-8")
        plan = plan_resume(project, force_scene_ids={"s03"})
        assert "s03" in plan.rerender_ids

    def test_digest_includes_duration(self) -> None:
        """尺が変われば別物として扱う（映像も変わるため）。"""
        assert scene_spec_digest({"beats": [], "min_duration_sec": 8.0}) != scene_spec_digest(
            {"beats": [], "min_duration_sec": 20.0}
        )


class TestGcAndCost:
    def _make(self, reports: Path, name: str, *, days_old: int) -> Path:
        target = reports / "videos" / name
        target.mkdir(parents=True)
        stamp = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
        (target / "project-spec.json").write_text(
            json.dumps(_spec(video_id=name), ensure_ascii=False), encoding="utf-8"
        )
        (target / "state.json").write_text(
            json.dumps({"state": "succeeded", "updated_at": stamp, "created_at": stamp}),
            encoding="utf-8",
        )
        (target / "output.mp4").write_bytes(b"x" * 2048)
        return target

    def test_dry_run_deletes_nothing(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        old = self._make(reports, "old", days_old=400)
        result = gc.run_gc(reports, keep_latest=0, ttl_days=90)
        assert result.dry_run
        assert [c.video_id for c in result.deleted] == ["old"]
        assert old.is_dir(), "dry-run なのに消えている"

    def test_confirmed_deletes(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        old = self._make(reports, "old", days_old=400)
        result = gc.run_gc(reports, keep_latest=0, ttl_days=90, confirmed=True)
        assert not result.dry_run
        assert not old.exists()
        assert result.freed_bytes > 0

    def test_latest_generations_are_kept(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        self._make(reports, "old", days_old=400)
        result = gc.run_gc(reports, keep_latest=10, ttl_days=1)
        assert result.deleted == []

    def test_keep_marker_protects(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        target = self._make(reports, "old", days_old=400)
        (target / gc.KEEP_MARKER).write_text("残す", encoding="utf-8")
        result = gc.run_gc(reports, keep_latest=0, ttl_days=90, confirmed=True)
        assert target.is_dir()
        assert any(c.protected for c in result.kept)

    def test_approved_project_is_protected(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        target = self._make(reports, "old", days_old=400)
        (target / "video-metadata.json").write_text("{}", encoding="utf-8")
        (target / "qa-report.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
        assert approve(target, approver="me@example.invalid").ok
        result = gc.run_gc(reports, keep_latest=0, ttl_days=90, confirmed=True)
        assert target.is_dir(), "有効な承認があるのに削除された"
        assert result.deleted == []

    def test_gc_never_touches_data_dir(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        self._make(reports, "old", days_old=400)
        data = tmp_path / "data"
        data.mkdir()
        (data / "secrets.txt").write_text("keep", encoding="utf-8")
        gc.run_gc(reports, keep_latest=0, ttl_days=90, confirmed=True)
        assert (data / "secrets.txt").is_file()

    def test_cost_report_counts_narration_chars(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        self._make(reports, "a", days_old=1)
        report = cost_report.build_report(reports)
        assert report.videos
        assert report.total_narration_chars == len("要点を説明します。")
        assert report.estimated_cost is None, "単価未設定なのに金額を出している"
        assert any("単価" in w for w in report.warnings)

    def test_cost_report_estimates_only_with_a_unit_price(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        self._make(reports, "a", days_old=1)
        report = cost_report.build_report(reports, tts_unit_price_per_1k_chars=2.0)
        assert report.estimated_cost is not None
        assert report.estimated_cost["currency"] == "JPY"

    def test_cost_report_is_saved_under_benchmarks(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        self._make(reports, "a", days_old=1)
        report = cost_report.build_report(reports)
        path = cost_report.write_report(report, tmp_path)
        assert path.is_file()
        assert path.parent == tmp_path / cost_report.REPORT_DIR
