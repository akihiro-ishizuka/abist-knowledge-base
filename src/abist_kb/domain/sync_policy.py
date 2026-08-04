"""差分同期の判定ロジック(純粋関数)。

旧実装 `tools/lib/sync-planner.js` の移植。ネットワークもファイルシステムも
触らない。判定の骨子は「取得元が変わったか × ローカルが編集されたか」の2軸:

|                | ローカル未編集 | ローカル編集済み          |
| -------------- | -------------- | -------------------------- |
| 取得元 未変更  | unchanged      | local_modified(上書きしない) |
| 取得元 更新    | update         | conflict(自動上書きしない)   |

判断材料が足りないときは「書かない」側に倒す(unknown_local / adopt)。

挙動の正しさは `tests/fixtures/kernel/sync-planner.json`(旧実装を実行して得た
ゴールデン値)で判定する。`ACTION_BUCKET` はレポート集計用の6区分マッピングで、
旧実装が非公開定数としていたため fixture は `recordSyncResult` 経由の観測結果
として記録されている。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class SyncAction(StrEnum):
    """1文書について何をするかの判定結果。"""

    CREATE = "create"
    UPDATE = "update"
    UNCHANGED = "unchanged"
    LOCAL_MODIFIED = "local_modified"
    CONFLICT = "conflict"
    CONFLICT_OVERWRITTEN = "conflict_overwritten"
    ADOPT = "adopt"
    UNKNOWN_LOCAL = "unknown_local"
    MISSING = "missing"
    ORPHAN = "orphan"
    ERROR = "error"


class SyncStatus(StrEnum):
    """`documents.sync_status` に永続化される同期状態(DB側にのみ持つ)。"""

    SYNCED = "synced"
    MODIFIED_LOCAL = "modified_local"
    CONFLICT = "conflict"
    SOURCE_MISSING = "source_missing"
    ERROR = "error"


_SYNC_STATUS_BY_ACTION: Mapping[SyncAction, SyncStatus] = {
    SyncAction.CREATE: SyncStatus.SYNCED,
    SyncAction.UPDATE: SyncStatus.SYNCED,
    SyncAction.UNCHANGED: SyncStatus.SYNCED,
    SyncAction.LOCAL_MODIFIED: SyncStatus.MODIFIED_LOCAL,
    SyncAction.CONFLICT: SyncStatus.CONFLICT,
    SyncAction.CONFLICT_OVERWRITTEN: SyncStatus.SYNCED,
    SyncAction.ADOPT: SyncStatus.SYNCED,
    SyncAction.UNKNOWN_LOCAL: SyncStatus.SYNCED,
    SyncAction.MISSING: SyncStatus.SOURCE_MISSING,
    SyncAction.ORPHAN: SyncStatus.SOURCE_MISSING,
    SyncAction.ERROR: SyncStatus.ERROR,
}


def sync_status_for(action: SyncAction) -> SyncStatus:
    """`SyncAction` を DB永続化用の `SyncStatus` へ変換する。

    `conflict` -> `conflict`、`local_modified` -> `modified_local`、
    `missing`/`orphan` -> `source_missing`、`error` -> `error`、それ以外は
    `synced`(旧 `download-article.js` の `syncStatusFor` と、書き込み時に
    ハードコードされていた `sync_status: 'synced'` を統合した対応)。
    """
    return _SYNC_STATUS_BY_ACTION[action]


ACTION_BUCKET: Mapping[SyncAction, str] = {
    SyncAction.CREATE: "added",
    SyncAction.UPDATE: "updated",
    SyncAction.CONFLICT_OVERWRITTEN: "updated",
    SyncAction.UNCHANGED: "skipped",
    SyncAction.LOCAL_MODIFIED: "skipped",
    SyncAction.ADOPT: "skipped",
    SyncAction.UNKNOWN_LOCAL: "skipped",
    SyncAction.CONFLICT: "conflict",
    SyncAction.MISSING: "missing",
    # 旧パスの取り残しも「その場所には取得元の文書が無い」ため missing に数える
    SyncAction.ORPHAN: "missing",
    SyncAction.ERROR: "error",
}


@dataclass(frozen=True, slots=True)
class RemoteState:
    """取得元の現在の状態。"""

    content_hash: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class SyncRecord:
    """sync-state DB の行(None なら未記録)。"""

    local_content_hash: str | None = None
    source_content_hash: str | None = None
    source_updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class LocalState:
    """ローカルファイルの現在の状態。"""

    exists: bool
    body_hash: str | None = None


@dataclass(frozen=True, slots=True)
class SyncDecision:
    """`decide_sync_action` の戻り値。"""

    action: SyncAction
    write: bool
    reason: str
    remote_changed: bool | None = None
    local_modified: bool | None = None


@dataclass(frozen=True, slots=True)
class MissingDecision:
    """`decide_missing_candidate` の戻り値。"""

    source_missing: bool
    change_status: bool
    reason: str


def _is_remote_changed(remote: RemoteState | None, record: SyncRecord) -> bool:
    """取得元が変わったかを判定する。

    本文ハッシュがあればそれを優先し(updated_at だけ動く更新を無視できる)、
    無ければ updated_at で判定する。比較材料が無い場合は「変わった」とみなす
    (取りこぼしより再取得を選ぶ)。
    """
    remote_content_hash = remote.content_hash if remote else None
    if remote_content_hash and record.source_content_hash:
        return remote_content_hash != record.source_content_hash

    remote_updated_at = remote.updated_at if remote else None
    if remote_updated_at and record.source_updated_at:
        current = _parse_date(remote_updated_at)
        recorded = _parse_date(record.source_updated_at)
        if current is None or recorded is None:
            return True
        return current > recorded

    return True


def _parse_date(value: str) -> datetime | None:
    """ISO 8601 文字列を aware な UTC datetime へ変換する。

    JS の `Date.parse` は素の日付文字列(`'2024-01-01'` のような時刻・オフセット
    無し)も常に UTC 深夜として解釈し、常に比較可能な数値(エポックミリ秒)を
    返す。Python の `datetime.fromisoformat` は naive/aware どちらも生成しうる
    ため、そのまま比較すると `TypeError: can't compare offset-naive and
    offset-aware datetimes` になる(front matter の `updated_at` は日付のみ=naive、
    取得元 API のタイムスタンプはオフセット付き=aware、という組み合わせが
    backfill 済み文書で実際に発生する)。ここで naive な結果を UTC 扱いに揃え、
    JS と同じく常に比較可能にする。
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def decide_sync_action(
    *,
    remote: RemoteState | None,
    record: SyncRecord | None,
    local: LocalState | None,
    force: bool = False,
) -> SyncDecision:
    """1文書について何をするかを決める。"""
    if local is None or not local.exists:
        return SyncDecision(
            action=SyncAction.CREATE,
            write=True,
            reason="ローカルにファイルがありません",
        )

    if record is None:
        return SyncDecision(
            action=SyncAction.ADOPT,
            write=False,
            reason=(
                "sync-state に記録がないため、ローカル編集の有無を判断できません。"
                "記録のみ作成し上書きしません"
            ),
        )

    remote_changed = _is_remote_changed(remote, record)

    if not record.local_content_hash:
        return SyncDecision(
            action=SyncAction.UNKNOWN_LOCAL,
            write=False,
            reason="local_content_hash が未記録のためローカル編集の有無を判断できません",
            remote_changed=remote_changed,
            local_modified=None,
        )

    local_modified = local.body_hash != record.local_content_hash

    if remote_changed and local_modified:
        if force:
            return SyncDecision(
                action=SyncAction.CONFLICT_OVERWRITTEN,
                write=True,
                reason="--force 指定のため取得元の内容で上書きしました",
                remote_changed=remote_changed,
                local_modified=local_modified,
            )
        return SyncDecision(
            action=SyncAction.CONFLICT,
            write=False,
            reason="取得元が更新され、かつローカルも編集されています。自動上書きしません",
            remote_changed=remote_changed,
            local_modified=local_modified,
        )

    if remote_changed:
        return SyncDecision(
            action=SyncAction.UPDATE,
            write=True,
            reason="取得元が更新されています",
            remote_changed=remote_changed,
            local_modified=local_modified,
        )

    if local_modified:
        return SyncDecision(
            action=SyncAction.LOCAL_MODIFIED,
            write=False,
            reason="ローカルのみ編集されています。上書きしません",
            remote_changed=remote_changed,
            local_modified=local_modified,
        )

    return SyncDecision(
        action=SyncAction.UNCHANGED,
        write=False,
        reason="取得元・ローカルとも変化なし",
        remote_changed=remote_changed,
        local_modified=local_modified,
    )


def decide_missing_candidate(
    *,
    full_sync_succeeded: bool,
    missing_count: int,
    individual_fetch_failed: bool,
    threshold: int = 3,
) -> MissingDecision:
    """取得元からの欠落を確定してよいかを判定する。

    次の3条件すべてを満たしたときだけ `source_missing` を提案する:
      1. 全件同期が正常終了している(API失敗・ページネーション漏れの最中に判定しない)
      2. 連続不在回数が閾値以上(カテゴリ移動・WIP変化の一時的な不在を除外)
      3. 記事ID個別取得でも取得できない(一覧に出ないだけの権限変更等を除外)

    業務状態(deprecated 等)の変更は常に人間判断なので、`change_status` は
    常に `False`。
    """
    if not full_sync_succeeded:
        return MissingDecision(
            source_missing=False,
            change_status=False,
            reason="全件同期が正常終了していないため欠落と判定しません（API失敗・ページネーション漏れの可能性）",
        )
    if not individual_fetch_failed:
        return MissingDecision(
            source_missing=False,
            change_status=False,
            reason="記事ID個別取得では取得できたため欠落ではありません（一覧に出ないだけ）",
        )
    if missing_count < threshold:
        return MissingDecision(
            source_missing=False,
            change_status=False,
            reason=(
                f"連続不在 {missing_count} 回で閾値 {threshold} に達していません"
                "（カテゴリ移動・WIP変化の可能性）"
            ),
        )
    return MissingDecision(
        source_missing=True,
        change_status=False,
        reason=f"全件同期成功 + 連続不在 {missing_count} 回 + 個別取得も失敗",
    )


__all__ = [
    "ACTION_BUCKET",
    "LocalState",
    "MissingDecision",
    "RemoteState",
    "SyncAction",
    "SyncDecision",
    "SyncRecord",
    "SyncStatus",
    "decide_missing_candidate",
    "decide_sync_action",
    "sync_status_for",
]
