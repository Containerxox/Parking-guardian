"""Relay: recovery of stalled jobs (ADR 0019, "10단계 AWS 인프라 사전 검토", decision of 2026-11-03).

The problem: the database is the source of truth, but a job that is `queued` or `processing` only moves when an
SQS message reaches a worker. If that message is gone (4-day retention, a lost DLQ message, a worker that died
with its message already deleted elsewhere) the job stays open forever and nothing republishes it.

Recovery (OutboxRelay.recover_stalled), all conditional UPDATEs so several relays and live workers cannot
double-handle a job:

    target      queued      COALESCE(published_at, updated_at) <= now - stalled_after   (the last confirmed send =
                                                                  start of the SQS retention clock)
                processing  COALESCE(lease_until, updated_at)  <= now - stalled_after   (the lease ended; a live
                                                                  lease is in the future and never matches)
    never       pending_publish (the relay already owns it), dead_lettered (manual redrive), finished jobs
    inbox gone  the photo is checked first; missing -> rejected / missing_image (same end state the worker gives),
                storage error -> left alone, tried again next pass
    otherwise   pending_publish, next_publish_at = now, claim_attempt + 1 (fences a late worker), lease cleared

stalled_after defaults to 1 day: far above any normal processing (lease 55 s, visibility 60 s), far below the
4-day queue retention and the 7-day inbox retention, and well past the 5-minute deduplication window (a requeue
inside that window would be silently dropped by SQS).

Same harness as test_relay.py. Nothing touches AWS. PostgreSQL-only tests at the end run relays in parallel.
"""
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app import adapters
from app.adapters.inference import InferenceResult
from app.extensions import db
from app.models import DetectionJob, JobStatus, as_utc, utcnow
from app.services.relay import STALLED_AFTER, OutboxRelay

from .test_relay import (GANGNAM, PANGYO, _fail_sends, _jobs, _require_postgres, relay,  # noqa: F401
                         run_relay)
from .test_worker import (Clock, _results, app_overrides, clock, drain, env, send, worker)  # noqa: F401

DAY = timedelta(days=1)


# --- helpers ------------------------------------------------------------------------------------------------

def _row(job_id):
    return db.session.get(DetectionJob, uuid.UUID(str(job_id)))


def _lose_messages(app):
    """The SQS messages are gone (retention, loss): main queue and DLQ both empty."""
    with app.app_context():
        queue = adapters.queue()
        queue._messages.clear()
        queue._dead.clear()
        queue._dedup.clear()          # the 5-minute deduplication records of those sends are long gone, too


def _set(app, job_id, **values):
    with app.app_context():
        db.session.execute(update(DetectionJob).where(DetectionJob.id == uuid.UUID(str(job_id)))
                           .values(**values).execution_options(synchronize_session=False))
        db.session.commit()


def _age_queued(app, clock, job_id, age, whole_job=False):
    """Make the job look `age` old as a queued job: its send was `age` ago on the relay's clock (so SQS has long
    forgotten its deduplication id). `whole_job` also moves created_at back (for the oldest-open-job number)."""
    values = dict(status=JobStatus.QUEUED, published_at=clock() - age, updated_at=clock() - age)
    if whole_job:
        values["created_at"] = clock() - age
    _set(app, job_id, **values)
    with app.app_context():
        adapters.queue()._dedup.pop(str(job_id), None)


def _state(app, job_id):
    with app.app_context():
        row = _row(job_id)
        return {"status": row.status, "attempt": row.claim_attempt, "error": row.last_error,
                "lease": row.lease_until, "machine": row.machine_id, "session": row.session_id, "seq": row.seq,
                "event": row.event, "created": row.created_at}


def _recover(app, relay):
    with app.app_context():
        return relay().recover_stalled()


def _inbox_path(app, job_id):
    with app.app_context():
        return adapters.storage()._path(_row(job_id).s3_key)


# --- the defect, reproduced by each loss scenario ------------------------------------------------------------

def test_without_recovery_a_queued_job_whose_message_expired_stays_open_forever(app, send, worker, relay, clock,
                                                                                 env):
    """Reproduction of the finding. A relay whose recovery threshold is out of reach behaves like the relay before
    this change: relay passes and worker drains change nothing once the message is gone."""
    with app.app_context():
        adapters.queue().retention = timedelta(days=4)
    job_id = send()
    clock.advance(days=5)                      # 4-day retention passed, nobody consumed the message
    assert drain(app, worker) == []            # the queue gave nothing
    with app.app_context():
        old_behaviour = OutboxRelay(clock=clock, stalled_after=timedelta(days=36500))
        for _ in range(3):
            old_behaviour.run_once()
    assert drain(app, worker) == []
    assert _state(app, job_id)["status"] == JobStatus.QUEUED          # stuck: nothing will ever move it
    run_relay(app, relay)                                              # the real relay recovers it
    assert drain(app, worker) == ["done"]


def test_message_still_in_the_queue_gives_two_messages_and_one_result(app, send, worker, relay, clock, env):
    job_id = send()                                           # queued, its message is in the queue
    clock.advance(days=2)                                     # no retention in the fake: the message is still there
    stats = run_relay(app, relay)
    assert stats["recovery"]["requeued"] == 1
    with app.app_context():
        assert adapters.queue().depth() == 2                  # old message + the republished one
    assert drain(app, worker) == ["done", "already"]          # the second message finds the job finished
    with app.app_context():
        assert len(_results()) == 1 and adapters.queue().depth() == 0
    assert _state(app, job_id)["status"] == JobStatus.DONE


def test_message_expired_from_the_queue_the_job_is_republished_and_finishes(app, send, worker, relay, clock, env):
    with app.app_context():
        adapters.queue().retention = timedelta(days=4)
    job_id = send()
    clock.advance(days=5)
    assert drain(app, worker) == []
    stats = run_relay(app, relay)
    assert stats["recovery"] == {"requeued": 1, "rejected": 0, "storage_errors": 0}
    assert drain(app, worker) == ["done"]
    state = _state(app, job_id)
    assert state["status"] == JobStatus.DONE
    with app.app_context():
        assert len(_results()) == 1


def test_worker_stopped_mid_job_and_its_message_expired(app, send, worker, relay, clock, env):
    """Claimed, then the pod vanished; the message came back, was lost with the retention, the lease is long over."""
    with app.app_context():
        adapters.queue().retention = timedelta(days=4)
    job_id = send()
    with app.app_context():
        message = adapters.queue().receive()[0]
        assert worker("dead")._claim(message.job_id) == 1
    assert _state(app, job_id)["status"] == JobStatus.PROCESSING
    clock.advance(days=5)
    assert drain(app, worker) == []
    run_relay(app, relay)
    assert drain(app, worker) == ["done"]
    assert _state(app, job_id)["status"] == JobStatus.DONE


def test_dlq_message_expired_before_the_relay_swept_it(app, send, worker, relay, clock, env):
    """Three failed receives move the message to the DLQ; the relay was down; the DLQ message expired. The job is
    neither dead_lettered nor finished: recovery is the only way out."""
    with app.app_context():
        adapters.queue().retention = timedelta(days=4)
    env.fail_times = 3
    job_id = send()
    for _ in range(3):
        with app.app_context():
            assert worker().run_once() == "retry"
        clock.advance(seconds=61)
    with app.app_context():
        assert adapters.queue().receive() == []               # the 4th receive moves it to the DLQ
        assert len(adapters.queue()._dead) == 1
    clock.advance(days=5)                                     # DLQ retention passes with nobody sweeping
    with app.app_context():
        assert adapters.queue().receive_dead_letters() == []
    assert _state(app, job_id)["status"] == JobStatus.PROCESSING
    env.fail_times = 0                                        # the model works again
    run_relay(app, relay)
    assert drain(app, worker) == ["done"]
    assert _state(app, job_id)["status"] == JobStatus.DONE


# --- what is NOT recovered ----------------------------------------------------------------------------------

def test_recent_or_live_or_foreign_states_are_left_alone(app, send, relay, clock, env):
    young = send(serial=GANGNAM)
    live = send(serial=PANGYO)
    just_over = send(serial=GANGNAM)
    pending = send(serial=PANGYO)
    dead = send(serial=GANGNAM)
    expired_short = send(serial=PANGYO)
    done = send(serial=GANGNAM)
    now = clock()
    _set(app, young, status=JobStatus.QUEUED, published_at=now - DAY + timedelta(minutes=1))
    _set(app, live, status=JobStatus.PROCESSING, lease_until=now + timedelta(seconds=30), claim_attempt=1,
         worker_id="alive", updated_at=now - 3 * DAY)          # old row, but the lease is alive
    _set(app, just_over, status=JobStatus.QUEUED, published_at=now - DAY - timedelta(minutes=1))
    _set(app, pending, status=JobStatus.PENDING_PUBLISH, next_publish_at=now + DAY, created_at=now - 9 * DAY,
         updated_at=now - 9 * DAY)
    _set(app, dead, status=JobStatus.DEAD_LETTERED, published_at=now - 9 * DAY, updated_at=now - 9 * DAY)
    _set(app, expired_short, status=JobStatus.PROCESSING, lease_until=now - timedelta(minutes=5), claim_attempt=1,
         worker_id="slow", updated_at=now - timedelta(minutes=5))
    _set(app, done, status=JobStatus.DONE, published_at=now - 9 * DAY, updated_at=now - 9 * DAY)
    before = {j: _state(app, j) for j in (young, live, pending, dead, expired_short, done)}

    stats = _recover(app, relay)

    assert stats == {"requeued": 1, "rejected": 0, "storage_errors": 0}      # only just_over
    assert _state(app, just_over)["status"] == JobStatus.PENDING_PUBLISH
    for job_id, state in before.items():
        assert _state(app, job_id) == state, job_id


def test_a_worker_that_claims_between_the_selection_and_the_update_keeps_its_job(app, send, worker, relay, clock,
                                                                                  env, monkeypatch):
    """The relay picked the job as stalled, then a worker claimed it before the relay's UPDATE: the conditional
    UPDATE must not match, so the live claim survives."""
    job_id = send()
    _age_queued(app, clock, job_id, 2 * DAY)
    claimed = {}
    with app.app_context():
        storage = adapters.storage()
        real = storage.exists

        def exists(key):
            if not claimed:
                claimed["attempt"] = worker("live")._claim(uuid.UUID(str(job_id)))
            return real(key)

        monkeypatch.setattr(storage, "exists", exists)
    stats = _recover(app, relay)
    assert stats["requeued"] == 0
    state = _state(app, job_id)
    assert state["status"] == JobStatus.PROCESSING and state["attempt"] == claimed["attempt"] == 1


# --- fencing, order, session ---------------------------------------------------------------------------------

def test_a_late_worker_result_is_dropped_after_the_job_was_recovered(app, send, worker, relay, clock, env):
    job_id = send()
    with app.app_context():
        message = adapters.queue().receive()[0]
        late = worker("late")
        attempt = late._claim(message.job_id)
    assert attempt == 1
    clock.advance(days=2)                                     # its lease ended long ago; nobody finished the job
    assert _recover(app, relay)["requeued"] == 1
    state = _state(app, job_id)
    assert state["status"] == JobStatus.PENDING_PUBLISH and state["attempt"] == 2 and state["lease"] is None

    result = InferenceResult(has_window=True, window={"coords": [0, 0, 1, 1], "confidence": 0.9}, stickers=[],
                             backend="late", elapsed_ms=1)
    with app.app_context():
        assert late._apply(uuid.UUID(str(job_id)), attempt, result) is None      # status is no longer 'processing'
        assert _results() == []

    with app.app_context():                                   # a new worker takes it; the old attempt stays dead
        newer = worker("newer")
        new_attempt = newer._claim(uuid.UUID(str(job_id)))
        assert new_attempt == 3
        assert late._apply(uuid.UUID(str(job_id)), attempt, result) is None
        assert _results() == []


def test_recovery_changes_only_status_attempt_and_schedule(app, send, relay, clock, env):
    """Machine, session, seq, event and creation time are the job's identity for ordering: recovery never edits them,
    and the machine's other jobs are not touched."""
    session = uuid.uuid4()
    first = send(session=session, seq=1, event="arrived", verdict="permitted")
    second = send(session=session, seq=2, event="refresh", verdict="permitted")
    third = send(session=session, seq=3, event="refresh", verdict="permitted")
    other = send(serial=PANGYO, verdict="permitted")
    with app.app_context():
        queue = adapters.queue()
        queue._messages[:] = [m for m in queue._messages if m.job_id_text != str(first)]
    _age_queued(app, clock, first, 2 * DAY)
    before = _state(app, first)

    assert _recover(app, relay)["requeued"] == 1

    after = _state(app, first)
    for key in ("machine", "session", "seq", "event", "created"):
        assert after[key] == before[key]
    for untouched in (second, third, other):
        assert _state(app, untouched)["status"] == JobStatus.QUEUED


def test_recovered_old_job_is_judged_stale_behind_newer_applied_jobs(app, send, worker, relay, clock, env):
    session = uuid.uuid4()
    first = send(session=session, seq=1, event="arrived", verdict="permitted")
    second = send(session=session, seq=2, event="refresh", verdict="permitted")
    third = send(session=session, seq=3, event="refresh", verdict="permitted")
    with app.app_context():
        queue = adapters.queue()
        queue._messages[:] = [m for m in queue._messages if m.job_id_text != str(first)]
    _age_queued(app, clock, first, 2 * DAY)
    assert _recover(app, relay)["requeued"] == 1
    run_relay(app, relay, "publish_pending")
    assert drain(app, worker) == ["done", "done", "stale"]    # seq 2, seq 3 applied; seq 1 is behind last_applied_seq
    with app.app_context():
        assert [_row(j).status for j in (first, second, third)] == [JobStatus.STALE, JobStatus.DONE, JobStatus.DONE]
        assert [(r.seq, r.applied) for r in sorted(_results(), key=lambda r: r.seq)] == [(1, False), (2, True),
                                                                                         (3, True)]


def test_republish_order_of_recovered_jobs_follows_the_machine_order(app, send, relay, clock, env):
    session = uuid.uuid4()
    ids = [send(session=session, seq=n, event="arrived" if n == 1 else "refresh", verdict="permitted")
           for n in (1, 2, 3)]
    _lose_messages(app)
    for job_id in ids:
        _age_queued(app, clock, job_id, 2 * DAY)
    assert _recover(app, relay)["requeued"] == 3
    run_relay(app, relay, "publish_pending")
    with app.app_context():
        assert [m.job_id_text for m in adapters.queue()._messages] == [str(j) for j in ids]


# --- inbox photo already deleted (7-day retention) ------------------------------------------------------------

def test_a_stalled_job_whose_inbox_photo_is_gone_is_rejected_not_republished(app, send, worker, relay, clock, env):
    job_id = send()
    _lose_messages(app)
    _age_queued(app, clock, job_id, 8 * DAY)
    _inbox_path(app, job_id).unlink()

    stats = _recover(app, relay)

    assert stats == {"requeued": 0, "rejected": 1, "storage_errors": 0}
    state = _state(app, job_id)
    assert state["status"] == JobStatus.REJECTED and state["error"].startswith("missing_image") and state["lease"] is None
    run_relay(app, relay, "publish_pending")
    with app.app_context():
        assert adapters.queue().depth() == 0 and _results() == []         # nothing sent, nothing judged
    assert _recover(app, relay)["rejected"] == 0                          # final state: not touched again


def test_a_stalled_processing_job_without_its_photo_is_rejected_too(app, send, relay, clock, env):
    job_id = send()
    _lose_messages(app)
    _set(app, job_id, status=JobStatus.PROCESSING, claim_attempt=1, worker_id="dead",
         lease_until=clock() - 3 * DAY, updated_at=clock() - 3 * DAY)
    _inbox_path(app, job_id).unlink()
    assert _recover(app, relay)["rejected"] == 1
    assert _state(app, job_id)["status"] == JobStatus.REJECTED


def test_a_storage_error_leaves_the_job_for_the_next_pass(app, send, worker, relay, clock, env, monkeypatch):
    job_id = send()
    _lose_messages(app)
    _age_queued(app, clock, job_id, 2 * DAY)
    with app.app_context():
        storage = adapters.storage()
        real = storage.exists

        def broken(key):
            raise OSError("storage unavailable")

        monkeypatch.setattr(storage, "exists", broken)
        stats = relay().recover_stalled()
        assert stats == {"requeued": 0, "rejected": 0, "storage_errors": 1}
        assert _row(job_id).status == JobStatus.QUEUED
        monkeypatch.setattr(storage, "exists", real)
    assert _recover(app, relay)["requeued"] == 1
    run_relay(app, relay, "publish_pending")
    assert drain(app, worker) == ["done"]


# --- coexistence with the manual redrive and the DLQ sweep -----------------------------------------------------

def test_dead_lettered_jobs_are_never_recovered_and_manual_redrive_still_works(app, send, worker, relay, clock, env):
    from app.services.relay import redrive

    job_id = send()
    _lose_messages(app)
    _set(app, job_id, status=JobStatus.DEAD_LETTERED, published_at=clock() - 9 * DAY, updated_at=clock() - 9 * DAY,
         last_error="dlq: received 3 time(s)")
    assert _recover(app, relay) == {"requeued": 0, "rejected": 0, "storage_errors": 0}
    assert _state(app, job_id)["status"] == JobStatus.DEAD_LETTERED
    with app.app_context():
        assert redrive([uuid.UUID(str(job_id))]) == 1
    clock.advance(minutes=6)
    run_relay(app, relay, "publish_pending")
    assert drain(app, worker) == ["done"]


def test_a_late_dlq_message_for_a_recovered_job_ends_in_dead_lettered_which_redrive_can_resend(app, send, worker,
                                                                                             relay, clock, env):
    from app.adapters.queue import _Stored
    from app.services.relay import redrive

    job_id = send()
    _lose_messages(app)
    _age_queued(app, clock, job_id, 2 * DAY)
    assert _recover(app, relay)["requeued"] == 1
    run_relay(app, relay, "publish_pending")                  # queued again with a new message
    with app.app_context():                                   # the original DLQ message turns up after all
        queue = adapters.queue()
        queue._dead.append(_Stored(str(job_id), '{"job_id": "%s"}' % job_id, "m-1", "late-dlq", receive_count=3,
                                   enqueued_at=clock()))
    clock.advance(seconds=31)
    stats = run_relay(app, relay, "sweep_dead_letters")
    assert stats["dead_lettered"] == 1
    assert _state(app, job_id)["status"] == JobStatus.DEAD_LETTERED
    with app.app_context():
        assert redrive([uuid.UUID(str(job_id))]) == 1         # explicit, operator-driven way out


def test_a_job_that_always_fails_ends_dead_lettered_not_in_a_recovery_loop(app, send, worker, relay, clock, env):
    env.fail_times = 1000
    job_id = send()
    _lose_messages(app)
    _age_queued(app, clock, job_id, 2 * DAY)
    assert _recover(app, relay)["requeued"] == 1
    run_relay(app, relay, "publish_pending")
    for _ in range(3):
        with app.app_context():
            assert worker().run_once() == "retry"
        clock.advance(seconds=61)
    with app.app_context():
        assert adapters.queue().receive() == []               # moved to the DLQ
    clock.advance(seconds=31)
    assert run_relay(app, relay, "sweep_dead_letters")["dead_lettered"] == 1
    assert _state(app, job_id)["status"] == JobStatus.DEAD_LETTERED
    assert _recover(app, relay)["requeued"] == 0              # a final state for recovery


# --- run_once, numbers for operations, settings -----------------------------------------------------------------

def test_run_once_reports_recovery_stalled_and_oldest_open_job(app, send, relay, clock, env, monkeypatch):
    job_id = send()
    _lose_messages(app)
    _age_queued(app, clock, job_id, 3 * DAY, whole_job=True)
    with app.app_context():
        monkeypatch.setattr(adapters.storage(), "exists", lambda key: (_ for _ in ()).throw(OSError("down")))
    stats = run_relay(app, relay)
    assert stats["recovery"]["storage_errors"] == 1
    assert stats["stalled_jobs"] == 1
    assert stats["oldest_open_job_age_seconds"] >= 3 * 86400 - 5


def test_run_once_without_open_jobs_reports_zero(app, relay, clock, env):
    stats = run_relay(app, relay)
    assert stats["recovery"] == {"requeued": 0, "rejected": 0, "storage_errors": 0}
    assert stats["stalled_jobs"] == 0 and stats["oldest_open_job_age_seconds"] == 0


def test_stalled_after_defaults_to_one_day_and_cannot_fall_inside_the_dedup_window(app):
    assert STALLED_AFTER == DAY
    with app.app_context():
        assert OutboxRelay().stalled_after == DAY
        assert OutboxRelay(stalled_after=timedelta(minutes=10)).stalled_after == timedelta(minutes=10)
        with pytest.raises(ValueError):
            OutboxRelay(stalled_after=timedelta(minutes=9))
        app.config["RELAY_STALLED_AFTER_SECONDS"] = 7200
        assert OutboxRelay().stalled_after == timedelta(hours=2)
        app.config["RELAY_STALLED_AFTER_SECONDS"] = 60
        with pytest.raises(ValueError):
            OutboxRelay()


# --- PostgreSQL: several relays recover at once -----------------------------------------------------------------

def test_postgres_parallel_relays_recover_each_stalled_job_exactly_once(app, send, worker, relay, clock, env):
    _require_postgres(app)
    ids = []
    for serial in (GANGNAM, PANGYO):
        session = uuid.uuid4()
        for seq in range(1, 7):
            ids.append(send(serial, session=session, seq=seq, event="arrived" if seq == 1 else "refresh",
                            verdict="permitted"))
    _lose_messages(app)
    for job_id in ids:
        _age_queued(app, clock, job_id, 2 * DAY)
    totals, barrier = [], threading.Barrier(4)

    def run():
        with app.app_context():
            barrier.wait()
            totals.append(OutboxRelay(clock=clock).recover_stalled())

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert sum(t["requeued"] for t in totals) == len(ids)                 # every job requeued by exactly one relay
    assert sum(t["storage_errors"] + t["rejected"] for t in totals) == 0
    with app.app_context():
        assert {j.claim_attempt for j in _jobs()} == {1}                  # bumped once each
        assert {j.status for j in _jobs()} == {JobStatus.PENDING_PUBLISH}
    for _ in range(4):
        run_relay(app, relay, "publish_pending")
    outcomes = drain(app, worker, limit=100)
    assert set(outcomes) <= {"done", "stale"} and len(outcomes) == len(ids)
    with app.app_context():
        assert {j.status for j in _jobs()} <= {JobStatus.DONE, JobStatus.STALE}
        assert len(_results()) == len(ids)                                # one result per job: nothing judged twice


def test_postgres_recovery_racing_a_live_worker_never_takes_a_claimed_job(app, send, worker, relay, clock, env):
    _require_postgres(app)
    ids = [send(verdict="permitted", session=uuid.uuid4()) for _ in range(8)]
    for job_id in ids:
        _age_queued(app, clock, job_id, 2 * DAY)
    claims, barrier = [], threading.Barrier(2)

    def claimer():
        with app.app_context():
            w = worker("live")
            barrier.wait()
            for job_id in ids:
                attempt = w._claim(uuid.UUID(str(job_id)))
                claims.append((str(job_id), attempt))

    def recoverer():
        with app.app_context():
            barrier.wait()
            relay().recover_stalled()

    threads = [threading.Thread(target=claimer), threading.Thread(target=recoverer)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    with app.app_context():
        assert len(claims) == len(ids)
        for job_id, attempt in claims:
            row = _row(job_id)
            # attempt 1: the worker claimed first, the relay's conditional UPDATE found no stalled job.
            # attempt 2: the relay requeued first (attempt 1), the worker then claimed the pending job (attempt 2).
            # Either way the worker's claim is the one on the row: nothing was lost or claimed twice.
            assert attempt in (1, 2)
            assert row.status == JobStatus.PROCESSING and row.claim_attempt == attempt
