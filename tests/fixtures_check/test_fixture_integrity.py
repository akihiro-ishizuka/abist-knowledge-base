"""M1 で採取したカーネルゴールデン fixture 自体の健全性を検証する。

ここでのテスト対象は Python 実装ではなく、旧 Node システムを実行して採取した
tests/fixtures/kernel/*.json そのもの。改行変換や JSON 往復で BOM・CRLF が
壊れていないこと、および brief が要求する網羅条件（512字切詰境界・
sync-planner の11アクション網羅）を満たすことを確認する。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURES_KERNEL_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "kernel"


def _kernel_json_paths() -> list[Path]:
    paths = sorted(FIXTURES_KERNEL_DIR.glob("*.json"))
    assert paths, f"kernel フィクスチャが1つも無い: {FIXTURES_KERNEL_DIR}"
    return paths


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_KERNEL_DIR / name).read_text(encoding="utf-8"))


def _iter_b64_fields(obj: Any) -> Iterator[tuple[str, str]]:
    """オブジェクトを再帰的に辿り、キーが "_b64" で終わる文字列値をすべて返す。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.endswith("_b64") and isinstance(value, str):
                yield key, value
            else:
                yield from _iter_b64_fields(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_b64_fields(item)


@pytest.mark.parametrize("path", _kernel_json_paths(), ids=lambda p: p.name)
def test_kernel_json_has_valid_schema(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert isinstance(data["source"], str) and data["source"]
    assert isinstance(data["cases"], list)
    assert len(data["cases"]) > 0


@pytest.mark.parametrize("path", _kernel_json_paths(), ids=lambda p: p.name)
def test_case_ids_are_unique_within_file(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [case["id"] for case in data["cases"]]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"{path.name} に重複した case id がある: {duplicates}"


@pytest.mark.parametrize("path", _kernel_json_paths(), ids=lambda p: p.name)
def test_all_b64_fields_decode_as_valid_base64(path: Path) -> None:
    """*_b64 フィールドは（存在すれば）すべて正しく base64 デコードできること。

    sync-planner.json のようにハッシュ・アクション名など byte-sensitive でない
    値しか持たないファイルには *_b64 フィールドが無くてよい
    （網羅性は test_bom_and_crlf_cases_exist_across_all_kernel_fixtures 側で見る）。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in _iter_b64_fields(data):
        try:
            base64.b64decode(value, validate=True)
        except binascii.Error as exc:  # pragma: no cover - 失敗時に原因を明示する
            pytest.fail(f"{path.name}: フィールド {key!r} が base64 として不正: {exc}")


def test_bom_case_survives_round_trip() -> None:
    """BOM 付き入力が改行変換や JSON 往復で壊れず \\ufeff のまま残っていること。"""
    data = _load("frontmatter.json")
    case = next(c for c in data["cases"] if c["id"] == "parse_BOM_DOC")
    raw_input = base64.b64decode(case["input_b64"]).decode("utf-8")
    assert raw_input.startswith("\ufeff")
    raw_expected = base64.b64decode(case["expected"]["raw_b64"]).decode("utf-8")
    assert raw_expected.startswith("\ufeff")
    assert case["expected"]["bomPresent"] is True


def test_crlf_case_survives_round_trip() -> None:
    """CRLF 入力が改行変換で LF に潰されず \\r\\n のまま残っていること。"""
    data = _load("frontmatter.json")
    case = next(c for c in data["cases"] if c["id"] == "parse_WEB_CRLF")
    raw_input = base64.b64decode(case["input_b64"]).decode("utf-8")
    assert "\r\n" in raw_input
    assert case["expected"]["eol"] == "\r\n"


def test_bom_and_crlf_cases_exist_across_all_kernel_fixtures() -> None:
    """個々のファイルだけでなく、採取物全体で BOM・CRLF が最低1件ずつ保存されていること。"""
    found_bom = False
    found_crlf = False
    for path in _kernel_json_paths():
        data = json.loads(path.read_text(encoding="utf-8"))
        for _, value in _iter_b64_fields(data):
            try:
                decoded = base64.b64decode(value).decode("utf-8")
            except UnicodeDecodeError:
                continue
            found_bom = found_bom or decoded.startswith("\ufeff")
            found_crlf = found_crlf or "\r\n" in decoded
    assert found_bom, "BOM で始まる採取ケースが1件も無い"
    assert found_crlf, "CRLF を含む採取ケースが1件も無い"


def test_e5_embedding_input_has_512_char_boundary_straddling_case() -> None:
    """512字切詰の「後」に passage: 接頭辞が付く順序を検証できるケースが存在すること。

    「先に接頭辞を足してから切り詰める」誤実装だと最終文字列長がちょうど512になる。
    正しい実装は 512 + len("passage: ") になるはずで、この非対称性がバグを可視化する。
    """
    data = _load("embeddings.json")
    case = next(c for c in data["cases"] if c["id"] == "boundary_straddle_512")
    prefix = "passage: "
    decoded = base64.b64decode(case["expected"]["embeddingInput_b64"]).decode("utf-8")
    assert decoded.startswith(prefix)
    assert len(decoded) == 512 + len(prefix)
    assert case["expected"]["inputLength"] == len(decoded)
    wrong_order_length = 512
    message = "誤実装(先に接頭辞→切詰)と区別できない長さになっている"
    assert case["expected"]["inputLength"] != wrong_order_length, message


def test_sync_planner_covers_all_11_actions() -> None:
    data = _load("sync-planner.json")
    actions_case = next(c for c in data["cases"] if c["id"] == "sync_actions_enum")
    all_actions = set(actions_case["expected"]["value"])
    assert len(all_actions) == 11, f"SYNC_ACTIONS が11個ではない: {sorted(all_actions)}"

    bucket_case = next(c for c in data["cases"] if c["id"] == "action_bucket_derived")
    covered = set(bucket_case["expected"]["mapping"].keys())
    missing = all_actions - covered
    assert covered == all_actions, f"ACTION_BUCKET が全アクションを網羅していない: 不足={missing}"
