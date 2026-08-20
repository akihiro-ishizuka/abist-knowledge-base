"""429 バックオフ(設計 §3.2)。

20分は初期値であって安全と証明された値ではない。`Retry-After` があれば従い、
無ければ指数バックオフ(上限240分)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from abist_kb.application.chat_watch.state import (
    WatchState,
    apply_rate_limit,
    clear_rate_limit,
    is_allowed,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def test_retry_after_is_honoured() -> None:
    state = WatchState()

    next_at = apply_rate_limit(state, now=NOW, retry_after_seconds=90)

    assert next_at == NOW + timedelta(seconds=90)
    assert state.backoff.next_allowed_at == next_at


def test_exponential_backoff_without_retry_after() -> None:
    state = WatchState()

    apply_rate_limit(state, now=NOW, retry_after_seconds=None)
    assert state.backoff.interval_minutes == 40

    apply_rate_limit(state, now=NOW, retry_after_seconds=None)
    assert state.backoff.interval_minutes == 80

    apply_rate_limit(state, now=NOW, retry_after_seconds=None)
    assert state.backoff.interval_minutes == 160

    apply_rate_limit(state, now=NOW, retry_after_seconds=None)
    assert state.backoff.interval_minutes == 240

    apply_rate_limit(state, now=NOW, retry_after_seconds=None)
    assert state.backoff.interval_minutes == 240


def test_clear_returns_to_normal_interval() -> None:
    state = WatchState()
    apply_rate_limit(state, now=NOW, retry_after_seconds=None)

    clear_rate_limit(state)

    assert state.backoff.interval_minutes == 20
    assert state.backoff.next_allowed_at is None


def test_is_allowed_respects_next_allowed_at() -> None:
    state = WatchState()
    apply_rate_limit(state, now=NOW, retry_after_seconds=600)

    assert is_allowed(state, now=NOW + timedelta(minutes=5)) is False
    assert is_allowed(state, now=NOW + timedelta(minutes=10)) is True


def test_is_allowed_when_never_rate_limited() -> None:
    assert is_allowed(WatchState(), now=NOW) is True
