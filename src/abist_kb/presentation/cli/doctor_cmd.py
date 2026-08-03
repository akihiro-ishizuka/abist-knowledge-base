"""doctor 診断コマンド(設計書 §7)。

各チェック項目は `{"name", "status", "detail", "hint"}` の辞書として表す。
`status` が `"fail"` のものが1つでもあれば `AppError` を送出し、終了コード3
(`CONFIG_ERROR`)で終了する。trigram トークナイザが利用できない場合は
`ErrorCode.FTS5_TRIGRAM_UNAVAILABLE` を使い、索引の検索精度が実測せずに
黙って劣化する状態を防ぐ(再構築手順を hint に載せる)。
"""

from __future__ import annotations

import shutil
import sys
from importlib.util import find_spec
from typing import Literal, TypedDict

import typer

from abist_kb.config import Settings
from abist_kb.domain.errors import AppError, ErrorCode, ExitCode
from abist_kb.infrastructure.db.connection import (
    MIN_SQLITE_VERSION,
    check_sqlite_capabilities,
)
from abist_kb.presentation.cli.context import get_context
from abist_kb.presentation.console.presenter import Presenter
from abist_kb.presentation.console.theme import TOKEN_STYLES, SemanticToken

Status = Literal["ok", "warn", "fail"]


class CheckResult(TypedDict):
    name: str
    status: Status
    detail: str
    hint: str | None


_STATUS_TOKEN: dict[Status, SemanticToken] = {
    "ok": SemanticToken.SUCCESS,
    "warn": SemanticToken.WARNING,
    "fail": SemanticToken.DANGER,
}


def _result(name: str, status: Status, detail: str, hint: str | None = None) -> CheckResult:
    return {"name": name, "status": status, "detail": detail, "hint": hint}


def _min_version_label() -> str:
    return ".".join(str(part) for part in MIN_SQLITE_VERSION)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".")[:3])


def _check_python() -> CheckResult:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] == (3, 12):
        return _result("python", "ok", version)
    return _result("python", "warn", version, hint="Python 3.12 系での実行を推奨します。")


def _check_sqlite_version(report_version: str) -> CheckResult:
    detail = f"{report_version}(必要: {_min_version_label()} 以上)"
    if _version_tuple(report_version) >= MIN_SQLITE_VERSION:
        return _result("sqlite", "ok", detail)
    return _result(
        "sqlite",
        "fail",
        detail,
        hint=f"SQLite {_min_version_label()} 以上を含む Python で実行してください。",
    )


def _first_problem(problems: tuple[str, ...], *, startswith: str) -> str | None:
    """`CapabilityReport.problems` から、この検査に対応する実測メッセージを探す。

    各 `_probe_*` はどれも識別可能な接頭辞を持つメッセージを最大1件だけ積むため、
    接頭辞の前方一致で対応付けられる。見つからなければ `None`(理論上は
    起きないが、`problems` の構造が変わっても検査自体はクラッシュしないための
    防御)。
    """
    for problem in problems:
        if problem.startswith(startswith):
            return problem
    return None


def _check_fts5(ok: bool, problems: tuple[str, ...]) -> CheckResult:
    if ok:
        return _result("fts5", "ok", "FTS5 拡張を利用できます。")
    detail = _first_problem(problems, startswith="FTS5") or "FTS5 拡張を利用できません。"
    return _result(
        "fts5",
        "fail",
        detail,
        hint="FTS5 が有効な SQLite を含む Python で実行してください。",
    )


def _check_unicode61(ok: bool, problems: tuple[str, ...]) -> CheckResult:
    if ok:
        return _result("unicode61", "ok", "unicode61 トークナイザを利用できます。")
    detail = _first_problem(problems, startswith="unicode61")
    detail = detail or "unicode61 トークナイザを利用できません。"
    return _result(
        "unicode61",
        "fail",
        detail,
        hint="FTS5 が有効な SQLite を含む Python で実行してください。",
    )


def _check_trigram(ok: bool, problems: tuple[str, ...]) -> CheckResult:
    if ok:
        return _result("trigram", "ok", "trigram トークナイザを利用できます(3文字語で実測)。")
    detail = _first_problem(problems, startswith="trigram")
    detail = detail or "trigram トークナイザを利用できません。"
    return _result(
        "trigram",
        "fail",
        detail,
        hint=(
            "SQLite を trigram 対応版へ更新したうえで検索索引を再構築してください。"
            "検索精度が黙って劣化した状態のまま運用しないでください。"
        ),
    )


def _check_external_content(ok: bool, problems: tuple[str, ...]) -> CheckResult:
    """§4 の実務/参照コーパス2索引が前提にする external-content FTS5 構成を実測する。"""
    if ok:
        return _result("external_content", "ok", "external-content FTS5 テーブルを利用できます。")
    detail = (
        _first_problem(problems, startswith="external-content")
        or "external-content FTS5 テーブルを利用できません。"
    )
    return _result(
        "external_content",
        "fail",
        detail,
        hint="FTS5 が有効な SQLite を含む Python で実行してください。",
    )


def _check_bm25(ok: bool, problems: tuple[str, ...]) -> CheckResult:
    """§4 の検索が関連度計算に使う `bm25()` ランキング関数を実測する。"""
    if ok:
        return _result("bm25", "ok", "bm25() ランキング関数を利用できます。")
    detail = _first_problem(problems, startswith="bm25()")
    detail = detail or "bm25() ランキング関数を利用できません。"
    return _result(
        "bm25",
        "fail",
        detail,
        hint="FTS5 が有効な SQLite を含む Python で実行してください。",
    )


def _check_config_file(settings: Settings) -> CheckResult:
    path = settings.config_file
    if path is not None and path.is_file():
        return _result("config_file", "ok", f"設定ファイルを検出しました: {path}")
    return _result("config_file", "ok", f"設定ファイルは未作成です(既定値を使用します): {path}")


def _check_data_dir(settings: Settings) -> CheckResult:
    data_dir = settings.data_dir
    if data_dir is None:
        return _result(
            "data_dir", "fail", "data_dir が未解決です。", hint="設定を確認してください。"
        )
    try:
        settings.ensure_directories()
        probe = data_dir / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return _result(
            "data_dir",
            "fail",
            f"データディレクトリに書き込めません: {data_dir}({exc})",
            hint="ディレクトリの権限を確認してください。",
        )
    return _result("data_dir", "ok", f"書込可能です: {data_dir}")


def _check_ffmpeg() -> CheckResult:
    path = shutil.which("ffmpeg")
    if path:
        return _result("ffmpeg", "ok", f"検出しました: {path}")
    return _result(
        "ffmpeg",
        "warn",
        "ffmpeg が見つかりません。",
        hint=(
            "https://ffmpeg.org/download.html からインストールしてください"
            "(動画書き出し機能が制限されます)。"
        ),
    )


def _check_manim() -> CheckResult:
    if find_spec("manim") is not None:
        return _result("manim", "ok", "Manim を検出しました。")
    return _result(
        "manim",
        "warn",
        "Manim が見つかりません。",
        hint="`uv add manim` 等でインストールしてください(図解の自動生成機能が制限されます)。",
    )


def _run_checks(settings: Settings) -> list[CheckResult]:
    report = check_sqlite_capabilities()
    return [
        _check_python(),
        _check_sqlite_version(report.sqlite_version),
        _check_fts5(report.fts5, report.problems),
        _check_unicode61(report.unicode61, report.problems),
        _check_trigram(report.trigram, report.problems),
        _check_external_content(report.external_content, report.problems),
        _check_bm25(report.bm25, report.problems),
        _check_config_file(settings),
        _check_data_dir(settings),
        _check_ffmpeg(),
        _check_manim(),
    ]


def _first_failure_error(checks: list[CheckResult]) -> AppError | None:
    """複数の `fail` が同時に起きても、最も対処すべきものを1つだけ選ぶ。

    trigram 不可は索引の検索精度が黙って劣化しうる最重要項目のため、
    他の fail(例: SQLite バージョン過小)と同時発生しても優先して報告する。
    """
    by_name = {c["name"]: c for c in checks}
    trigram = by_name.get("trigram")
    if trigram is not None and trigram["status"] == "fail":
        return AppError(
            code=ErrorCode.FTS5_TRIGRAM_UNAVAILABLE,
            message="trigram トークナイザを利用できないため、検索索引を安全に構築できません。",
            hint=trigram["hint"],
            exit_code=ExitCode.CONFIG_ERROR,
        )
    sqlite_check = by_name.get("sqlite")
    if sqlite_check is not None and sqlite_check["status"] == "fail":
        return AppError(
            code=ErrorCode.SQLITE_TOO_OLD,
            message="SQLite のバージョンが古すぎます。",
            hint=sqlite_check["hint"],
            exit_code=ExitCode.CONFIG_ERROR,
        )
    for check in checks:
        if check["status"] == "fail":
            return AppError(
                code=ErrorCode.CONFIG_ERROR,
                message=f"診断チェック '{check['name']}' が失敗しました: {check['detail']}",
                hint=check["hint"],
                exit_code=ExitCode.CONFIG_ERROR,
            )
    return None


def _status_symbol(status: Status) -> str:
    return TOKEN_STYLES[_STATUS_TOKEN[status]].symbol


def _render(presenter: Presenter, checks: list[CheckResult]) -> None:
    if presenter.is_json:
        ok = not any(c["status"] == "fail" for c in checks)
        presenter.json_result({"ok": ok, "checks": [dict(c) for c in checks]})
        return

    rows = [[_status_symbol(c["status"]), c["name"], c["status"], c["detail"]] for c in checks]
    presenter.table("doctor", ["記号", "名前", "状態", "詳細"], rows)
    for check in checks:
        if check["status"] == "fail" and check["hint"]:
            presenter.danger(f"{check['name']}: {check['hint']}")
        elif check["status"] == "warn" and check["hint"]:
            presenter.warning(f"{check['name']}: {check['hint']}")


def doctor(ctx: typer.Context) -> None:
    """SQLite の機能・依存ツールの実行可否を実測して報告する。"""
    cli_ctx = get_context(ctx)
    checks = _run_checks(cli_ctx.settings)
    _render(cli_ctx.presenter, checks)
    error = _first_failure_error(checks)
    if error is not None:
        # `fail()` を直接呼ぶのではなく `raise` する。`doctor` は
        # `AppTyper.command()` 経由(既定 `cls=AppErrorHandlingCommand`)で
        # 登録されているため、`AppError` を投げるだけで choke point が
        # 拾って `Presenter` 提示・終了コード変換の両方を行う。ここで
        # `fail()` を直接呼ぶと、この関所を経由しないコマンドの書き方を
        # `doctor_cmd.py` というM3が模写する前提のテンプレート自身が
        # 示してしまう(今日時点では外部から見た挙動に差は無いが、悪い手本)。
        raise error


__all__ = ["check_sqlite_capabilities", "doctor"]
