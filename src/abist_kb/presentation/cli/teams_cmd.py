"""`teams` コマンド群: 連絡チャットの監視と応答(設計 §10.0)。

Track A では取得と判断が Claude 側にあるため、tick を3つに分解している。
`inbox ingest` が判断の必要な件を返し、Claude が回答を作り、`reply` が投稿する。
`questions track` / `questions mark` も同じ理由で分けている――「これは質問か」
「解決したか」は Claude の判断であり、Python 側はその結果を記録するだけ。

呼び出し元は人間ではなく Claude なので、全コマンドが `Presenter.json_result()`
経由で JSON を1回だけ返す(`--output` の指定に関わらず)。`json_result()` は
「stdout は単一の JSON ドキュメントのみ」という契約を1プロセス1回の呼び出しで
強制する唯一の経路であり、`typer.echo`/`json.dumps` を直接使ってはならない。

**このコマンド群は投稿の重複を完全には防げない。** Webhook に idempotency 機構が
無いため、送信結果が不明な場合は再送しない(at-most-once)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import httpx
import typer

from abist_kb.application.chat_watch.source import JsonFileMessageSource
from abist_kb.application.chat_watch.state import (
    apply_rate_limit,
    clear_rate_limit,
    is_allowed,
    load_state,
    save_state,
)
from abist_kb.application.chat_watch.tick import (
    due_reminders,
    mark_question,
    run_ingest,
    run_reply,
    skip_message,
    track_question,
)
from abist_kb.domain.chat_watch import QuestionStatus
from abist_kb.domain.errors import AppError, ErrorCode
from abist_kb.presentation.cli.context import AppTyper, get_context

teams_app = AppTyper(help="連絡チャットの監視と応答。", no_args_is_help=True)
inbox_app = AppTyper(help="受信の取り込み。", no_args_is_help=True)
questions_app = AppTyper(help="質問の追跡。", no_args_is_help=True)
reminders_app = AppTyper(help="放置された質問の抽出。", no_args_is_help=True)
state_app = AppTyper(help="監視 state の確認。", no_args_is_help=True)
backoff_app = AppTyper(help="429 バックオフの管理。", no_args_is_help=True)
teams_app.add_typer(inbox_app, name="inbox")
teams_app.add_typer(questions_app, name="questions")
teams_app.add_typer(reminders_app, name="reminders")
teams_app.add_typer(state_app, name="state")
teams_app.add_typer(backoff_app, name="backoff")


@inbox_app.command("ingest")
def inbox_ingest(
    ctx: typer.Context,
    from_: Annotated[Path, typer.Option("--from", help="MCP 検索の結果を書き出した JSON。")],
) -> None:
    """検索結果を state へ取り込み、判断が必要な件を返す。

    「質問かどうか」「回答をどう書くか」の判断は Claude 側が担うため、ここは
    取り込みと状態遷移だけを行い、判断の必要な `pending` を返して終わる。
    """
    cli_ctx = get_context(ctx)
    result = run_ingest(cli_ctx.settings, JsonFileMessageSource(from_), now=datetime.now(UTC))
    cli_ctx.presenter.json_result(result.model_dump(mode="json"))


@inbox_app.command("skip")
def inbox_skip(
    ctx: typer.Context,
    message_id: Annotated[str, typer.Option("--message-id", help="対象の message_id。")],
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="スキップ理由(監査用。人事・金額等の記録に使う)。"),
    ] = None,
) -> None:
    """「質問・依頼ではない」と判定したメッセージを `skipped` へ進める。

    `pending` に出た件は返信するかここを呼ぶかのどちらかを必ず行うこと。どちらも
    しないと `processing` のまま次 tick でも古株として選ばれ続け、新着の質問が
    いつまでも後回しになる(設計 §5.2, §6.1)。人事・評価・金額・契約に関わる
    ため見送った場合は `--reason` に理由を残し、石塚さんへの報告と突き合わせられ
    るようにする。
    """
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    assert settings.teams_state_path is not None  # `_derive_paths` で必ず埋まる
    state = load_state(settings.teams_state_path)
    record = skip_message(state, message_id=message_id, reason=reason)
    save_state(settings.teams_state_path, state)
    cli_ctx.presenter.json_result(record.model_dump(mode="json"))


@teams_app.command("reply")
def reply(
    ctx: typer.Context,
    message_id: Annotated[str, typer.Option("--message-id", help="返信先の message_id。")],
    title: Annotated[str, typer.Option("--title", help="カードの見出し。")],
    body: Annotated[Path, typer.Option("--body", help="本文の Markdown ファイル。")],
    source: Annotated[list[str] | None, typer.Option("--source", help="出典 URL(複数可)。")] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="既に `accepted`/`unknown` でも再送する(二重投稿の恐れあり)。"
        ),
    ] = False,
) -> None:
    """回答を投稿し、state を遷移させる。

    回答の文面を組み立てるのは Claude 側の仕事であり、ここは投稿と state 遷移
    (`sending` → `accepted`/`unknown`)だけを担う。`accepted`/`unknown` への
    再送は `--force` を付けない限り拒否する(設計 §6.1.1)。
    """
    cli_ctx = get_context(ctx)
    with httpx.Client() as client:
        result = run_reply(
            cli_ctx.settings,
            message_id=message_id,
            title=title,
            body=body.read_text(encoding="utf-8"),
            sources=source,
            client=client,
            now=datetime.now(UTC),
            force=force,
        )
    cli_ctx.presenter.json_result(result.model_dump(mode="json"))


@questions_app.command("track")
def questions_track(
    ctx: typer.Context,
    message_id: Annotated[str, typer.Option("--message-id", help="質問と判定した message_id。")],
) -> None:
    """メッセージを追跡対象の質問として登録する。

    「これは質問か」の判定は Claude 側にあるため、登録は明示的な呼び出しで行う。
    `asked_by` / `asked_at` は ingest 済みの `MessageRecord` から引く(呼び出し側に
    同じ値を二度渡させない)。
    """
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    assert settings.teams_state_path is not None  # `_derive_paths` で必ず埋まる
    state = load_state(settings.teams_state_path)
    record = state.messages.get(message_id)
    if record is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"未知の message_id です: {message_id}",
            hint="先に `abist-kb teams inbox ingest` を実行してください。",
        )
    track_question(
        state,
        message_id=message_id,
        asked_by=record.sender_email,
        asked_at=record.created_at,
    )
    save_state(settings.teams_state_path, state)
    cli_ctx.presenter.json_result(state.questions[message_id].model_dump(mode="json"))


@questions_app.command("mark")
def questions_mark(
    ctx: typer.Context,
    message_id: Annotated[str, typer.Option("--message-id", help="対象の message_id。")],
    status: Annotated[QuestionStatus, typer.Option("--status", help="遷移先の状態。")],
) -> None:
    """質問の状態を更新する。

    `resolved` と `acknowledged` の区別は Claude 側の判断であり、ここは記録するだけ。
    `reminded` へ遷移させたときは `reminded_at` も刻む(いつ催促したかが残らないと、
    二重催促を後から検証できない)。
    """
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    assert settings.teams_state_path is not None  # `_derive_paths` で必ず埋まる
    state = load_state(settings.teams_state_path)
    if message_id not in state.questions:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"追跡していない質問です: {message_id}",
            hint="先に `abist-kb teams questions track` を実行してください。",
        )
    mark_question(state, message_id=message_id, status=status, now=datetime.now(UTC))
    save_state(settings.teams_state_path, state)
    cli_ctx.presenter.json_result(state.questions[message_id].model_dump(mode="json"))


@reminders_app.command("due")
def reminders_due(ctx: typer.Context) -> None:
    """営業時間4時間を超えた質問を返す。

    送信するかどうかの最終判断(確証が持てるか)は Claude 側にある(設計 §7.2)。
    ここは閾値超過の抽出だけを行う。
    """
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    assert settings.teams_state_path is not None  # `_derive_paths` で必ず埋まる
    state = load_state(settings.teams_state_path)
    due = due_reminders(
        state,
        now=datetime.now(UTC),
        threshold_hours=settings.teams_reminder_business_hours,
    )
    cli_ctx.presenter.json_result({"due": [q.model_dump(mode="json") for q in due]})


@state_app.command("show")
def state_show(ctx: typer.Context) -> None:
    """現在の state を表示する。"""
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    assert settings.teams_state_path is not None  # `_derive_paths` で必ず埋まる
    cli_ctx.presenter.json_result(load_state(settings.teams_state_path).model_dump(mode="json"))


@backoff_app.command("status")
def backoff_status(ctx: typer.Context) -> None:
    """いま検索してよいかを返す。

    ティックの最初に呼ぶ。`allowed` が false のあいだは検索も投稿もしない。

    `search_since` は `run_ingest` が実際に使う `since` と同じ計算
    (`search_watermark - teams_overlap_minutes`。`search_watermark` が無ければ
    `now - teams_overlap_minutes`)で求める。運用手順が overlap の分数を手で
    書き写すと `Settings.teams_overlap_minutes` を変えたときに乖離するため、
    ここで計算済みの値を返す(設計・運用手順 手順2)。
    """
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    assert settings.teams_state_path is not None  # `_derive_paths` で必ず埋まる
    state = load_state(settings.teams_state_path)
    now = datetime.now(UTC)
    overlap = timedelta(minutes=settings.teams_overlap_minutes)
    search_since = (state.search_watermark or now) - overlap
    cli_ctx.presenter.json_result(
        {
            "allowed": is_allowed(state, now=now),
            "interval_minutes": state.backoff.interval_minutes,
            "next_allowed_at": (
                state.backoff.next_allowed_at.isoformat()
                if state.backoff.next_allowed_at is not None
                else None
            ),
            "search_since": search_since.isoformat(),
        }
    )


@backoff_app.command("hit")
def backoff_hit(
    ctx: typer.Context,
    retry_after: Annotated[
        int | None,
        typer.Option("--retry-after", help="応答の Retry-After(秒)。無ければ省く。"),
    ] = None,
) -> None:
    """429 を受けたことを記録する。

    `Retry-After` があればそれに従い、間隔は据え置く。無ければ間隔を倍にして
    240分で頭打ちにする(設計 §3.2)。20分という初期値は安全と証明された値では
    ないため、記録せずに再開してはならない。
    """
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    assert settings.teams_state_path is not None  # `_derive_paths` で必ず埋まる
    state = load_state(settings.teams_state_path)
    apply_rate_limit(state, now=datetime.now(UTC), retry_after_seconds=retry_after)
    save_state(settings.teams_state_path, state)
    cli_ctx.presenter.json_result(
        {
            "interval_minutes": state.backoff.interval_minutes,
            "next_allowed_at": (
                state.backoff.next_allowed_at.isoformat()
                if state.backoff.next_allowed_at is not None
                else None
            ),
        }
    )


@backoff_app.command("clear")
def backoff_clear(ctx: typer.Context) -> None:
    """検索が成功したので通常間隔へ戻す。"""
    cli_ctx = get_context(ctx)
    settings = cli_ctx.settings
    assert settings.teams_state_path is not None  # `_derive_paths` で必ず埋まる
    state = load_state(settings.teams_state_path)
    clear_rate_limit(state)
    save_state(settings.teams_state_path, state)
    cli_ctx.presenter.json_result(
        {"interval_minutes": state.backoff.interval_minutes, "next_allowed_at": None}
    )


__all__ = ["teams_app"]
