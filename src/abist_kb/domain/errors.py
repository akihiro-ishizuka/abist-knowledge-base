"""アプリケーション共通のエラー型と終了コード(設計書 §8)。

外部API例外やSQLite例外をUIへ直接露出させず、必ず AppError へ正規化する。
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    """プロセス終了コード(設計書 §8)。"""

    SUCCESS = 0
    FAILURE = 1
    INVALID_INPUT = 2
    CONFIG_ERROR = 3
    EXTERNAL_SERVICE = 4
    CONFLICT = 5
    CANCELLED = 130


class ErrorCode(StrEnum):
    """安定したエラーコード。UIとMCP応答で同じ値を使う。

    値は名前と一致させる(StrEnum の auto は小文字になるため明示指定)。
    """

    FAILURE = "FAILURE"
    INVALID_INPUT = "INVALID_INPUT"
    CONFIG_ERROR = "CONFIG_ERROR"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    CONFLICT = "CONFLICT"
    CANCELLED = "CANCELLED"
    NOT_FOUND = "NOT_FOUND"
    FTS5_TRIGRAM_UNAVAILABLE = "FTS5_TRIGRAM_UNAVAILABLE"
    SQLITE_TOO_OLD = "SQLITE_TOO_OLD"
    MIGRATION_FAILED = "MIGRATION_FAILED"


_EXIT_CODE_BY_ERROR: dict[ErrorCode, ExitCode] = {
    ErrorCode.INVALID_INPUT: ExitCode.INVALID_INPUT,
    ErrorCode.CONFIG_ERROR: ExitCode.CONFIG_ERROR,
    ErrorCode.EXTERNAL_SERVICE: ExitCode.EXTERNAL_SERVICE,
    ErrorCode.CONFLICT: ExitCode.CONFLICT,
    ErrorCode.CANCELLED: ExitCode.CANCELLED,
}


class AppError(Exception):
    """UIへ提示できる正規化済みエラー。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        exit_code: ExitCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details: dict[str, Any] = dict(details) if details else {}
        self.retryable = retryable
        self.exit_code = exit_code if exit_code is not None else ExitCode.FAILURE

    def to_dict(self) -> dict[str, Any]:
        """`--output json` と MCP 応答で使う辞書表現。"""
        return {
            "code": str(self.code),
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
            "retryable": self.retryable,
        }


def default_exit_code(code: ErrorCode) -> ExitCode:
    """エラーコードに対応する既定の終了コード。"""
    return _EXIT_CODE_BY_ERROR.get(code, ExitCode.FAILURE)


def wrap(
    exc: BaseException,
    *,
    code: ErrorCode,
    message: str,
    hint: str | None = None,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
    exit_code: ExitCode | None = None,
) -> AppError:
    """外部例外を AppError へ包む。元例外は __cause__ と details に保持する。

    例外の生メッセージは message へ混ぜない(利用者向け文言を壊さないため)。
    詳細は --debug 時に details と traceback から辿る。
    """
    merged: dict[str, Any] = dict(details) if details else {}
    merged.setdefault("cause_type", type(exc).__name__)
    merged.setdefault("cause_message", str(exc))
    err = AppError(
        code=code,
        message=message,
        hint=hint,
        details=merged,
        retryable=retryable,
        exit_code=exit_code if exit_code is not None else default_exit_code(code),
    )
    err.__cause__ = exc
    return err
