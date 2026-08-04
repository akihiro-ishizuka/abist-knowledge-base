"""並行稼働比較ハーネス(設計書 §14 step 8, M9 task 9.2)。

M3 のバイト同一検証(`tests/sources/test_e2e_byte_identity.py`)、M5 の MCP
契約 diff(`tests/mcp/test_all_server_tools_list_diff.py`)、M4 の検索品質評価
(`application.audit.search_quality.evaluate`)という、milestone ごとに別々に
作られた3つの比較手段を1コマンドから呼び出し、まとめて記録する。第4の
比較実装を新たに書くのではなく、既存の3つを束ねるだけ(読み取り専用、
旧リポジトリへは一切書き込まない)。

- 同期判定/収集の新旧一致: `tests/sources/test_e2e_byte_identity.py` を
  pytest サブプロセスとして実行し、結果(pass/fail件数)を記録する。
  旧リポジトリ(`multi-source-knowledge-base`)が無い環境では
  `skipped` として明示する(このモジュール自身は旧リポジトリに触れない —
  実行は pytest 経由でテスト側のサンドボックス機構に委ねる)。
- MCP 応答の新旧一致: `tests/mcp/test_all_server_tools_list_diff.py` を
  同様にサブプロセス実行する。
- 検索品質(22クエリ): `evaluate()` を直接呼び、`compare_with_previous()`
  で直近レポートとの悪化を検出する(索引が無ければ `skipped`)。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from abist_kb.application.audit.search_quality import (
    compare_with_previous,
    evaluate,
    find_latest_report,
    load_queries,
    write_report,
)

#: リポジトリルートからの相対パス。pytest はこのモジュールファイルの位置から
#: 逆算したリポジトリルートを cwd として起動する。
_BYTE_IDENTITY_TEST = "tests/sources/test_e2e_byte_identity.py"
_MCP_DIFF_TEST = "tests/mcp/test_all_server_tools_list_diff.py"


@dataclass
class PytestSectionResult:
    name: str
    status: str  # "passed" | "failed" | "skipped" | "error"
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class ParallelCompareReport:
    generated_at: str
    repo_root: str
    sync_and_collection: PytestSectionResult
    mcp_contract: PytestSectionResult
    search_quality: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "repo_root": self.repo_root,
            "sync_and_collection": self.sync_and_collection.as_dict(),
            "mcp_contract": self.mcp_contract.as_dict(),
            "search_quality": self.search_quality,
        }

    def all_ok(self) -> bool:
        statuses = {self.sync_and_collection.status, self.mcp_contract.status}
        if statuses - {"passed", "skipped"}:
            return False
        sq_status = self.search_quality.get("status")
        return sq_status in (None, "ok", "skipped")


def default_repo_root() -> Path:
    """このパッケージが属する abist-knowledge-base リポジトリのルートを推定する。

    `--root`(調査対象データのルート、例: 旧リポジトリ)とは別物 —
    pytest でテストを実行するには *このソースツリー* の場所が要る。
    このファイルから `pyproject.toml` を持つ祖先ディレクトリを遡って探す。
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "tests").is_dir():
            return candidate
    return here.parents[4]


def _run_pytest_module(repo_root: Path, name: str, relative_path: Path) -> PytestSectionResult:
    """指定テストファイルを `-q` でサブプロセス実行し、結果を分類する。"""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", str(relative_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = "\n".join(proc.stdout.strip().splitlines()[-5:]) if proc.stdout else ""
    stdout_lower = proc.stdout.lower()
    # 旧リポジトリ不在などで module 丸ごとスキップされたケース。
    if proc.returncode == 0 and "skip" in stdout_lower and "passed" not in stdout_lower:
        return PytestSectionResult(name=name, status="skipped", detail=tail)
    if proc.returncode == 0:
        return PytestSectionResult(name=name, status="passed", detail=tail)
    return PytestSectionResult(name=name, status="failed", detail=tail or proc.stderr[-500:])


def run_parallel_compare(
    *,
    repo_root: Path,
    work_index_conn: Any | None = None,
    queries_path: Path | None = None,
    reports_dir: Path | None = None,
    docs_dir: Path | None = None,
) -> ParallelCompareReport:
    """3つの既存比較手段をまとめて実行し、単一レポートにする。

    `work_index_conn` / `queries_path` / `reports_dir` / `docs_dir` を渡さない
    場合、検索品質セクションは `skipped`(索引未指定)として記録する。
    """
    sync_result = _run_pytest_module(
        repo_root, "sync_and_collection_byte_identity", Path(_BYTE_IDENTITY_TEST)
    )
    mcp_result = _run_pytest_module(repo_root, "mcp_tools_list_diff", Path(_MCP_DIFF_TEST))

    search_quality: dict[str, Any] = {"status": "skipped", "reason": "索引/クエリが未指定"}
    if work_index_conn is not None and queries_path is not None and docs_dir is not None:
        query_list = load_queries(queries_path)
        report = evaluate(work_index_conn, query_list, docs_dir=docs_dir)
        previous_path = find_latest_report(reports_dir) if reports_dir is not None else None
        previous = (
            json.loads(previous_path.read_text(encoding="utf-8"))
            if previous_path is not None
            else None
        )
        warnings = compare_with_previous(report, previous)
        report_path = (
            write_report(report, reports_dir=reports_dir) if reports_dir is not None else None
        )
        search_quality = {
            "status": "ok",
            "macro": report["macro"],
            "warnings": warnings,
            "report_path": str(report_path) if report_path is not None else None,
        }

    return ParallelCompareReport(
        generated_at=datetime.now(UTC).isoformat(),
        repo_root=str(repo_root),
        sync_and_collection=sync_result,
        mcp_contract=mcp_result,
        search_quality=search_quality,
    )
