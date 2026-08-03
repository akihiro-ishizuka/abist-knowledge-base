"""M2 追加採取(sync_status_for ギャップ解消)の
`tests/fixtures/kernel/sync-status-for.json` の健全性検証。

`capture-sync-status.mjs` は、11の同期アクション(SYNC_ACTIONS)から5の
sync_status 列挙値(SYNC_STATUSES)へのマッピングを、旧 Node システムの
export されていない3つの関数(download-article.js の syncStatusFor /
download-web.js の downloadPage 内インラインマッピング / download-git.js の
recordMarkdownFiles)について、savePost() の直接実行・download-web.js/
download-git.js の子プロセス実行で観測した結果を記録したもの。
ここでのテスト対象は Python 実装ではなく、旧 Node システムを実行して採取した
fixture 自体。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "kernel" / "sync-status-for.json"

# tools/lib/sync-planner.js の SYNC_ACTIONS (11個)。tests/fixtures/kernel/sync-planner.json の
# sync_actions_enum ケースと同じ値。
ALL_11_ACTIONS = {
    "create",
    "update",
    "unchanged",
    "local_modified",
    "conflict",
    "conflict_overwritten",
    "adopt",
    "unknown_local",
    "missing",
    "orphan",
    "error",
}

# tools/lib/metadata-schema.js の SYNC_STATUSES (5個)。
ALL_5_SYNC_STATUSES = {"synced", "modified_local", "conflict", "source_missing", "error"}

VALID_DERIVATIONS = {
    "executed_sandbox",
    "executed_sandbox_http",
    "executed_sandbox_local_repo",
    "read_only_no_safe_execution_path",
    "read_only_corroborated_by_web_execution",
    "read_only",
}


def _load() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_fixture_has_valid_standard_envelope() -> None:
    data = _load()
    assert data["schema"] == 1
    assert isinstance(data["source"], str) and data["source"]
    assert isinstance(data["cases"], list)
    assert len(data["cases"]) > 0


def test_case_ids_are_unique() -> None:
    data = _load()
    ids = [case["id"] for case in data["cases"]]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"重複した case id がある: {duplicates}"


def test_all_cases_have_a_known_derivation_method() -> None:
    """各ケースが「実行して確認した」のか「安全な実行経路が無く読解に留めた」のかを
    明示していること(brief: 手で転記した場合は honest gap として明示する)。"""
    data = _load()
    for case in data["cases"]:
        assert case["derivation"] in VALID_DERIVATIONS, (
            f"{case['id']}: 未知の derivation 値: {case['derivation']}"
        )
        assert isinstance(case.get("citation"), str) and case["citation"], (
            f"{case['id']}: citation (旧リポジトリのファイル:行番号) が無い"
        )


def test_esa_covers_all_11_actions() -> None:
    """esa(download-article.js)は11アクション全部について、sync_status への
    影響(書く/書かない両方を含む)が記録されていること。"""
    data = _load()
    esa_cases = [c for c in data["cases"] if c["source_file"] == "download-article.js"]
    covered_actions = {c["action"] for c in esa_cases}
    # esa の 'missing' は2つのサブシナリオ(閾値未満・source_missing確定)に
    # 分かれてケース化されているため、action の集合としては11種類そろえばよい。
    assert covered_actions == ALL_11_ACTIONS, (
        f"esa が11アクション全部を網羅していない: 不足={ALL_11_ACTIONS - covered_actions}, "
        f"想定外={covered_actions - ALL_11_ACTIONS}"
    )


def test_esa_and_web_share_the_same_mapping_for_actions_both_support() -> None:
    """esa と web は別ファイルに独立実装されているが、両方が対応するアクションに
    ついては同じ sync_status を書く(=実質同じマッピング)ことを確認する。"""
    data = _load()
    by_id = {c["id"]: c for c in data["cases"]}

    shared_action_pairs = [
        ("esa_create", "web_create"),
        ("esa_update", "web_update"),
        ("esa_unchanged", "web_unchanged_via_decide_sync_action"),
        ("esa_local_modified", "web_local_modified"),
        ("esa_conflict", "web_conflict"),
        ("esa_conflict_overwritten", "web_conflict_overwritten"),
        ("esa_adopt", "web_adopt"),
        ("esa_unknown_local", "web_unknown_local"),
    ]
    for esa_id, web_id in shared_action_pairs:
        esa_status = by_id[esa_id]["expected"]["sync_status"]
        web_status = by_id[web_id]["expected"]["sync_status"]
        assert esa_status == web_status, (
            f"{esa_id} ({esa_status}) と {web_id} ({web_status}) が食い違う: "
            "esa/web は同じマッピングを共有しているはず"
        )
        assert esa_status in ALL_5_SYNC_STATUSES, f"{esa_id}: 未知の sync_status 値: {esa_status}"


def test_git_writes_synced_unconditionally_regardless_of_action() -> None:
    """git-sync は decideSyncAction を使わず、記録するすべての Markdown に
    無条件で 'synced' を書く(アクション非依存)ことを確認する。"""
    data = _load()
    by_id = {c["id"]: c for c in data["cases"]}

    for case_id in ("git_create", "git_update"):
        assert by_id[case_id]["expected"]["sync_status"] == "synced", (
            f"{case_id}: git-sync は create/update いずれも 'synced' を書くはず"
        )

    not_applicable = by_id["git_actions_not_applicable"]
    assert not_applicable["source_file"] == "download-git.js"
    assert not_applicable["action"] is None


def test_actions_that_never_touch_sync_status_are_explicitly_marked_null() -> None:
    """missing(閾値未満)/orphan/error のように sync_status を一切書き換えない
    ケースは、expected.sync_status が None であり、かつ意味(meaning)が
    説明されていること(「値を書かない」という事実そのものが golden)。"""
    data = _load()
    untouched_ids = [
        "esa_missing_below_threshold_or_individual_fetch_succeeded",
        "esa_orphan",
        "esa_error",
        "web_unchanged_via_http_304",
        "web_error_http_status",
        "web_error_network",
        "git_missing_deleted_upstream",
        "git_error",
    ]
    by_id = {c["id"]: c for c in data["cases"]}
    for case_id in untouched_ids:
        case = by_id[case_id]
        assert case["expected"]["sync_status"] is None, (
            f"{case_id}: sync_status を書かないケースのはずだが値が入っている"
        )
        assert case["expected"].get("meaning"), f"{case_id}: meaning の説明が無い"


def test_source_missing_confirmed_case_matches_sync_planner_decide_missing_candidate() -> None:
    """esa の 'missing' -> 'source_missing' 遷移は、decideMissingCandidate の
    「全条件が揃った」ケース(sync-planner.json の missing_candidate_all_conditions_met)
    と整合していること。"""
    data = _load()
    case = next(c for c in data["cases"] if c["id"] == "esa_missing_source_confirmed")
    assert case["expected"]["sync_status"] == "source_missing"

    sync_planner_path = FIXTURE_PATH.parent / "sync-planner.json"
    sync_planner = json.loads(sync_planner_path.read_text(encoding="utf-8"))
    verdict_case = next(
        c for c in sync_planner["cases"] if c["id"] == "missing_candidate_all_conditions_met"
    )
    assert verdict_case["expected"]["sourceMissing"] is True


def test_all_non_null_sync_status_values_are_within_the_5_value_enum() -> None:
    data = _load()
    for case in data["cases"]:
        status = case["expected"]["sync_status"]
        if status is not None:
            assert status in ALL_5_SYNC_STATUSES, (
                f"{case['id']}: sync_status {status!r} が SYNC_STATUSES の5値に無い"
            )
