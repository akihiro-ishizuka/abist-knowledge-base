"""`SourceService`(設計書 §9.2): ソースの CRUD と接続設定の妥当性テスト。

**`test_connection` は実ネットワーク呼び出しを行わない。** M3 Task 3〜5(esa/web/git
ソースアダプター)がまだ実装されていない現時点では、実際に到達性を確認する手段が
無い。ここでは各 `type` が要求する接続設定キーが揃っているかどうかだけを検証する
「設定の健全性チェック」として実装し、実アダプターが実装され次第 Task 3〜5 が
実接続確認へ差し替えられるようにする(呼び出し側の契約 ―― `{"ok", "detail"}` ――
は変えない想定)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from abist_kb.config import Settings
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.infrastructure.db import audit
from abist_kb.infrastructure.db.sources_repo import SourceRepository

ConfirmFn = Callable[[str], bool]

#: type ごとに接続設定へ必須のキー。
_REQUIRED_CONNECTION_KEYS: dict[str, tuple[str, ...]] = {
    "esa": ("team", "access_token"),
    "web": ("url",),
}

_GIT_REMOVED = (
    "Git 同期は削除されました。リポジトリは git / gh で docs/ に置いてから "
    "document register-disk --apply で登録してください。"
)


class SourceService:
    """`SourceRepository` を包み、確認・監査を伴う操作を提供する。

    `settings` は `test_connection` の健全性チェックにのみ使う。§12 に従い
    esa/git の資格情報は `sources.connection` へ書かせないため、`connection` が
    空でも `Settings`(`.env`)から埋まる分は「不足」と報告しない
    (`application.sync_service.SyncService._resolve_esa_credentials` と同じ理由)。
    `settings` を渡さない呼び出し元は従来通り DB の `connection` のみで判定する。
    """

    def __init__(self, conn: sqlite3.Connection, *, settings: Settings | None = None) -> None:
        self._conn = conn
        self._repo = SourceRepository(conn)
        self._settings = settings

    def add(
        self,
        *,
        type: str,
        display_name: str,
        connection: dict[str, Any] | None = None,
        output_dir: str,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if type == "git":
            raise AppError(code=ErrorCode.INVALID_INPUT, message=_GIT_REMOVED)
        return self._repo.create(
            type=type,
            display_name=display_name,
            connection=connection,
            output_dir=output_dir,
            enabled=enabled,
        )

    def list(self) -> list[dict[str, Any]]:
        return self._repo.list()

    def get(self, source_id: str) -> dict[str, Any]:
        source = self._repo.get(source_id)
        if source is None:
            raise AppError(code=ErrorCode.NOT_FOUND, message=f"ソースが見つかりません: {source_id}")
        return source

    def edit(self, source_id: str, **fields: Any) -> dict[str, Any]:
        self.get(source_id)  # NOT_FOUND を先に出す
        return self._repo.update(source_id, **fields)

    def remove(self, source_id: str, *, confirm: ConfirmFn, actor: str | None = None) -> bool:
        existing = self.get(source_id)  # NOT_FOUND を先に出す
        if not confirm(f"ソース '{existing['display_name']}' を削除しますか?"):
            return False
        deleted = self._repo.delete(source_id)
        if deleted:
            audit.record_event(
                self._conn,
                action="source.delete",
                target_type="source",
                target_id=source_id,
                details={"display_name": existing["display_name"], "type": existing["type"]},
                actor=actor,
            )
        return deleted

    def test_connection(self, source_id: str) -> dict[str, Any]:
        """接続設定の健全性チェック(実ネットワーク呼び出しはしない、モジュール docstring参照)。"""
        source = self.get(source_id)
        required = _REQUIRED_CONNECTION_KEYS.get(source["type"])
        if required is None:
            return {"ok": False, "detail": f"未知のソース種別です: {source['type']}"}
        connection = source.get("connection") or {}
        fallback: dict[str, Any] = {}
        if self._settings is not None and source["type"] == "esa":
            fallback = {
                "team": self._settings.esa_team_name,
                "access_token": self._settings.esa_access_token,
            }
        missing = [key for key in required if not connection.get(key) and not fallback.get(key)]
        if missing:
            return {
                "ok": False,
                "detail": f"接続設定に不足しているキーがあります: {', '.join(missing)}",
            }
        return {"ok": True, "detail": "必須の接続設定キーが揃っています。"}


__all__ = ["ConfirmFn", "SourceService"]
