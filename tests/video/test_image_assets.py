"""持ち込み画像の取り込み契約。

台本作成時に用意した画像（エージェントが描いた図、撮ったスクリーンショット等）を
動画に載せる。原本はリポジトリ外にあってよいが、**プロジェクトの中へ複製し、
sha256 とライセンスを必ず記録する**。後から「この画は何だったのか」を辿れない
まま社内配布する状態を作らない。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from abist_kb.application.video.image_assets import (
    ALLOWED_IMAGE_SUFFIXES,
    IMAGE_ASSETS_DIR,
    ingest_image_assets,
    validate_image_asset_refs,
)

# 1x1 の PNG（最小の正当なファイル）。
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082"
)


@pytest.fixture
def source_png(tmp_path: Path) -> Path:
    path = tmp_path / "outside" / "diagram.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(_PNG)
    return path


def _asset(path: Path, **overrides) -> dict:
    asset = {"id": "fig1", "path": str(path), "license": "in-house", "caption": "構成図"}
    asset.update(overrides)
    return asset


# -- 取り込み -----------------------------------------------------------------------


def test_asset_is_copied_into_the_project(tmp_path: Path, source_png: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = ingest_image_assets([_asset(source_png)], project)

    assert result.ok, result.errors
    copied = project / IMAGE_ASSETS_DIR / "fig1.png"
    assert copied.is_file()
    assert copied.read_bytes() == _PNG


def test_sha256_is_recorded(tmp_path: Path, source_png: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = ingest_image_assets([_asset(source_png)], project)

    assert result.assets[0]["sha256"] == hashlib.sha256(_PNG).hexdigest()
    assert result.assets[0]["origin"] == "author_supplied"


def test_original_path_is_recorded_for_audit(tmp_path: Path, source_png: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = ingest_image_assets([_asset(source_png)], project)

    assert result.assets[0]["source_path"] == str(source_png)


def test_ingestion_is_deterministic(tmp_path: Path, source_png: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    first = ingest_image_assets([_asset(source_png)], project)
    second = ingest_image_assets([_asset(source_png)], project)

    assert first.assets == second.assets


# -- 受け付けない入力 -----------------------------------------------------------------


def test_missing_file_is_reported(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = ingest_image_assets([_asset(tmp_path / "nope.png")], project)

    assert not result.ok
    assert result.errors[0]["code"] == "IMAGE_ASSET_NOT_FOUND"


def test_license_is_required(tmp_path: Path, source_png: Path) -> None:
    """出所不明の画像を社内配布物へ入れない。"""
    project = tmp_path / "project"
    project.mkdir()

    result = ingest_image_assets([_asset(source_png, license="")], project)

    assert not result.ok
    assert result.errors[0]["code"] == "IMAGE_ASSET_LICENSE_REQUIRED"


def test_unsupported_format_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    bad = tmp_path / "notes.txt"
    bad.write_text("not an image", encoding="utf-8")

    result = ingest_image_assets([_asset(bad)], project)

    assert not result.ok
    assert result.errors[0]["code"] == "IMAGE_ASSET_UNSUPPORTED_FORMAT"
    assert ".png" in ALLOWED_IMAGE_SUFFIXES


def test_duplicate_ids_are_rejected(tmp_path: Path, source_png: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = ingest_image_assets([_asset(source_png), _asset(source_png)], project)

    assert not result.ok
    assert result.errors[0]["code"] == "DUPLICATE_IMAGE_ASSET_ID"


def test_id_cannot_escape_the_project(tmp_path: Path, source_png: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = ingest_image_assets([_asset(source_png, id="../../evil")], project)

    assert not result.ok
    assert result.errors[0]["code"] == "INVALID_IMAGE_ASSET_ID"


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    huge = tmp_path / "huge.png"
    huge.write_bytes(_PNG + b"\x00" * (30 * 1024 * 1024))

    result = ingest_image_assets([_asset(huge)], project)

    assert not result.ok
    assert result.errors[0]["code"] == "IMAGE_ASSET_TOO_LARGE"


# -- 台本からの参照 -------------------------------------------------------------------


def test_unknown_asset_reference_is_reported() -> None:
    scenes = [
        {
            "id": "s03",
            "scene_spec": {"beats": [{"type": "image", "asset_id": "missing", "caption": "図"}]},
        }
    ]
    errors = validate_image_asset_refs(scenes, registered_ids={"fig1"})
    assert [e["code"] for e in errors] == ["UNKNOWN_IMAGE_ASSET"]
    assert errors[0]["sceneId"] == "s03"


def test_registered_asset_reference_passes() -> None:
    scenes = [
        {
            "id": "s03",
            "scene_spec": {"beats": [{"type": "image", "asset_id": "fig1", "caption": "図"}]},
        }
    ]
    assert validate_image_asset_refs(scenes, registered_ids={"fig1"}) == []


def test_capture_supplied_images_are_left_alone() -> None:
    """キャプチャ経路が付けた `path` 直指定の image beat は対象外。"""
    scenes = [
        {"id": "s03", "scene_spec": {"beats": [{"type": "image", "path": "shot.png"}]}},
    ]
    assert validate_image_asset_refs(scenes, registered_ids=set()) == []


def test_reference_is_resolved_to_a_scene_relative_path(tmp_path: Path, source_png: Path) -> None:
    """`image.path` は scene-spec.json からの相対（キャプチャ経路と同じ約束）。"""
    from abist_kb.application.video.image_assets import attach_image_assets

    project = tmp_path / "project"
    project.mkdir()
    ingested = ingest_image_assets([_asset(source_png)], project)
    scenes = [
        {"id": "s03", "scene_spec": {"beats": [{"type": "image", "asset_id": "fig1"}]}},
    ]

    attach_image_assets(scenes, ingested.assets)

    beat = scenes[0]["scene_spec"]["beats"][0]
    assert beat["path"] == "../../assets/images/fig1.png"
    assert beat["license"] == "in-house"


# -- QA: 来歴が残っているか ----------------------------------------------------------


def test_qa_flags_an_image_asset_missing_from_the_project(tmp_path: Path) -> None:
    from abist_kb.application.video.qa import STATUS_FAIL, run_qa

    project = tmp_path / "project"
    (project / "audio").mkdir(parents=True)
    spec = {
        "title": "t",
        "image_assets": [
            {"id": "fig1", "path": "assets/images/fig1.png", "sha256": "a" * 64, "license": "x"}
        ],
    }
    report = run_qa(project, spec=spec)
    check = next(c for c in report.checks if c.id == "image_provenance")
    assert check.status == STATUS_FAIL


def test_qa_passes_when_the_image_and_its_record_are_present(
    tmp_path: Path, source_png: Path
) -> None:
    from abist_kb.application.video.qa import STATUS_PASS, run_qa

    project = tmp_path / "project"
    (project / "audio").mkdir(parents=True)
    ingested = ingest_image_assets([_asset(source_png)], project)
    report = run_qa(project, spec={"title": "t", "image_assets": ingested.assets})
    check = next(c for c in report.checks if c.id == "image_provenance")
    assert check.status == STATUS_PASS
