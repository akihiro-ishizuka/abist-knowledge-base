"""`metadata_schema.py` の M1 ゴールデン(`tests/fixtures/kernel/metadata-schema.json`)照合テスト。

fixture は旧 `tools/lib/metadata-schema.js`(および `download-article.js` の
sanitize系)を実行して得たゴールデン値であり、テストが落ちたら疑うべきは実装で
fixture ではない。

**`safe_batch_name` と `sanitize_file_name` は別物である**(モジュール docstring 参照)。
`resolve_batch_output_dirs` 系のケースは `safe_batch_name` を、`sanitize_filename_*` 系
のケースは `sanitize_file_name` を検証しており、この2つを混同する実装変更をすると
どちらか一方のケース群だけが赤くなる。
"""

from __future__ import annotations

import base64

import pytest

from abist_kb.domain.metadata_schema import (
    BACKFILL_KEYS,
    FRONTMATTER_KEY_ORDER,
    MANAGED_BY_FOR_SOURCE,
    REFERENCE_CORPUS_PREFIXES,
    DocumentType,
    ManagedBy,
    Source,
    Status,
    classify_document,
    extract_repo_name,
    is_reference_corpus,
    resolve_batch_output_dirs,
    safe_batch_name,
    sanitize_category_path,
    sanitize_file_name,
    to_posix_path,
    validate_metadata,
)
from abist_kb.domain.sync_policy import SyncStatus
from conftest import load_kernel_fixture

FIXTURE = load_kernel_fixture("metadata-schema")
CASES = {case["id"]: case for case in FIXTURE["cases"]}
_case_count = len(FIXTURE["cases"])
assert _case_count == 68, f"想定68ケースに対し {_case_count} 件しか読み込めていない"


def _b64d(s: str) -> str:
    return base64.b64decode(s).decode("utf-8")


# --- 列挙・定数 ---------------------------------------------------------------


def test_sources_enum() -> None:
    assert [s.value for s in Source] == CASES["sources_enum"]["expected"]["value"]


def test_managed_by_enum() -> None:
    assert [m.value for m in ManagedBy] == CASES["managed_by_enum"]["expected"]["value"]


def test_document_types_enum() -> None:
    assert [d.value for d in DocumentType] == CASES["document_types_enum"]["expected"]["value"]


def test_statuses_enum() -> None:
    assert [s.value for s in Status] == CASES["statuses_enum"]["expected"]["value"]


def test_sync_statuses_enum() -> None:
    assert [s.value for s in SyncStatus] == CASES["sync_statuses_enum"]["expected"]["value"]


def test_frontmatter_key_order() -> None:
    assert list(FRONTMATTER_KEY_ORDER) == CASES["frontmatter_key_order"]["expected"]["value"]


def test_managed_by_for_source() -> None:
    expected = CASES["managed_by_for_source"]["expected"]["value"]
    actual = {source.value: mb.value for source, mb in MANAGED_BY_FOR_SOURCE.items()}
    assert actual == expected


def test_backfill_keys() -> None:
    assert list(BACKFILL_KEYS) == CASES["backfill_keys"]["expected"]["value"]


def test_reference_corpus_prefixes() -> None:
    expected = CASES["reference_corpus_prefixes"]["expected"]["value"]
    assert list(REFERENCE_CORPUS_PREFIXES) == expected


# --- classify_document --------------------------------------------------------

CLASSIFY_CASE_IDS = [
    "classify_post_number_esa",
    "classify_source_web",
    "classify_git_dir",
    "classify_manual_default",
    "classify_manual_handwritten_0",
    "classify_manual_handwritten_1",
    "classify_manual_handwritten_2",
    "classify_post_number_non_numeric_0",
    "classify_post_number_non_numeric_1",
    "classify_post_number_non_numeric_2",
    "classify_post_number_non_numeric_3",
    "classify_post_number_non_numeric_4",
    "classify_post_number_non_numeric_5",
    "classify_source_website_not_web",
    "classify_source_web_case_insensitive",
    "classify_existing_source_conflict",
    "classify_meeting_path_1",
    "classify_meeting_path_2",
    "classify_memo_path_1",
    "classify_memo_path_2",
    "classify_specification_path_1",
    "classify_specification_path_2",
    "classify_reference_path_1",
    "classify_reference_path_2",
    "classify_knowledge_default",
    "classify_status_active_default",
    "classify_status_archived_path",
    "classify_status_preserved",
]


@pytest.mark.parametrize("case_id", CLASSIFY_CASE_IDS)
def test_classify_document(case_id: str) -> None:
    case = CASES[case_id]
    input_data = case["input"]
    result = classify_document(
        relative_path=input_data["relativePath"],
        frontmatter=input_data["frontmatter"],
        git_output_dirs=input_data.get("gitOutputDirs", []),
    )
    expected = case["expected"]
    assert result.source == expected["source"]
    assert result.managed_by == expected["managed_by"]
    assert result.document_type == expected["document_type"]
    assert result.status == expected["status"]
    assert result.conflict == expected["conflict"]
    assert list(result.reasons) == expected["reasons"]


# --- is_reference_corpus -------------------------------------------------------

IS_REFERENCE_CORPUS_CASE_IDS = [
    "is_reference_corpus_0",
    "is_reference_corpus_1",
    "is_reference_corpus_2",
    "is_reference_corpus_3",
    "is_reference_corpus_4",
]


@pytest.mark.parametrize("case_id", IS_REFERENCE_CORPUS_CASE_IDS)
def test_is_reference_corpus(case_id: str) -> None:
    case = CASES[case_id]
    assert is_reference_corpus(case["input"]["relativePath"]) == case["expected"]["value"]


# --- to_posix_path --------------------------------------------------------------

TO_POSIX_PATH_CASE_IDS = ["to_posix_path_0", "to_posix_path_1"]


@pytest.mark.parametrize("case_id", TO_POSIX_PATH_CASE_IDS)
def test_to_posix_path(case_id: str) -> None:
    case = CASES[case_id]
    assert to_posix_path(case["input"]["raw"]) == case["expected"]["value"]


# --- validate_metadata ------------------------------------------------------------


def test_validate_metadata_ok() -> None:
    case = CASES["validate_metadata_ok"]
    result = validate_metadata(case["input"]["meta"])
    assert result.ok == case["expected"]["ok"]
    assert list(result.errors) == case["expected"]["errors"]


def test_validate_metadata_bad_enums() -> None:
    case = CASES["validate_metadata_bad_enums"]
    result = validate_metadata(case["input"]["meta"])
    assert result.ok == case["expected"]["ok"]
    assert list(result.errors) == case["expected"]["errors"]


def test_validate_metadata_managed_by_mismatch() -> None:
    case = CASES["validate_metadata_managed_by_mismatch"]
    result = validate_metadata(case["input"]["meta"])
    assert result.ok == case["expected"]["ok"]
    assert list(result.errors) == case["expected"]["errors"]


# --- sanitize_file_name -----------------------------------------------------------

SANITIZE_FILENAME_CASE_IDS = [
    "sanitize_filename_special_chars",
    "sanitize_filename_whitespace",
    "sanitize_filename_reserved_con",
    "sanitize_filename_empty",
    "sanitize_filename_reserved_or_edge_con",
    "sanitize_filename_reserved_or_edge_con_txt",
    "sanitize_filename_reserved_or_edge_PRN",
    "sanitize_filename_reserved_or_edge_AUX",
    "sanitize_filename_reserved_or_edge_NUL",
    "sanitize_filename_reserved_or_edge_COM1",
    "sanitize_filename_reserved_or_edge_COM9",
    "sanitize_filename_reserved_or_edge_LPT1",
    "sanitize_filename_reserved_or_edge_LPT9",
    "sanitize_filename_reserved_or_edge_CONFERENCE",
    "sanitize_filename_reserved_or_edge____",
    "sanitize_filename_255byte_truncation_japanese",
    "sanitize_filename_255byte_truncation_ascii",
]


@pytest.mark.parametrize("case_id", SANITIZE_FILENAME_CASE_IDS)
def test_sanitize_file_name(case_id: str) -> None:
    case = CASES[case_id]
    name = _b64d(case["input"]["name_b64"]) if case["input"]["name_b64"] else ""
    assert sanitize_file_name(name) == case["expected"]["value"]


def test_sanitize_file_name_truncated_result_fits_255_bytes() -> None:
    """255バイト切詰の境界そのものを性質として確認する(fixtureの2ケースを流用)。"""
    for case_id in (
        "sanitize_filename_255byte_truncation_japanese",
        "sanitize_filename_255byte_truncation_ascii",
    ):
        result = CASES[case_id]["expected"]["value"]
        assert len(result.encode("utf-8")) <= 255


# --- sanitize_category_path --------------------------------------------------------

SANITIZE_CATEGORY_PATH_CASE_IDS = [
    "sanitize_category_path_japanese",
    "sanitize_category_path_empty",
    "sanitize_category_path_reserved_segment",
]


@pytest.mark.parametrize("case_id", SANITIZE_CATEGORY_PATH_CASE_IDS)
def test_sanitize_category_path(case_id: str) -> None:
    case = CASES[case_id]
    category_path_b64 = case["input"]["categoryPath_b64"]
    category_path = _b64d(category_path_b64) if category_path_b64 else ""
    assert sanitize_category_path(category_path) == case["expected"]["value"]


# --- resolve_batch_output_dirs(safe_batch_name を間接検証) --------------------------


def test_resolve_batch_output_dirs() -> None:
    """`safeBatchName`/`withDocsPrefix`/`extractRepoName` を間接検証する。

    fixture の `batchConfig` は JSON化の過程でキー順がソートされてしまっているため、
    呼び出し順を保証する `batchConfigKeys` の順で辞書を組み直してから渡す
    (`resolveBatchOutputDirs` は `Object.entries` の挿入順で処理するため、順序が
    結果の `resolved` 配列の並びに影響する)。
    """
    case = CASES["resolve_batch_output_dirs"]
    input_data = case["input"]
    raw_config = input_data["batchConfig"]
    ordered_config = {key: raw_config[key] for key in input_data["batchConfigKeys"]}

    resolved = resolve_batch_output_dirs(ordered_config)

    expected = case["expected"]
    assert [
        {"name": entry.name, "type": entry.type, "dir": entry.dir} for entry in resolved
    ] == expected["resolved"]

    from abist_kb.domain.metadata_schema import resolve_git_output_dirs

    assert resolve_git_output_dirs(ordered_config) == expected["gitOutputDirs"]


def test_extract_repo_name_ssh_colon_branch_without_slash() -> None:
    """レビュー指摘(mutation blind-spot): `extract_repo_name` の
    `if ':' in last: return last.split(':')[-1]` 分岐は、fixture の
    `git-ssh-form`(`git@github.com:foo/ssh-repo.git`)では実は一度も実行
    されない——コロンは `parts` の最後のセグメントより前(`foo` の前)に
    あるため、`last` (`'ssh-repo'`) 自体にはコロンが含まれない。
    この分岐が本当に実行されるのは、リポジトリ部分にスラッシュが無い
    SSH形式(`git@host:repo.git` のように `/` を挟まない場合)だけであり、
    そのケースはどの fixture にも無かった(mutation testing で分岐が
    未検出のまま生き残った理由)。
    """
    assert extract_repo_name("git@github.com:bar-repo.git") == "bar-repo"
    assert extract_repo_name("git@github.com:foo/ssh-repo.git") == "ssh-repo"


def test_safe_batch_name_differs_from_sanitize_file_name_for_long_names() -> None:
    """`safe_batch_name` と `sanitize_file_name` が別物であることを直接示す回帰テスト。

    255バイトを超える名前に対して、`safe_batch_name` は切り詰めず、
    `sanitize_file_name` は切り詰める(末尾に `...` を付ける)。
    """
    long_name = "あ" * 500
    assert safe_batch_name(long_name) == long_name
    truncated = sanitize_file_name(long_name)
    assert truncated != long_name
    assert truncated.endswith("...")
    assert len(truncated.encode("utf-8")) <= 255


def test_all_fixture_cases_covered() -> None:
    """fixture の全68ケースがこのテストファイルで参照されていることの網羅性チェック。"""
    covered = {
        "sources_enum",
        "managed_by_enum",
        "document_types_enum",
        "statuses_enum",
        "sync_statuses_enum",
        "frontmatter_key_order",
        "managed_by_for_source",
        "backfill_keys",
        "reference_corpus_prefixes",
        *CLASSIFY_CASE_IDS,
        *IS_REFERENCE_CORPUS_CASE_IDS,
        *TO_POSIX_PATH_CASE_IDS,
        "validate_metadata_ok",
        "validate_metadata_bad_enums",
        "validate_metadata_managed_by_mismatch",
        *SANITIZE_FILENAME_CASE_IDS,
        *SANITIZE_CATEGORY_PATH_CASE_IDS,
        "resolve_batch_output_dirs",
    }
    assert covered == set(CASES)
