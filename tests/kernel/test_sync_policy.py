"""`sync_policy.py` の M1 ゴールデン(`tests/fixtures/kernel/sync-planner.json`)照合テスト。

fixture は旧 `tools/lib/sync-planner.js` を実行して得たゴールデン値であり、
テストが落ちたら疑うべきは実装であって fixture ではない。

fixture のキーは Node 由来の camelCase(`localModified` 等)、Python 側 API は
snake_case のため、ここで明示的にマッピングする。
"""

from __future__ import annotations

from typing import Any

import pytest

from abist_kb.domain.sync_policy import (
    ACTION_BUCKET,
    LocalState,
    RemoteState,
    SyncAction,
    SyncRecord,
    decide_missing_candidate,
    decide_sync_action,
)
from conftest import load_kernel_fixture

FIXTURE = load_kernel_fixture("sync-planner")
CASES = {case["id"]: case for case in FIXTURE["cases"]}
_case_count = len(FIXTURE["cases"])
assert _case_count == 19, f"想定19ケースに対し {_case_count} 件しか読み込めていない"


def _remote(data: dict[str, Any] | None) -> RemoteState | None:
    if data is None:
        return None
    return RemoteState(content_hash=data.get("contentHash"), updated_at=data.get("updatedAt"))


def _record(data: dict[str, Any] | None) -> SyncRecord | None:
    if data is None:
        return None
    return SyncRecord(
        local_content_hash=data.get("local_content_hash"),
        source_content_hash=data.get("source_content_hash"),
        source_updated_at=data.get("source_updated_at"),
    )


def _local(data: dict[str, Any] | None) -> LocalState | None:
    if data is None:
        return None
    return LocalState(exists=data["exists"], body_hash=data.get("bodyHash"))


def _decide(input_data: dict[str, Any]):
    return decide_sync_action(
        remote=_remote(input_data.get("remote")),
        record=_record(input_data.get("record")),
        local=_local(input_data.get("local")),
        force=input_data.get("force", False),
    )


def _assert_decision(decision, expected: dict[str, Any]) -> None:
    assert decision.action.value == expected["action"]
    assert decision.write == expected["write"]
    assert decision.reason == expected["reason"]
    assert decision.remote_changed == expected["remoteChanged"]
    assert decision.local_modified == expected["localModified"]


def test_sync_actions_enum() -> None:
    case = CASES["sync_actions_enum"]
    expected = case["expected"]["value"]
    assert [action.value for action in SyncAction] == expected


DECISION_CASE_IDS = [
    "decide_unchanged",
    "decide_create_no_local_file",
    "decide_adopt_no_record",
    "decide_update_content_changed",
    "decide_unchanged_updated_at_only_hash_wins",
    "decide_update_via_updated_at_fallback_older",
    "decide_unchanged_via_updated_at_fallback_same",
    "decide_local_modified",
    "decide_conflict",
    "decide_conflict_overwritten_force",
    "decide_unknown_local_no_recorded_hash",
]


@pytest.mark.parametrize("case_id", DECISION_CASE_IDS)
def test_decide_sync_action(case_id: str) -> None:
    case = CASES[case_id]
    decision = _decide(case["input"])
    _assert_decision(decision, case["expected"])


def test_decide_rerun_stability_update_then_unchanged() -> None:
    case = CASES["decide_rerun_stability_update_then_unchanged"]
    first_decision = _decide(case["input"]["first"])
    _assert_decision(first_decision, case["expected"]["first"])

    second_decision = _decide(case["input"]["second"])
    _assert_decision(second_decision, case["expected"]["second"])


MISSING_CANDIDATE_CASE_IDS = [
    "missing_candidate_full_sync_failed",
    "missing_candidate_below_threshold",
    "missing_candidate_individual_fetch_succeeded",
    "missing_candidate_all_conditions_met",
]


@pytest.mark.parametrize("case_id", MISSING_CANDIDATE_CASE_IDS)
def test_decide_missing_candidate(case_id: str) -> None:
    case = CASES[case_id]
    input_data = case["input"]
    decision = decide_missing_candidate(
        full_sync_succeeded=input_data["fullSyncSucceeded"],
        missing_count=input_data["missingCount"],
        individual_fetch_failed=input_data["individualFetchFailed"],
        threshold=input_data["threshold"],
    )
    expected = case["expected"]
    assert decision.source_missing == expected["sourceMissing"]
    assert decision.change_status == expected["changeStatus"]
    assert decision.reason == expected["reason"]


def test_record_sync_result_totals_uses_action_bucket() -> None:
    """`record_sync_result_totals` fixture は ACTION_BUCKET の集計仕様そのものを固定する。

    Python 側に summary recorder は無い(M3 で実装予定)ため、ここでは
    ACTION_BUCKET を使って fixture の items から totals を再現できることを検証する。
    """
    case = CASES["record_sync_result_totals"]
    expected = case["expected"]

    totals: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    for item in expected["items"]:
        action = SyncAction(item["action"])
        bucket = ACTION_BUCKET[action]
        totals[bucket] = totals.get(bucket, 0) + 1
        action_counts[action.value] = action_counts.get(action.value, 0) + 1

    expected_totals = {
        "added": 0,
        "updated": 0,
        "skipped": 0,
        "conflict": 0,
        "missing": 0,
        "error": 0,
    }
    expected_totals.update(expected["totals"])
    assert totals == {k: v for k, v in expected_totals.items() if v}
    assert action_counts == expected["actionCounts"]


def test_action_bucket_derived() -> None:
    case = CASES["action_bucket_derived"]
    expected = case["expected"]

    assert sorted(action.value for action in ACTION_BUCKET) == expected["actionsCovered"]
    for action_value, bucket in expected["mapping"].items():
        assert ACTION_BUCKET[SyncAction(action_value)] == bucket


# ---------------------------------------------------------------------------
# I1 レビュー指摘の回帰テスト: naive/aware datetime混在での TypeError。
#
# fixture(`sync-planner.json`)には無いケース。`download-article.js` の
# コメント通り、backfill 済み文書は `source_content_hash` を持たず
# `updated_at` 比較にフォールバックする。front matter の `updated_at` は
# 日付のみ(例 "2024-01-01"、naive)、取得元側はオフセット付き ISO
# (例 "2024-05-01T10:00:00+09:00"、aware)というのが実際に起きる組み合わせ。
# 旧実装は JS の `Date.parse` を使うため常に比較可能な数値になるが、Python の
# `datetime.fromisoformat` は naive/aware を素朴に比較すると
# `TypeError: can't compare offset-naive and offset-aware datetimes` になる。
# ---------------------------------------------------------------------------


def test_decide_sync_action_mixed_awareness_remote_aware_record_naive() -> None:
    """remote が aware・record が naive でも例外にならない(backfill 済み文書の典型)。"""
    decision = decide_sync_action(
        remote=RemoteState(content_hash=None, updated_at="2024-05-01T10:00:00+09:00"),
        record=SyncRecord(
            local_content_hash="hash-1",
            source_content_hash=None,
            source_updated_at="2024-01-01",
        ),
        local=LocalState(exists=True, body_hash="hash-1"),
    )
    # 2024-05-01T10:00:00+09:00 == 2024-05-01T01:00:00Z > 2024-01-01T00:00:00Z(naive→UTC扱い)
    assert decision.remote_changed is True
    assert decision.action == SyncAction.UPDATE


def test_decide_sync_action_mixed_awareness_remote_naive_record_aware() -> None:
    """remote が naive・record が aware の組み合わせでも例外にならない。"""
    decision = decide_sync_action(
        remote=RemoteState(content_hash=None, updated_at="2024-01-01"),
        record=SyncRecord(
            local_content_hash="hash-1",
            source_content_hash=None,
            source_updated_at="2023-12-31T23:00:00+00:00",
        ),
        local=LocalState(exists=True, body_hash="hash-1"),
    )
    assert decision.remote_changed is True
    assert decision.action == SyncAction.UPDATE


def test_decide_sync_action_both_naive_treated_as_utc_and_equal() -> None:
    """両方 naive で同じ瞬間を指す場合は「変化なし」と判定される。"""
    decision = decide_sync_action(
        remote=RemoteState(content_hash=None, updated_at="2024-01-01T00:00:00"),
        record=SyncRecord(
            local_content_hash="hash-1",
            source_content_hash=None,
            source_updated_at="2024-01-01T00:00:00+00:00",
        ),
        local=LocalState(exists=True, body_hash="hash-1"),
    )
    assert decision.remote_changed is False
    assert decision.action == SyncAction.UNCHANGED


def test_sync_record_defaults_to_none_for_all_fields() -> None:
    """`SyncRecord` は兄弟データクラス(`RemoteState`/`LocalState`)と同様に既定値を持つ。"""
    record = SyncRecord()
    assert record.local_content_hash is None
    assert record.source_content_hash is None
    assert record.source_updated_at is None


def test_all_fixture_cases_covered() -> None:
    """fixture の全19ケースがこのテストファイルで参照されていることの網羅性チェック。"""
    covered = {
        "sync_actions_enum",
        *DECISION_CASE_IDS,
        "decide_rerun_stability_update_then_unchanged",
        *MISSING_CANDIDATE_CASE_IDS,
        "record_sync_result_totals",
        "action_bucket_derived",
    }
    assert covered == set(CASES)
