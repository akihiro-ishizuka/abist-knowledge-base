"""`infrastructure.sources.esa`: 旧 `download-article.js` / `test/sync-esa.test.js` の移植。

`tests/fixtures/PROVENANCE.md` が明示する通り、`test/sync-esa.test.js` の
シナリオは fixture 化されておらず、このファイルでの pytest 再実装が
その受け入れ基準そのものになる。各テストの docstring に旧テスト名を残す。

`test/orphan-detection.test.js` が検証する `findOrphanCandidates`/
`resolveOrphansWithSource`(`tools/verify-integrity.js`)自体は M7 の範囲
(`tests/fixtures/PROVENANCE.md` 該当表)であり、ここでは扱わない。ここで
再現するのは、esa 同期ループ自身が持つカテゴリ移動検出(`detectMissingPosts`
内の `orphan` アクション、`--prune-orphans`)であり、これは M3 の esa アダプター
本体の契約である。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from abist_kb.domain.frontmatter import hash_body
from abist_kb.domain.metadata_schema import sanitize_category_path, sanitize_file_name
from abist_kb.domain.sync_policy import SyncAction, SyncStatus
from abist_kb.infrastructure.db.documents_repo import DocumentRepository
from abist_kb.infrastructure.sources.esa import (
    EsaClient,
    EsaSyncRunner,
    generate_front_matter,
    resolve_post_path,
)

from .conftest import FAKE_TOKEN, MockEsaServer

_LATER_UPDATED_AT = "2026-07-25T10:00:00+09:00"

_counter = 0


def make_post(**overrides: object) -> dict:
    global _counter
    _counter += 1
    post = {
        "name": f"テスト記事{_counter}",
        "created_at": "2026-07-01T10:00:00+09:00",
        "updated_at": "2026-07-01T10:00:00+09:00",
        "created_by": {"screen_name": "tester"},
        "updated_by": {"screen_name": "tester"},
        "category": "同期テスト",
        "tags": [],
        "number": 90000 + _counter,
        "url": f"https://abist.esa.io/posts/{90000 + _counter}",
        "body_md": f"# 見出し{_counter}\r\n\r\n本文\r\n",
    }
    post.update(overrides)
    return post


@pytest.fixture
def sync_dirs(tmp_root: Path) -> tuple[Path, Path, str]:
    docs_dir = tmp_root / "docs"
    docs_dir.mkdir()
    return tmp_root, docs_dir, "docs/_test_sync_esa"


def make_runner(
    documents: DocumentRepository,
    sync_dirs: tuple[Path, Path, str],
    *,
    force: bool = False,
    dry_run: bool = False,
    missing_threshold: int = 3,
) -> EsaSyncRunner:
    root_dir, docs_dir, output_dir = sync_dirs
    return EsaSyncRunner(
        documents=documents,
        root_dir=root_dir,
        docs_dir=docs_dir,
        output_dir=output_dir,
        force=force,
        dry_run=dry_run,
        missing_threshold=missing_threshold,
    )


def file_path_of(post: dict, sync_dirs: tuple[Path, Path, str]) -> Path:
    root_dir, docs_dir, output_dir = sync_dirs
    _, file_path = resolve_post_path(
        post, root_dir=root_dir, output_dir=output_dir, docs_dir=docs_dir
    )
    return file_path


def read_raw(path: Path) -> str:
    """`Path.read_text()` の既定(universal newlines)を避け、生バイトのまま読む。

    `EsaSyncRunner` は改行を1バイトも変えずに読み書きする(`_read_text_preserving_eol`
    参照)。テスト側が `Path.read_text()`/`write_text()` の既定(Windows では
    書込時に単独の `\\n` を `\\r\\n` へ変換する)を混ぜて使うと、CRLF本文を
    含む固定具の期待値がプラットフォーム依存で壊れるため、比較用の読み書きは
    すべてこのヘルパーに統一する。
    """
    return path.read_bytes().decode("utf-8")


def write_raw(path: Path, content: str) -> None:
    path.write_bytes(content.encode("utf-8"))


def rel_path_of(post: dict, sync_dirs: tuple[Path, Path, str]) -> str:
    root_dir, docs_dir, output_dir = sync_dirs
    file_path = file_path_of(post, sync_dirs)
    return file_path.relative_to(docs_dir).as_posix()


# ---------------------------------------------------------------------------
# front matter 生成: キー順序・引用規則
# ---------------------------------------------------------------------------


def test_front_matter_key_order_and_quoting_rules() -> None:
    post = make_post(tags=["a", "b"])
    metadata = {
        "source": "esa",
        "managed_by": "esa-sync",
        "document_type": "knowledge",
        "status": "active",
    }
    content = generate_front_matter(post, metadata)
    lines = content.splitlines()
    assert lines[0] == "---"
    assert lines[-1] == ""
    assert lines[-2] == "---"
    keys_in_order = [line.split(":", 1)[0] for line in lines[1:-2]]
    assert keys_in_order == [
        "title",
        "date",
        "updated_at",
        "author",
        "updated_by",
        "category",
        "tags",
        "post_number",
        "url",
        "source",
        "managed_by",
        "document_type",
        "status",
    ]
    body = "\n".join(lines)
    # 通常のメタキーはダブルクォート、末尾4キーは非引用。
    assert f'title: "{post["name"]}"' in body
    assert 'tags: ["a", "b"]' in body
    assert "source: esa" in body
    assert "managed_by: esa-sync" in body
    assert "document_type: knowledge" in body
    assert "status: active" in body


def test_front_matter_omits_empty_values() -> None:
    post = make_post(category="", tags=[], url="")
    content = generate_front_matter(post, {})
    assert "category:" not in content
    assert "tags:" not in content
    assert "url:" not in content


# ---------------------------------------------------------------------------
# 受入条件: 未変更文書を上書きしない(旧テスト「初回は create」「unchanged」)
# ---------------------------------------------------------------------------


def test_first_sync_creates(documents: DocumentRepository, sync_dirs) -> None:
    """旧テスト『初回は create で保存する』。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    item = runner.save_post(post)
    assert item.action == str(SyncAction.CREATE)
    assert file_path_of(post, sync_dirs).is_file()
    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["sync_status"] == str(SyncStatus.SYNCED)


def test_unchanged_resync_does_not_touch_mtime(documents: DocumentRepository, sync_dirs) -> None:
    """旧テスト『同じ内容を再同期しても unchanged でファイルに触れない(受入条件1)』。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)
    path = file_path_of(post, sync_dirs)
    before_mtime = path.stat().st_mtime_ns
    before_content = read_raw(path)

    time.sleep(0.02)
    item = runner.save_post(post)

    assert item.action == str(SyncAction.UNCHANGED)
    assert read_raw(path) == before_content
    assert path.stat().st_mtime_ns == before_mtime, "未変更なのにファイルを書き換えた"


def test_same_day_update_with_identical_body_stays_unchanged(
    documents: DocumentRepository, sync_dirs
) -> None:
    """旧テスト『updated_at だけ新しく本文が同じなら上書きしない』(実装詳細1の第二ガード)。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)
    path = file_path_of(post, sync_dirs)
    before_mtime = path.stat().st_mtime_ns

    time.sleep(0.02)
    touched = {**post, "updated_at": "2026-07-25T10:00:00+09:00"}
    item = runner.save_post(touched)

    assert item.action == str(SyncAction.UNCHANGED)
    assert path.stat().st_mtime_ns == before_mtime


def test_remote_body_change_triggers_update(documents: DocumentRepository, sync_dirs) -> None:
    """旧テスト『取得元の本文が変わったら update する』。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)

    updated = {
        **post,
        "body_md": "# 見出し\r\n\r\n更新後の本文\r\n",
        "updated_at": "2026-07-25T10:00:00+09:00",
    }
    item = runner.save_post(updated)

    assert item.action == str(SyncAction.UPDATE)
    content = read_raw(file_path_of(post, sync_dirs))
    assert "更新後の本文" in content
    row = documents.get(rel_path_of(post, sync_dirs))
    from abist_kb.domain.frontmatter import sha256_hex

    assert row["source_content_hash"] == sha256_hex(updated["body_md"])


# ---------------------------------------------------------------------------
# 受入条件: ローカル編集を自動上書きしない
# ---------------------------------------------------------------------------


def test_local_edit_with_unchanged_remote_is_not_overwritten(
    documents: DocumentRepository, sync_dirs
) -> None:
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)

    path = file_path_of(post, sync_dirs)
    write_raw(path, read_raw(path) + "ローカル追記\r\n")
    edited = read_raw(path)

    item = runner.save_post(post)

    assert item.action == str(SyncAction.LOCAL_MODIFIED)
    assert read_raw(path) == edited, "ローカル編集を消した"
    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["sync_status"] == str(SyncStatus.MODIFIED_LOCAL)


def test_conflict_when_local_edited_and_remote_updated(
    documents: DocumentRepository, sync_dirs
) -> None:
    """旧テスト『ローカル編集 かつ 取得元更新 は conflict にして上書きしない(受入条件2)』。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)

    path = file_path_of(post, sync_dirs)
    write_raw(path, read_raw(path) + "ローカル追記\r\n")
    edited = read_raw(path)

    updated = {
        **post,
        "body_md": "# 見出し\r\n\r\n取得元も更新\r\n",
        "updated_at": _LATER_UPDATED_AT,
    }
    item = runner.save_post(updated)

    assert item.action == str(SyncAction.CONFLICT)
    assert read_raw(path) == edited, "conflict なのに上書きした"
    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["sync_status"] == str(SyncStatus.CONFLICT)


def test_conflict_is_stable_across_repeated_runs(documents: DocumentRepository, sync_dirs) -> None:
    """旧テスト『conflict は再実行しても conflict のまま(取得元状態を勝手に進めない)』。

    受入条件: conflict を3回連続で実行しても結果が揺れない・いつの間にか上書きされない。
    """
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)

    path = file_path_of(post, sync_dirs)
    write_raw(path, read_raw(path) + "ローカル追記\r\n")

    updated = {
        **post,
        "body_md": "# 見出し\r\n\r\n取得元も更新\r\n",
        "updated_at": _LATER_UPDATED_AT,
    }

    actions = [runner.save_post(updated).action for _ in range(3)]
    assert actions == [str(SyncAction.CONFLICT)] * 3
    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["sync_status"] == str(SyncStatus.CONFLICT)


def test_force_converts_conflict_to_conflict_overwritten(
    documents: DocumentRepository, sync_dirs
) -> None:
    """旧テスト『--force 相当を指定したときだけ conflict を上書きする』。"""
    base_runner = make_runner(documents, sync_dirs)
    post = make_post()
    base_runner.save_post(post)

    path = file_path_of(post, sync_dirs)
    write_raw(path, read_raw(path) + "ローカル追記\r\n")

    updated = {
        **post,
        "body_md": "# 見出し\r\n\r\n取得元優先\r\n",
        "updated_at": _LATER_UPDATED_AT,
    }

    # force=False では conflict のまま(--force を指定したときだけ上書きされることの対照)。
    assert base_runner.save_post(updated).action == str(SyncAction.CONFLICT)

    force_runner = make_runner(documents, sync_dirs, force=True)
    item = force_runner.save_post(updated)

    assert item.action == str(SyncAction.CONFLICT_OVERWRITTEN)
    assert "取得元優先" in read_raw(path)
    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["sync_status"] == str(SyncStatus.SYNCED)


def test_adopt_existing_file_without_record(documents: DocumentRepository, sync_dirs) -> None:
    """旧テスト『記録が無い既存ファイルは adopt で上書きしない』。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    dir_path, path = resolve_post_path(
        post, root_dir=sync_dirs[0], output_dir=sync_dirs[2], docs_dir=sync_dirs[1]
    )
    dir_path.mkdir(parents=True, exist_ok=True)
    write_raw(path, '---\ntitle: "手で置いた"\n---\n\n中身\n')
    before = read_raw(path)

    item = runner.save_post(post)

    assert item.action == str(SyncAction.ADOPT)
    assert read_raw(path) == before, "記録が無いだけで上書きした"
    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["local_content_hash"] == hash_body(before)
    assert row["source_content_hash"] is None, "一致の保証が無い取得元ハッシュを入れてはいけない"


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(documents: DocumentRepository, sync_dirs) -> None:
    """旧テスト『dry-run はファイルも DB も書き換えない』。"""
    runner = make_runner(documents, sync_dirs, dry_run=True)
    post = make_post()
    item = runner.save_post(post)

    assert item.action == str(SyncAction.CREATE)
    assert not file_path_of(post, sync_dirs).exists()
    assert documents.get(rel_path_of(post, sync_dirs)) is None


# ---------------------------------------------------------------------------
# 受入条件: 同じ同期を再実行しても結果が安定する
# ---------------------------------------------------------------------------


def test_three_consecutive_syncs_are_stable(documents: DocumentRepository, sync_dirs) -> None:
    """旧テスト『3回連続で同期しても create -> unchanged -> unchanged で安定する』。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    actions = [runner.save_post(post).action for _ in range(3)]
    assert actions == [str(SyncAction.CREATE), str(SyncAction.UNCHANGED), str(SyncAction.UNCHANGED)]


def test_resync_after_update_becomes_unchanged(documents: DocumentRepository, sync_dirs) -> None:
    """旧テスト『update 後の再同期は unchanged になる』。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)

    updated = {**post, "body_md": "# 変更\r\n", "updated_at": "2026-07-25T10:00:00+09:00"}
    assert runner.save_post(updated).action == str(SyncAction.UPDATE)
    assert runner.save_post(updated).action == str(SyncAction.UNCHANGED)
    assert runner.save_post(updated).action == str(SyncAction.UNCHANGED)


# ---------------------------------------------------------------------------
# 人間が決めた値の保全
# ---------------------------------------------------------------------------


def test_update_preserves_human_set_status(documents: DocumentRepository, sync_dirs) -> None:
    """旧テスト『update しても人間が変えた status を維持する』。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)

    path = file_path_of(post, sync_dirs)
    write_raw(path, read_raw(path).replace("status: active", "status: deprecated"))

    updated = {**post, "body_md": "# 更新\r\n", "updated_at": "2026-07-25T10:00:00+09:00"}
    item = runner.save_post(updated)

    assert item.action == str(SyncAction.UPDATE)
    content = read_raw(path)
    assert "status: deprecated" in content, "同期が業務状態を戻した"
    assert "# 更新" in content, "本文が更新されていない"


def test_update_preserves_human_set_document_type(documents: DocumentRepository, sync_dirs) -> None:
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)

    path = file_path_of(post, sync_dirs)
    original = read_raw(path)
    assert "document_type:" in original
    content_with_type = original.replace("document_type: knowledge", "document_type: specification")
    write_raw(path, content_with_type)

    updated = {**post, "body_md": "# 更新\r\n", "updated_at": "2026-07-25T10:00:00+09:00"}
    runner.save_post(updated)

    assert "document_type: specification" in read_raw(path)


# ---------------------------------------------------------------------------
# DB書き込み失敗はダウンロードを失敗させない(旧 doc-record.js の契約、実装詳細2)
# ---------------------------------------------------------------------------


def test_db_failure_does_not_fail_the_download(
    documents: DocumentRepository, sync_dirs, monkeypatch
) -> None:
    def _boom(self, record):  # noqa: ANN001
        raise RuntimeError("DB は一時的に利用できません")

    monkeypatch.setattr(DocumentRepository, "upsert", _boom)
    runner = make_runner(documents, sync_dirs)
    post = make_post()

    item = runner.save_post(post)

    assert item.action == str(SyncAction.CREATE)
    assert file_path_of(post, sync_dirs).is_file(), "DB失敗でダウンロード自体を失わせてはいけない"


def test_db_read_failure_falls_back_to_adopt(
    documents: DocumentRepository, sync_dirs, monkeypatch
) -> None:
    """`getRecord` 相当が失敗した場合は「記録なし」側へ倒れ、上書きしない(adopt)。"""
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)  # create

    def _boom(self, path):  # noqa: ANN001
        raise RuntimeError("DB 読み取り失敗")

    monkeypatch.setattr(DocumentRepository, "get", _boom)
    item = runner.save_post(post)

    assert item.action == str(SyncAction.ADOPT)


# ---------------------------------------------------------------------------
# パス安全性: docs/ の外への書き込みを拒否する
# ---------------------------------------------------------------------------


def test_path_traversal_via_category_is_rejected(sync_dirs) -> None:
    post = make_post(category="../../outside")
    root_dir, docs_dir, output_dir = sync_dirs
    with pytest.raises(Exception):  # noqa: B017 - AppError(INVALID_INPUT)
        resolve_post_path(post, root_dir=root_dir, output_dir=output_dir, docs_dir=docs_dir)


def test_windows_reserved_name_is_sanitized(sync_dirs) -> None:
    post = make_post(name="CON", category=None)
    root_dir, docs_dir, output_dir = sync_dirs
    _, path = resolve_post_path(post, root_dir=root_dir, output_dir=output_dir, docs_dir=docs_dir)
    assert path.name == "file-CON.md"


# ---------------------------------------------------------------------------
# カテゴリ改名時の orphan 解決、--prune-orphans
# ---------------------------------------------------------------------------


def test_category_rename_is_detected_as_orphan_and_pruned(
    documents: DocumentRepository, sync_dirs
) -> None:
    runner = make_runner(documents, sync_dirs)
    post = make_post(category="旧カテゴリ")
    runner.save_post(post)
    old_path = file_path_of(post, sync_dirs)
    assert old_path.is_file()

    moved = {**post, "category": "新カテゴリ"}
    runner.save_post(moved)
    new_path = file_path_of(moved, sync_dirs)
    assert new_path.is_file()

    orphan_items = asyncio.run(
        runner.detect_missing_posts(
            all_posts=[moved], category_path=None, full_sync_succeeded=True, prune_orphans=False
        )
    )
    assert len(orphan_items) == 1
    assert orphan_items[0].action == str(SyncAction.ORPHAN)
    assert orphan_items[0].expected_path == rel_path_of(moved, sync_dirs)
    assert old_path.is_file(), "prune_orphans=False では削除しない"

    pruned_items = asyncio.run(
        runner.detect_missing_posts(
            all_posts=[moved], category_path=None, full_sync_succeeded=True, prune_orphans=True
        )
    )
    assert pruned_items[0].action == str(SyncAction.ORPHAN)
    assert not old_path.exists(), "--prune-orphans で取り残しが削除されなかった"
    assert documents.get(rel_path_of(post, sync_dirs)) is None


def test_orphan_deletion_loop_stops_after_lease_is_lost(
    documents: DocumentRepository, sync_dirs
) -> None:
    """取り残し(orphan)削除ループはリース確認を反復ごとに行う(carried-over fix)。

    fix2 で `save_post` の書き込みループには `check_lease()` が入ったが、同じ
    形の1件ずつの副作用ループである orphan 削除ループは対応漏れだった。削除は
    書き込みより取り返しがつかないため、リースを奪われた後も削除を続けるのは
    書き込みループの不具合より深刻。ここでは3件の取り残しのうち2件目の反復で
    リースが奪われたと模して、3件目が削除されずに残ることを確認する。
    """
    runner = make_runner(documents, sync_dirs)
    posts = [make_post(category="旧カテゴリ") for _ in range(3)]
    for post in posts:
        runner.save_post(post)
    old_paths = [file_path_of(post, sync_dirs) for post in posts]
    assert all(p.is_file() for p in old_paths)

    moved = [{**post, "category": "新カテゴリ"} for post in posts]
    for post in moved:
        runner.save_post(post)

    from abist_kb.domain.errors import AppError, ErrorCode

    calls = 0

    def lease_lost_on_second_item() -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AppError(code=ErrorCode.CONFLICT, message="リースが奪われました")

    with pytest.raises(AppError):
        asyncio.run(
            runner.detect_missing_posts(
                all_posts=moved,
                category_path=None,
                full_sync_succeeded=True,
                prune_orphans=True,
                check_lease=lease_lost_on_second_item,
            )
        )

    assert calls == 2, "2件目の反復でリース確認が行われなかった"
    assert sum(p.is_file() for p in old_paths) >= 1, "リース喪失後も削除を続けた"


def test_missing_below_threshold_does_not_mark_source_missing(
    documents: DocumentRepository, sync_dirs
) -> None:
    """設計原則5: 連続不在が閾値未満のうちは source_missing にしない。"""
    runner = make_runner(documents, sync_dirs, missing_threshold=3)
    post = make_post()
    runner.save_post(post)

    # 2回連続で一覧に出てこない(閾値3未満)。
    for _ in range(2):
        items = asyncio.run(
            runner.detect_missing_posts(
                all_posts=[], category_path=None, full_sync_succeeded=True, prune_orphans=False
            )
        )
        assert items[0].action == str(SyncAction.MISSING)

    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["sync_status"] == str(SyncStatus.SYNCED), "閾値未満なのに source_missing にした"


def test_missing_at_threshold_with_failed_individual_fetch_marks_source_missing(
    documents: DocumentRepository, sync_dirs
) -> None:
    """全件同期成功 + 連続不在が閾値到達 + 個別取得も失敗、の3条件が揃って初めて確定する。"""
    runner = make_runner(documents, sync_dirs, missing_threshold=2)
    post = make_post()
    runner.save_post(post)

    async def failing_fetch(number: int) -> None:
        raise RuntimeError("404")

    items: list = []
    for _ in range(2):
        items = asyncio.run(
            runner.detect_missing_posts(
                all_posts=[],
                category_path=None,
                full_sync_succeeded=True,
                prune_orphans=False,
                fetch_post=failing_fetch,
            )
        )

    assert items[0].action == str(SyncAction.MISSING)
    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["sync_status"] == str(SyncStatus.SOURCE_MISSING)
    assert row["status"] == "active", "業務状態(status)は同期処理が変えてはいけない"


def test_missing_but_individual_fetch_succeeds_clears_missing_count(
    documents: DocumentRepository, sync_dirs
) -> None:
    """一覧に出ないだけで個別取得できるなら欠落ではない(権限変更等の除外)。"""
    runner = make_runner(documents, sync_dirs, missing_threshold=1)
    post = make_post()
    runner.save_post(post)

    async def succeeding_fetch(number: int) -> dict:
        return {"number": number}

    items = asyncio.run(
        runner.detect_missing_posts(
            all_posts=[],
            category_path=None,
            full_sync_succeeded=True,
            prune_orphans=False,
            fetch_post=succeeding_fetch,
        )
    )
    assert items[0].action == str(SyncAction.MISSING)
    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["sync_status"] == str(SyncStatus.SYNCED)
    assert row["missing_count"] == 0


def test_full_sync_not_succeeded_never_marks_source_missing(
    documents: DocumentRepository, sync_dirs
) -> None:
    """全件同期が失敗している最中は欠落判定をしない(API失敗・ページネーション漏れ対策)。"""
    runner = make_runner(documents, sync_dirs, missing_threshold=1)
    post = make_post()
    runner.save_post(post)

    async def failing_fetch(number: int) -> None:
        raise RuntimeError("404")

    items = asyncio.run(
        runner.detect_missing_posts(
            all_posts=[],
            category_path=None,
            full_sync_succeeded=False,
            prune_orphans=False,
            fetch_post=failing_fetch,
        )
    )
    assert items[0].action == str(SyncAction.MISSING)
    row = documents.get(rel_path_of(post, sync_dirs))
    assert row["sync_status"] == str(SyncStatus.SYNCED)


def test_duplicate_post_number_at_different_batch_roots_is_not_conflated(
    documents: DocumentRepository, sync_dirs
) -> None:
    """異なる出力先ルートに同じ post_number が重複しても、片方の同期はもう片方に影響しない。"""
    root_dir, docs_dir, _ = sync_dirs
    runner_a = EsaSyncRunner(
        documents=documents, root_dir=root_dir, docs_dir=docs_dir, output_dir="docs/batchA"
    )
    runner_b = EsaSyncRunner(
        documents=documents, root_dir=root_dir, docs_dir=docs_dir, output_dir="docs/batchB"
    )
    post = make_post()

    item_a = runner_a.save_post(post)
    item_b = runner_b.save_post(post)

    assert item_a.action == str(SyncAction.CREATE)
    assert item_b.action == str(SyncAction.CREATE)
    assert item_a.path != item_b.path
    assert documents.get(item_a.path) is not None
    assert documents.get(item_b.path) is not None


# ---------------------------------------------------------------------------
# 秘密情報(トークン)がどこにも漏れないこと
# ---------------------------------------------------------------------------


def test_saved_file_and_db_record_never_contain_the_access_token(
    documents: DocumentRepository, sync_dirs
) -> None:
    runner = make_runner(documents, sync_dirs)
    post = make_post()
    runner.save_post(post)

    content = read_raw(file_path_of(post, sync_dirs))
    assert FAKE_TOKEN not in content
    row = documents.get(rel_path_of(post, sync_dirs))
    assert FAKE_TOKEN not in json.dumps(row, default=str)


# ---------------------------------------------------------------------------
# EsaClient: モックHTTPサーバー越しの検索・個別取得・ページネーション
# ---------------------------------------------------------------------------


async def _search(server: MockEsaServer, query: str) -> list[dict]:
    async with EsaClient(
        team=server.team, access_token=FAKE_TOKEN, base_url=server.base_url
    ) as client:
        return await client.search_posts(query)


def test_client_search_posts_filters_by_category(esa_server: MockEsaServer) -> None:
    esa_server.add_post(make_post(category="対象カテゴリ"))
    esa_server.add_post(make_post(category="別カテゴリ"))

    posts = asyncio.run(_search(esa_server, 'category:"対象カテゴリ"'))

    assert len(posts) == 1
    assert posts[0]["category"] == "対象カテゴリ"


def test_client_get_post_by_number(esa_server: MockEsaServer) -> None:
    post = make_post()
    esa_server.add_post(post)

    async def _get() -> dict:
        async with EsaClient(
            team=esa_server.team, access_token=FAKE_TOKEN, base_url=esa_server.base_url
        ) as client:
            return await client.get_post(post["number"])

    fetched = asyncio.run(_get())
    assert fetched["number"] == post["number"]


def test_client_get_post_missing_raises_external_service_error(esa_server: MockEsaServer) -> None:
    from abist_kb.domain.errors import AppError

    async def _get() -> None:
        async with EsaClient(
            team=esa_server.team, access_token=FAKE_TOKEN, base_url=esa_server.base_url
        ) as client:
            await client.get_post(999999)

    with pytest.raises(AppError):
        asyncio.run(_get())


def test_client_rejects_wrong_token(esa_server: MockEsaServer) -> None:
    from abist_kb.domain.errors import AppError

    async def _get() -> None:
        async with EsaClient(
            team=esa_server.team, access_token="wrong-token", base_url=esa_server.base_url
        ) as client:
            await client.search_posts("")

    with pytest.raises(AppError):
        asyncio.run(_get())


def test_sanitize_helpers_reused_directly(sync_dirs) -> None:
    """アダプターは `metadata_schema` のサニタイズ関数を再実装せず再利用する。"""
    assert sanitize_file_name("a/b") == "a-b"
    assert sanitize_category_path("a/b/c") == "a/b/c"
