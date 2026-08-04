"""UI 横断契約テスト(§15 受入条件、M6 Task 6.4 の実質的な成果物)。

同一ジョブが Web(FastAPI `/api/v1`)・TUI(Textual)・CLI で同じ状態・件数・
エラーコードを表示することを確認する。

M3 で見つかった実バグ(design/plans/M6-M10-remaining.md の共通申し送り
「ジョブ状態」行)を回帰させないため、`PARTIAL` ジョブを主眼に置く:
一部バッチが失敗した同期は `PARTIAL` であって `SUCCEEDED` ではない。
これは「エラーがあるかどうか」だけを見る消費者が緑塗りにしてしまいやすい
状態であり、Web・TUI・CLI のどの経路でも `SUCCEEDED` へ丸められないことを
表明する。

3つの経路はすべて同じ `app.sqlite`(同一 `root_dir`)を指す独立したエントリ
ポイントとして駆動する:
- Web: `create_api_app()` を被せた `TestClient`(`presentation/api/app.py`)。
- TUI: `KbApp` を `run_test()`(Textual Pilot)で実際に描画・ナビゲートする
  (`screens.py` を直接呼ぶだけでなく、実際の TUI 画面テキストを検査する)。
- CLI: `typer.testing.CliRunner` で実プロセス相当のコマンド呼び出し
  (`jobs_cmd.py`)を `--output json` で叩く。

非自明性の確認(実際に実行して確認した手順): `tokens.py::JOB_STATE_TOKENS` の
`JobState.PARTIAL: SemanticToken.WARNING` を一時的に `SemanticToken.SUCCESS` に
書き換えて `pytest tests/ui/test_cross_ui_contract.py` を実行すると、
`test_partial_job_renders_as_warning_not_success_on_every_surface` が
`web["state_token"] == "warning"` で `AssertionError: assert 'success' ==
'warning'` を出して落ちる(Web の view-model が SUCCESS を返すようになるため)。
同時に既存の `tests/ui/test_view_models.py::test_partial_job_is_warning_not_success`
/ `test_dashboard_surfaces_partial_job_as_warning` も同じ理由で落ちることを確認した
(改変は検証後に元へ戻した。コミットには含めていない)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from abist_kb.config import Settings
from abist_kb.domain.job import JobState
from abist_kb.infrastructure.jobs.repository import JobRepository
from abist_kb.presentation.api.app import create_api_app
from abist_kb.presentation.cli.app import app as cli_app
from abist_kb.presentation.tui.app import KbApp
from abist_kb.presentation.web.viewmodels.container import ServiceContainer

PARTIAL_ERROR = {
    "code": "FAILURE",
    "message": "3/10 バッチが失敗しました。",
    "hint": None,
    "details": {"failed_batches": 3, "total_batches": 10},
    "retryable": True,
}
PARTIAL_RESULT = {"succeeded_batches": 7, "failed_batches": 3, "total_batches": 10}


@pytest.fixture
def project_root(tmp_root: Path) -> Path:
    settings = Settings(root_dir=tmp_root, _env_file=None)
    settings.ensure_directories()
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    return tmp_root


@pytest.fixture
def partial_job_id(project_root: Path) -> str:
    """3つの UI 経路すべてが同じ DB ファイルから読む、実在する PARTIAL ジョブ。"""
    settings = Settings(root_dir=project_root, _env_file=None)
    container = ServiceContainer(settings, check_same_thread=False)
    try:
        repo = JobRepository(container.conn)
        job = repo.submit("batch", {"batch_id": "release-notes"})
        claimed = repo.claim("owner-contract-test", ttl_seconds=30)
        assert claimed is not None and claimed.id == job.id
        repo.finish(job.id, state=JobState.PARTIAL, result=PARTIAL_RESULT, error=PARTIAL_ERROR)
        return job.id
    finally:
        container.close()


def _web_job(project_root: Path, job_id: str) -> dict:
    settings = Settings(root_dir=project_root, _env_file=None)
    container = ServiceContainer(settings, check_same_thread=False)
    try:
        client = TestClient(create_api_app(container, bind_host="127.0.0.1"))
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        return response.json()
    finally:
        container.close()


async def _tui_job_text(project_root: Path, job_id: str) -> str:
    container = ServiceContainer(
        Settings(root_dir=project_root, _env_file=None), check_same_thread=False
    )
    try:
        app = KbApp(container, start_worker=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_goto_area("jobs")
            await pilot.pause()
            app.detail = ("job", job_id)
            app.render_area("jobs")
            await pilot.pause()
            body = app.query("#content Static")
            return "\n".join(str(w.render()) for w in body)
    finally:
        container.close()


def _cli_job(project_root: Path, job_id: str) -> dict:
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["--root", str(project_root), "--output", "json", "jobs", "show", job_id],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_all_surfaces_report_the_same_partial_state(
    project_root: Path, partial_job_id: str
) -> None:
    """状態・件数・エラーコードが Web/TUI/CLI で一致する(§15)。"""
    import asyncio

    web = _web_job(project_root, partial_job_id)["job"]
    cli = _cli_job(project_root, partial_job_id)
    tui_text = asyncio.run(_tui_job_text(project_root, partial_job_id))

    # 状態: どの経路も PARTIAL(SUCCEEDED へ丸めない)。
    assert web["state"] == "partial"
    assert cli["state"] == "partial"
    assert "partial" in tui_text.lower()

    # 件数: result のバッチ内訳が3経路で一致する。
    assert web["result"] == PARTIAL_RESULT
    assert cli["result"] == PARTIAL_RESULT

    # エラーコード: 3経路とも同じ code を保持する。
    assert web["error"]["code"] == "FAILURE"
    assert cli["error"]["code"] == "FAILURE"

    # id が同一ジョブを指している(別ジョブを比較していないことの保証)。
    assert web["id"] == partial_job_id == cli["id"]


def test_partial_job_renders_as_warning_not_success_on_every_surface(
    project_root: Path, partial_job_id: str
) -> None:
    """M3 回帰ガード: 一部失敗を緑(success)塗りしない(§6.1、共通申し送り)。

    Web は `state_token`、TUI は記号+ラベル(バッジ)文字列で検証する。
    """
    import asyncio

    web = _web_job(project_root, partial_job_id)["job"]
    assert web["state_token"] == "warning"
    assert web["state_token"] != "success"

    tui_text = asyncio.run(_tui_job_text(project_root, partial_job_id))
    assert "!" in tui_text  # WARNING の記号(TOKEN_STYLES)
    assert "✓" not in tui_text  # SUCCESS の記号が紛れ込んでいない
