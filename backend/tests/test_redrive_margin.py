"""Redrive: a 60-second margin behind SQS's 5-minute deduplication window (ADR 0019, "10단계 AWS 인프라 사전 검토",
decision of 2026-11-03).

The defect (reproduced in Compose with ElasticMQ, faults3b.py): `flask jobs redrive` scheduled the next send at
`last send + 5 min`, taking the last send from `published_at`. The relay stamps `published_at` with the START of its
pass (`now`), but the SQS send happens later (earlier sends of the same batch, a slow call). A resend exactly 5 min
after that stamp can still be inside the window that started at the real send; SQS drops it silently, the relay marks
the job queued, and no message exists.

    next_publish_at = max(now, last_send + 5 min + 60 s)

What 60 s guarantees: any stamp-to-send lag below 60 s. What it does not: a lag of 60 s or more (a relay pass of many
slow sends), or a stamp that is not the relay's. The stalled-job recovery (1 day) is the net for those.

Same harness as test_relay.py. Nothing touches AWS. PostgreSQL-only tests at the end run relays in parallel.
"""
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import update

from app import adapters
from app.extensions import db
from app.models import DetectionJob, JobStatus, as_utc, utcnow
from app.services.relay import DEDUPLICATION_WINDOW, REDRIVE_MARGIN, OutboxRelay, redrive

from .test_relay import (GANGNAM, SendCounter, _fail_sends, _jobs, _require_postgres, relay,  # noqa: F401
                         run_relay)
from .test_worker import (Clock, _results, app_overrides, clock, drain, env, send, worker)  # noqa: F401

MARGIN = timedelta(seconds=60)


# --- helpers ------------------------------------------------------------------------------------------------

def _row(job_id):
    return db.session.get(DetectionJob, uuid.UUID(str(job_id)))


def _set(app, job_id, **values):
    with app.app_context():
        db.session.execute(update(DetectionJob).where(DetectionJob.id == uuid.UUID(str(job_id)))
                           .values(**values).execution_options(synchronize_session=False))
        db.session.commit()


def _go_to(clock, moment):
    """Move the test clock so that clock() == moment (never backwards)."""
    delta = moment - clock()
    if delta > timedelta(0):
        clock.advance(seconds=delta.total_seconds())


def _published_by_relay_with_latency(app, send, relay, clock, latency=2.0):
    """The job reaches the queue through the RELAY (ingest's send failed), and the real send happens `latency`
    seconds after the relay stamped the pass start (the stamp becomes published_at)."""
    restore = _fail_sends(app, 1)
    job_id = send()
    restore()
    clock.advance(seconds=5)                       # past the ingest's first backoff
    with app.app_context():
        queue = adapters.queue()
    real = queue.send

    def slow_send(job, machine, fast=False):
        clock.advance(seconds=latency)             # earlier sends of the batch / a slow call
        return real(job, machine, fast=fast)

    queue.send = slow_send
    assert run_relay(app, relay, "publish_pending") == 1
    queue.send = real
    return job_id


def _dead_letter(app, job_id):
    """What the DLQ sweep leaves behind: the message is gone from the main queue, the job is dead_lettered. SQS
    keeps its deduplication record of the send (it is not tied to the message)."""
    with app.app_context():
        adapters.queue()._messages.clear()
    _set(app, job_id, status=JobStatus.DEAD_LETTERED, lease_until=None, last_error="dlq: received 3 time(s)")


def _redrive(app, ids):
    with app.app_context():
        return redrive([uuid.UUID(str(i)) for i in ids])


def _next_publish(app, job_id):
    with app.app_context():
        return as_utc(_row(job_id).next_publish_at)


def _published(app, job_id):
    with app.app_context():
        return as_utc(_row(job_id).published_at)


# --- the reproduction ---------------------------------------------------------------------------------------

def test_redriven_job_whose_last_stamp_precedes_its_real_send_is_not_dropped(app, send, worker, relay, clock, env):
    """Before the fix this stalls: published_at = pass start T, real send T+2 s, redrive sends at T+300 s = 298 s after
    the real send, inside the window; the queue drops it and the job sits in 'queued' with no message."""
    job_id = _published_by_relay_with_latency(app, send, relay, clock)
    stamp = _published(app, job_id)
    _dead_letter(app, job_id)
    assert _redrive(app, [job_id]) == 1

    due = _next_publish(app, job_id)
    assert due >= stamp + DEDUPLICATION_WINDOW + MARGIN
    _go_to(clock, due + timedelta(milliseconds=1))        # the earliest moment the relay may send
    assert run_relay(app, relay, "publish_pending") == 1
    with app.app_context():
        assert adapters.queue().depth() == 1              # the message really is in the queue
    assert drain(app, worker) == ["done"]
    with app.app_context():
        assert _row(job_id).status == JobStatus.DONE and len(_results()) == 1


def test_the_margin_constant_is_sixty_seconds_on_top_of_the_window():
    assert REDRIVE_MARGIN == MARGIN and DEDUPLICATION_WINDOW == timedelta(minutes=5)


# --- the schedule around the boundary --------------------------------------------------------------------------

@pytest.mark.parametrize("since_send, wait_more", [
    (timedelta(minutes=4, seconds=59), timedelta(minutes=1, seconds=1)),     # just before the old boundary
    (timedelta(minutes=5, seconds=1), timedelta(seconds=59)),                # just after it: still inside the margin
    (timedelta(minutes=5, seconds=59), timedelta(seconds=1)),
    (timedelta(minutes=6), timedelta(0)),                                    # exactly at 5 min + 60 s: due now
    (timedelta(minutes=6, seconds=1), timedelta(0)),
    (timedelta(minutes=10), timedelta(0)),                                   # long after: no extra wait
    (timedelta(days=2), timedelta(0)),
])
def test_next_publish_is_last_send_plus_five_minutes_plus_sixty_seconds_or_now(app, send, since_send, wait_more):
    job_id = send()
    _set(app, job_id, published_at=utcnow() - since_send)
    _dead_letter(app, job_id)
    before = utcnow()
    assert _redrive(app, [job_id]) == 1
    wait = (_next_publish(app, job_id) - before).total_seconds()
    assert abs(wait - wait_more.total_seconds()) < 2.0, (since_send, wait)


def test_the_relay_waits_until_the_margin_is_over(app, send, relay, clock, env):
    job_id = _published_by_relay_with_latency(app, send, relay, clock)
    stamp = _published(app, job_id)
    _dead_letter(app, job_id)
    _redrive(app, [job_id])
    _go_to(clock, stamp + timedelta(minutes=5, seconds=1))        # the old rule would send here
    assert run_relay(app, relay, "publish_pending") == 0
    _go_to(clock, stamp + timedelta(minutes=5, seconds=59))
    assert run_relay(app, relay, "publish_pending") == 0
    _go_to(clock, stamp + timedelta(minutes=6, seconds=1))
    assert run_relay(app, relay, "publish_pending") == 1


def test_a_job_without_any_send_record_uses_its_creation_time(app, send):
    job_id = send()
    _set(app, job_id, published_at=None, created_at=utcnow() - timedelta(minutes=1))
    _dead_letter(app, job_id)
    before = utcnow()
    _redrive(app, [job_id])
    wait = (_next_publish(app, job_id) - before).total_seconds()
    assert 5 * 60 - 2 <= wait <= 5 * 60 + 2                         # created 1 min ago -> 5 more minutes


# --- duplicates, restart, CLI ----------------------------------------------------------------------------------

def test_a_redriven_job_whose_old_message_still_exists_is_judged_once(app, send, worker, relay, clock, env):
    job_id = send()                                               # queued, its message M1 is in the queue
    _set(app, job_id, status=JobStatus.DEAD_LETTERED, last_error="dlq: received 3 time(s)")   # M1 not removed
    assert _redrive(app, [job_id]) == 1
    clock.advance(minutes=6, seconds=5)
    assert run_relay(app, relay, "publish_pending") == 1
    with app.app_context():
        assert adapters.queue().depth() == 2
    assert drain(app, worker) == ["done", "already"]
    with app.app_context():
        assert len(_results()) == 1 and adapters.queue().depth() == 0


def test_a_new_relay_after_a_restart_follows_the_stored_schedule(app, send, worker, relay, clock, env):
    job_id = _published_by_relay_with_latency(app, send, relay, clock)
    stamp = _published(app, job_id)
    _dead_letter(app, job_id)
    _redrive(app, [job_id])
    _go_to(clock, stamp + timedelta(minutes=5, seconds=30))
    for _ in range(3):                                            # three fresh relays (restarts), none may send yet
        with app.app_context():
            assert OutboxRelay(clock=clock).publish_pending() == 0
    _go_to(clock, stamp + timedelta(minutes=6, seconds=2))
    with app.app_context():
        assert OutboxRelay(clock=clock).publish_pending() == 1    # the schedule lived in the database
        assert OutboxRelay(clock=clock).publish_pending() == 0    # and is not sent twice
    assert drain(app, worker) == ["done"]


def test_cli_redrive_twice_schedules_once(app, send):
    job_id = send()
    _dead_letter(app, job_id)
    first = app.test_cli_runner().invoke(args=["jobs", "redrive", job_id])
    assert first.exit_code == 0 and "1 job" in first.output
    scheduled = _next_publish(app, job_id)
    second = app.test_cli_runner().invoke(args=["jobs", "redrive", job_id])
    assert "0 job" in second.output
    assert _next_publish(app, job_id) == scheduled


# --- order and the other mechanisms ---------------------------------------------------------------------------------

def test_redriven_jobs_of_one_machine_are_published_in_ingest_order(app, send, worker, relay, clock, env):
    session = uuid.uuid4()
    ids = [send(session=session, seq=n, event="arrived" if n == 1 else "refresh", verdict="permitted")
           for n in (1, 2, 3)]
    for job_id in ids:
        _dead_letter(app, job_id)
    assert _redrive(app, ids) == 3
    clock.advance(minutes=6, seconds=5)
    assert run_relay(app, relay, "publish_pending") == 3
    with app.app_context():
        assert [m.job_id_text for m in adapters.queue()._messages] == [str(j) for j in ids]
    assert drain(app, worker) == ["done", "done", "done"]


def test_a_pending_newer_job_waits_behind_the_redriven_older_one_and_keeps_the_order(app, send, worker, relay,
                                                                                    clock, env):
    """Strict per-machine order is unchanged: the machine stops at its first job that is not due, so the newer job
    (seq 2) is not published ahead of the redriven seq 1 while the margin runs; both then go out in ingest order."""
    session = uuid.uuid4()
    first = send(session=session, seq=1, event="arrived", verdict="permitted")
    second = send(session=session, seq=2, event="refresh", verdict="permitted")
    _dead_letter(app, first)                                       # also clears the queue (seq 2's message too)
    _set(app, second, status=JobStatus.PENDING_PUBLISH, next_publish_at=utcnow(), published_at=None)
    with app.app_context():
        adapters.queue()._dedup.clear()
    assert _redrive(app, [first]) == 1
    clock.advance(minutes=1)
    assert run_relay(app, relay, "publish_pending") == 0           # blocked behind the older, not-yet-due job
    clock.advance(minutes=5, seconds=5)
    assert run_relay(app, relay, "publish_pending") == 2
    with app.app_context():
        assert [m.job_id_text for m in adapters.queue()._messages] == [str(first), str(second)]
    assert drain(app, worker) == ["done", "done"]                  # applied in seq order: nothing stale


def test_a_redriven_message_that_was_dropped_anyway_is_caught_by_the_stalled_job_recovery(app, send, worker, relay,
                                                                                          clock, env):
    """The margin is not a proof (a stamp-to-send lag of a minute or more is still possible). If a resend is lost
    the job sits 'queued' without a message; the 1-day recovery republishes it."""
    job_id = _published_by_relay_with_latency(app, send, relay, clock, latency=0)
    _dead_letter(app, job_id)
    _redrive(app, [job_id])
    clock.advance(minutes=6, seconds=5)
    assert run_relay(app, relay, "publish_pending") == 1
    with app.app_context():
        adapters.queue()._messages.clear()                         # the resend vanished
        adapters.queue()._dedup.clear()
    assert drain(app, worker) == []
    clock.advance(days=2)
    stats = run_relay(app, relay)
    assert stats["recovery"]["requeued"] == 1
    assert drain(app, worker) == ["done"]


# --- PostgreSQL: several relays and two redrives at once ---------------------------------------------------------------

def test_postgres_two_simultaneous_redrives_schedule_each_job_once(app, send):
    _require_postgres(app)
    ids = [send(session=uuid.uuid4()) for _ in range(6)]
    for job_id in ids:
        _dead_letter(app, job_id)
    counts, barrier = [], threading.Barrier(2)

    def run():
        with app.app_context():
            barrier.wait()
            counts.append(redrive([uuid.UUID(i) for i in ids]))

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert sum(counts) == len(ids)                                 # every job taken by exactly one of the two
    with app.app_context():
        assert {j.status for j in _jobs()} == {JobStatus.PENDING_PUBLISH}


def test_postgres_parallel_relays_send_each_redriven_job_once_at_the_scheduled_time(app, send, worker, relay, clock,
                                                                                    env):
    _require_postgres(app)
    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    ids = [send(session=s, seq=n, event="arrived" if n == 1 else "refresh", verdict="permitted")
           for s in (session_a, session_b) for n in (1, 2, 3)]
    for job_id in ids:
        _dead_letter(app, job_id)
    assert _redrive(app, ids) == len(ids)
    with app.app_context():
        queue = adapters.queue()
    counter = SendCounter(queue)
    queue.send = counter
    clock.advance(minutes=6, seconds=5)
    barrier = threading.Barrier(4)

    def run():
        with app.app_context():
            barrier.wait()
            OutboxRelay(clock=clock).publish_pending()

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    for _ in range(3):
        run_relay(app, relay, "publish_pending")
    queue.send = counter.real
    assert sorted(counter.calls) == sorted(ids)                    # exactly one send per job
    outcomes = drain(app, worker, limit=50)
    assert set(outcomes) <= {"done", "stale"} and len(outcomes) == len(ids)
    with app.app_context():
        assert len(_results()) == len(ids)
