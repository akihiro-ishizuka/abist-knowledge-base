"""M1 Task 5 (Part 3) で生成した `tests/fixtures/capture-manifest.json` の健全性検証。

このファイルは `tests/fixtures/capture/build-manifest.mjs` が `tests/fixtures/`
配下の全カテゴリを集計して生成したものである。ここでは:
  - manifest に記録された各 SHA-256 が実ファイルと一致すること(brief必須要件)。
  - `tests/fixtures/` 配下の全ファイルが manifest に現れること(孤児が無いこと、
    brief必須要件: 「除外は黙って行わない」の裏返しとして「網羅も嘘をつかない」)。
  - 除外・逸脱レジストリ(`exclusions_registry`)・非決定項目一覧
    (`nondeterministic_items`)が理由付きで記録されていること。
  - 旧リポジトリ情報・採取日時(git log由来)の形式が壊れていないこと。
を検証する。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST_PATH = FIXTURES_DIR / "capture-manifest.json"

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _load() -> dict[str, Any]:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert data, f"capture-manifest.json が空: {MANIFEST_PATH}"
    return data


def _all_fixture_files_on_disk() -> set[str]:
    """tests/fixtures/ 配下の全ファイル(capture-manifest.json自身を除く)を
    そのファイル自身から見た相対パス(posix区切り)の集合として返す。"""
    paths = set()
    for path in FIXTURES_DIR.rglob("*"):
        if path.is_file() and path != MANIFEST_PATH:
            paths.add(path.relative_to(FIXTURES_DIR).as_posix())
    return paths


def test_schema_and_top_level_fields() -> None:
    data = _load()
    assert data["schema"] == 1
    assert isinstance(data["generated_by"], str) and "build-manifest.mjs" in data["generated_by"]
    assert isinstance(data["categories"], dict) and data["categories"]
    assert isinstance(data["exclusions_registry"], list)
    assert isinstance(data["nondeterministic_items"], list)


def test_node_version_is_recorded_in_expected_format() -> None:
    data = _load()
    assert re.match(r"^v\d+\.\d+\.\d+$", data["node_version"]), (
        f"node_version の形式が想定外: {data['node_version']!r}"
    )


def test_old_repo_path_and_head_are_recorded() -> None:
    data = _load()
    old_repo = data["old_repo"]
    assert isinstance(old_repo["path"], str) and "multi-source-knowledge-base" in old_repo["path"]
    if old_repo["git_head"] is not None:
        assert _GIT_SHA_RE.match(old_repo["git_head"]), (
            f"git_head が40桁16進数ではない: {old_repo['git_head']!r}"
        )
        assert old_repo["git_head_error"] is None
    else:
        assert isinstance(old_repo["git_head_error"], str) and old_repo["git_head_error"]


def test_every_manifest_sha256_matches_file_on_disk() -> None:
    """brief必須要件: manifest の各 SHA-256 が実ファイルと一致すること。"""
    data = _load()
    checked = 0
    for category, info in data["categories"].items():
        for entry in info["files"]:
            full_path = FIXTURES_DIR / entry["path"]
            assert full_path.is_file(), (
                f"{category}/{entry['path']}: manifest記載のファイルが実在しない"
            )
            actual_bytes = full_path.stat().st_size
            assert actual_bytes == entry["bytes"], (
                f"{entry['path']}: バイト数が食い違う"
                f"(manifest={entry['bytes']}, 実際={actual_bytes})"
            )
            actual_sha256 = hashlib.sha256(full_path.read_bytes()).hexdigest()
            assert actual_sha256 == entry["sha256"], (
                f"{entry['path']}: SHA-256が実ファイルと食い違う"
                f"(manifest={entry['sha256']}, 実際={actual_sha256})"
            )
            assert _SHA256_HEX_RE.match(entry["sha256"])
            checked += 1
    assert checked > 0


def test_every_fixture_file_on_disk_appears_in_manifest_no_orphans() -> None:
    """brief必須要件: fixture 全体を網羅していること(孤児が無いこと)。"""
    data = _load()
    manifest_paths = {
        entry["path"] for info in data["categories"].values() for entry in info["files"]
    }
    disk_paths = _all_fixture_files_on_disk()
    orphans_on_disk = disk_paths - manifest_paths
    assert not orphans_on_disk, f"manifestに現れないファイルがdiskにある(孤児): {orphans_on_disk}"
    missing_from_disk = manifest_paths - disk_paths
    assert not missing_from_disk, (
        f"manifestに記載されているがdiskに無いファイル: {missing_from_disk}"
    )


def test_category_file_count_and_total_bytes_are_internally_consistent() -> None:
    data = _load()
    for category, info in data["categories"].items():
        assert info["file_count"] == len(info["files"]), (
            f"{category}: file_countが実際のfiles件数と食い違う"
        )
        assert info["total_bytes"] == sum(f["bytes"] for f in info["files"]), (
            f"{category}: total_bytesが実際の合計と食い違う"
        )
        paths = [f["path"] for f in info["files"]]
        assert paths == sorted(paths), f"{category}: filesがpath昇順にソートされていない"


def test_expected_categories_are_present() -> None:
    """brief が名指しした6カテゴリ(kernel/real-docs/mcp/eval/embedding/html)が
    すべて manifest に存在すること。"""
    data = _load()
    expected = {"kernel", "real-docs", "mcp", "eval", "embedding", "html"}
    assert expected.issubset(set(data["categories"])), (
        f"brief必須の6カテゴリが揃っていない: 不足={expected - set(data['categories'])}"
    )


def test_capture_dates_are_either_valid_iso8601_or_explicitly_noted_as_uncommitted() -> None:
    """各カテゴリの採取日時は「有効なISO8601日時+40桁コミットSHA」か、
    「コミット前で null + 理由付きnote」のどちらかであるべきで、沈黙した
    欠落があってはならない。"""
    data = _load()
    for category, info in data["capture_dates"].items():
        if info["date"] is not None:
            datetime.fromisoformat(info["date"])  # 不正なら例外で失敗する
            assert _GIT_SHA_RE.match(info["commit"]), f"{category}: commitが40桁16進数ではない"
        else:
            assert info["commit"] is None
            assert isinstance(info.get("note"), str) and info["note"], (
                f"{category}: dateがnullなのにnoteが無い(理由不明のまま欠落している)"
            )


def test_exclusions_registry_entries_have_reasons() -> None:
    """brief必須要件: 除外は理由付きで記録し、黙って落とさない。"""
    data = _load()
    exclusions = data["exclusions_registry"]
    assert exclusions, "exclusions_registryが空(Task 1〜4で判明した既知の除外が反映されていない)"
    for entry in exclusions:
        assert isinstance(entry.get("id"), str) and entry["id"]
        assert isinstance(entry.get("category"), str) and entry["category"]
        assert isinstance(entry.get("source_task"), str) and entry["source_task"]
        assert isinstance(entry.get("description"), str) and len(entry["description"]) > 10
        assert isinstance(entry.get("reference"), str) and entry["reference"]


def test_exclusions_registry_covers_known_facts_from_prior_tasks() -> None:
    """Task 1〜4のレポートで判明した既知の除外・逸脱が、それぞれ1件以上
    exclusions_registryに反映されていること(consolidateし忘れて欠落しないための固定)。"""
    data = _load()
    ids = {entry["id"] for entry in data["exclusions_registry"]}
    expected_ids = {
        "real_docs_secret_pattern_key_value",
        "real_docs_64kb_oversize",
        "mcp_add_web_batch_skipped",
        "mcp_download_git_deviation",
        "mcp_sdk_validation_error_caveat",
        "kb_search_sandbox_copy_execution_environment",
        "eval_baseline_plain_json_deviation",
        "web_and_reference_layers_empty_corrected",
    }
    missing = expected_ids - ids
    assert not missing, f"既知の除外・逸脱がexclusions_registryから欠落している: {missing}"


def test_nondeterministic_items_have_notes() -> None:
    data = _load()
    items = data["nondeterministic_items"]
    assert items, "nondeterministic_itemsが空"
    for item in items:
        assert isinstance(item.get("field"), str) and item["field"]
        assert isinstance(item.get("note"), str) and len(item["note"]) > 10
