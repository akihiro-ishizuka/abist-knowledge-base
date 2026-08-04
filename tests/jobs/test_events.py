"""`ProgressEvent` の永続化(`job_events`)とインプロセス購読(`EventBus`)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from abist_kb.domain.job import ProgressEvent, Severity
from abist_kb.infrastructure.db.connection import connect
from abist_kb.infrastructure.jobs import events as events_mod
from abist_kb.infrastructure.jobs.db import ensure_jobs_schema
from abist_kb.infrastructure.jobs.repository import JobRepository


@pytest.fixture
def conn(tmp_root: Path):
    c = connect(tmp_root / "jobs.sqlite")
    ensure_jobs_schema(c)
    yield c
    c.close()


def test_append_event_assigns_monotonic_seq_per_job(conn) -> None:
    job = JobRepository(conn).submit("noop")
    seq1 = events_mod.append_event(conn, ProgressEvent(job_id=job.id, phase="p1", message="1"))
    seq2 = events_mod.append_event(conn, ProgressEvent(job_id=job.id, phase="p2", message="2"))
    assert (seq1, seq2) == (1, 2)


def test_append_event_seq_is_independent_per_job(conn) -> None:
    repo = JobRepository(conn)
    job_a = repo.submit("noop")
    job_b = repo.submit("noop")
    events_mod.append_event(conn, ProgressEvent(job_id=job_a.id, phase="p"))
    seq_b = events_mod.append_event(conn, ProgressEvent(job_id=job_b.id, phase="p"))
    assert seq_b == 1


def test_list_events_returns_in_seq_order(conn) -> None:
    job = JobRepository(conn).submit("noop")
    events_mod.append_event(conn, ProgressEvent(job_id=job.id, phase="a", message="first"))
    events_mod.append_event(
        conn, ProgressEvent(job_id=job.id, phase="b", message="second", severity=Severity.WARNING)
    )
    history = events_mod.list_events(conn, job.id)
    assert [e.message for e in history] == ["first", "second"]
    assert history[1].severity is Severity.WARNING


def test_event_bus_publishes_to_subscribers() -> None:
    bus = events_mod.EventBus()
    received: list[ProgressEvent] = []
    bus.subscribe(received.append)
    event = ProgressEvent(job_id="j1", phase="p")
    bus.publish(event)
    assert received == [event]


def test_event_bus_unsubscribe_stops_delivery() -> None:
    bus = events_mod.EventBus()
    received: list[ProgressEvent] = []
    unsubscribe = bus.subscribe(received.append)
    unsubscribe()
    bus.publish(ProgressEvent(job_id="j1", phase="p"))
    assert received == []
