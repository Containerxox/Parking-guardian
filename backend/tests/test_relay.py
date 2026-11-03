"""Phase 2 outbox relay (ADR 0019 decision 3; design 6.5, 6.7, 6.11, 6.12; decisions of 2026-11-02).

    publish   pending_publish jobs whose next_publish_at has come, per machine in ingest order; stop a machine at
              its first job that is not due or fails (order); backoff 1, 2, 4 ... 60 s, never give up
    DLQ       known job -> dead_lettered (+ reason), then delete the DLQ message; finished job -> just delete;
              unreadable / unknown job -> keep in the DLQ (hold it out of sight), log identifiers only
    effects   done jobs whose evidence copy / notification is still pending -> redo only the missing part
    retention commands 30 d -> finished jobs 30 d -> unreferenced sessions 30 d -> results 90 d
    redrive   flask jobs redrive: dead_lettered -> pending_publish, not before the SQS deduplication window ends

Same harness as test_worker.py (real ingest, in-memory queue on a test clock, scripted inference). Nothing
touches AWS. PostgreSQL-only tests at the end run relays in parallel.
"""
import hashlib
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app import adapters
from app.adapters.queue import QueueError, _Stored
from app.extensions import db
from app.models import (CommandStatus, CommandType, DetectionJob, DetectionResult, DeviceCommand, DeviceSession,
                        HistoryAction, ImageStatus, JobStatus, Machine, Notification, Violation, ViolationHistory,
                        as_utc, utcnow)
from app.services.relay import OutboxRelay

from .test_worker import (GANGNAM, PANGYO, Clock, _machine, _message, _new_violations, _results,  # noqa: F401
                          _take_messages, app_overrides, clock, drain, env, send, worker)


@pytest.fixture
def relay(app, clock):
    def make():
        return OutboxRelay(clock=clock, batch_size=50)

    return make


def run_relay(app, relay, step="run_once"):
    with app.app_context():
        return getattr(relay(), step)()


def _jobs():
    return db.session.scalars(select(DetectionJob).order_by(DetectionJob.created_at)).all()


def _queued_order(app):
    """Job ids in the order the queue holds them (without consuming)."""
    with app.app_context():
        return [m.job_id_text for m in adapters.queue()._messages]


class SendCounter:
    """Wrap the queue's send to count real sends per job (deduplication would hide repeats)."""

    def __init__(self, queue):
        self.queue, self.calls, self.lock = queue, [], threading.Lock()
        self.real = queue.send

    def __call__(self, job_id, machine_id, fast=False):
        with self.lock:
            self.calls.append(str(job_id))
        return self.real(job_id, machine_id, fast=fast)


def _fail_sends(app, times):
    with app.app_context():
        queue = adapters.queue()
    real, state = queue.send, {"left": times}

    def send(job_id, machine_id, fast=False):
        if state["left"]:
            state["left"] -= 1
            raise QueueError("send", "ServiceUnavailable", "injected", True)
        return real(job_id, machine_id, fast=fast)

    queue.send = send
    return lambda: setattr(queue, "send", real)


# --- publish ----------------------------------------------------------------------------------------------

def test_relay_publishes_a_job_the_ingest_could_not_send_once_it_is_due(app, send, relay, clock):
    restore = _fail_sends(app, 1)
    job_id = send()
    restore()
    assert run_relay(app, relay, "publish_pending") == 0          # backoff 1 s not over yet
    clock.advance(seconds=2)
    assert run_relay(app, relay, "publish_pending") == 1
    with app.app_context():
        [job] = _jobs()
        assert job.status == JobStatus.QUEUED and job.publish_attempts == 2 and job.last_error is None
    assert _queued_order(app) == [job_id]


def test_backoff_doubles_up_to_60_seconds_and_never_gives_up(app, send, relay, clock):
    restore = _fail_sends(app, 100)
    send()
    waits = []
    for _ in range(9):
        clock.advance(seconds=61)
        run_relay(app, relay, "publish_pending")
        with app.app_context():
            job = _jobs()[0]
            waits.append(round((as_utc(job.next_publish_at) - clock()).total_seconds()))
    restore()
    assert waits == [2, 4, 8, 16, 32, 60, 60, 60, 60]   # attempt 1 (ingest) waited 1 s
    with app.app_context():
        job = _jobs()[0]
        assert job.status == JobStatus.PENDING_PUBLISH and job.publish_attempts == 10
        assert job.last_error == "send: ServiceUnavailable"
    clock.advance(seconds=61)
    assert run_relay(app, relay, "publish_pending") == 1


def test_jobs_of_one_machine_go_out_in_ingest_order(app, send, relay, clock):
    a = uuid.uuid4()
    restore = _fail_sends(app, 1)
    first = send(session=a, seq=1)                       # send fails -> pending
    second = send(session=a, seq=2, event="confirm")     # held back behind it (design 6.5)
    third = send(session=a, seq=3, event="refresh")
    restore()
    other = send(PANGYO)                                 # another machine goes straight out
    clock.advance(seconds=2)
    assert run_relay(app, relay, "publish_pending") == 3
    assert _queued_order(app) == [other, first, second, third]


def test_a_failed_send_stops_that_machine_for_this_round(app, send, relay, clock):
    a = uuid.uuid4()
    restore = _fail_sends(app, 1)
    first = send(session=a, seq=1)
    second = send(session=a, seq=2, event="confirm")
    restore()
    clock.advance(seconds=2)
    restore = _fail_sends(app, 1)                        # the relay's first send fails again
    assert run_relay(app, relay, "publish_pending") == 0
    restore()
    assert _queued_order(app) == []                      # second was NOT sent ahead of first
    clock.advance(seconds=3)
    run_relay(app, relay, "publish_pending")
    assert _queued_order(app) == [first, second]


def test_a_not_yet_due_older_job_holds_back_a_due_newer_one(app, send, relay, clock):
    a = uuid.uuid4()
    restore = _fail_sends(app, 1)
    first = send(session=a, seq=1)
    second = send(session=a, seq=2, event="confirm")
    restore()
    with app.app_context():   # first is in a long backoff, second is due
        db.session.execute(update(DetectionJob).where(DetectionJob.id == uuid.UUID(first))
                           .values(next_publish_at=clock() + timedelta(seconds=30)))
        db.session.execute(update(DetectionJob).where(DetectionJob.id == uuid.UUID(second))
                           .values(next_publish_at=clock() - timedelta(seconds=1)))
        db.session.commit()
    assert run_relay(app, relay, "publish_pending") == 0
    clock.advance(seconds=31)
    run_relay(app, relay, "publish_pending")
    assert _queued_order(app) == [first, second]


def test_sent_but_not_marked_queued_is_deduplicated_within_five_minutes(app, send, relay, clock, monkeypatch):
    from app.services import relay as relay_module

    restore = _fail_sends(app, 1)
    send()
    restore()
    clock.advance(seconds=2)
    monkeypatch.setattr(relay_module, "_mark_queued", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    run_relay(app, relay, "publish_pending")             # the message went out, the row still says pending
    monkeypatch.undo()
    with app.app_context():
        assert _jobs()[0].status == JobStatus.PENDING_PUBLISH and adapters.queue().depth() == 1
    clock.advance(seconds=10)
    run_relay(app, relay, "publish_pending")             # resent within 5 min: SQS drops the duplicate
    with app.app_context():
        assert _jobs()[0].status == JobStatus.QUEUED and adapters.queue().depth() == 1


def test_sent_but_not_marked_queued_after_five_minutes_is_applied_once(app, send, relay, worker, clock,
                                                                       monkeypatch):
    from app.services import relay as relay_module

    restore = _fail_sends(app, 1)
    send()
    restore()
    clock.advance(seconds=2)
    monkeypatch.setattr(relay_module, "_mark_queued", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    run_relay(app, relay, "publish_pending")
    monkeypatch.undo()
    clock.advance(minutes=6)                              # outside the dedup window: a second message
    run_relay(app, relay, "publish_pending")
    with app.app_context():
        assert adapters.queue().depth() == 2
    assert sorted(drain(app, worker)) == ["already", "done"]
    with app.app_context():
        assert len(_results()) == 1 and adapters.queue().depth() == 0


def test_relay_dies_between_send_and_commit_then_recovers(app, send, relay, clock, monkeypatch):
    """Same state as a failed 'queued' update: the next relay round finishes the bookkeeping."""
    from app.services import relay as relay_module

    restore = _fail_sends(app, 1)
    send()
    restore()
    clock.advance(seconds=2)

    def die(*args, **kwargs):
        raise SystemExit("relay killed")

    monkeypatch.setattr(relay_module, "_mark_queued", die)
    with pytest.raises(SystemExit):
        run_relay(app, relay, "publish_pending")
    monkeypatch.undo()
    with app.app_context():
        db.session.rollback()
    clock.advance(seconds=5)
    run_relay(app, relay, "publish_pending")
    with app.app_context():
        assert _jobs()[0].status == JobStatus.QUEUED and adapters.queue().depth() == 1


def test_old_pending_publish_is_reported(app, send, relay, clock, caplog):
    restore = _fail_sends(app, 100)
    send()
    clock.advance(minutes=6)
    with app.app_context():
        stats = OutboxRelay(clock=clock).run_once()
    restore()
    assert stats["oldest_pending_publish_seconds"] >= 360
    assert "pending_publish older than" in caplog.text


# --- DLQ ----------------------------------------------------------------------------------------------------

def _to_dlq(app, send, worker, clock, env, **kwargs):
    """A job whose inference fails three times ends in the DLQ (maxReceiveCount 3)."""
    job_id = send(**kwargs)
    env.fail_times = 3
    for _ in range(3):
        drain(app, worker)
        clock.advance(seconds=61)
    with app.app_context():
        adapters.queue().receive()      # triggers the redrive into the DLQ
    return job_id


def test_dlq_message_marks_the_job_dead_lettered_and_is_then_deleted(app, send, worker, relay, clock, env):
    job_id = _to_dlq(app, send, worker, clock, env)
    stats = run_relay(app, relay, "sweep_dead_letters")
    assert stats == {"dead_lettered": 1, "already_finished": 0, "kept": 0, "busy": 0}
    with app.app_context():
        job = db.session.get(DetectionJob, uuid.UUID(job_id))
        assert job.status == JobStatus.DEAD_LETTERED and job.last_error.startswith("dlq:")
        assert adapters.queue().receive_dead_letters() == []


def test_relay_dies_after_marking_dead_lettered_before_deleting(app, send, worker, relay, clock, env, monkeypatch):
    job_id = _to_dlq(app, send, worker, clock, env)
    with app.app_context():
        queue = adapters.queue()
    real_delete = queue.delete_dead_letter
    queue.delete_dead_letter = lambda receipt: (_ for _ in ()).throw(QueueError("delete_dead_letter", "Gone", "", True))
    run_relay(app, relay, "sweep_dead_letters")
    queue.delete_dead_letter = real_delete
    clock.advance(seconds=31)   # the undeleted DLQ message is visible again
    assert run_relay(app, relay, "sweep_dead_letters")["already_finished"] == 1
    with app.app_context():
        assert db.session.get(DetectionJob, uuid.UUID(job_id)).status == JobStatus.DEAD_LETTERED
        assert adapters.queue().receive_dead_letters() == []


def test_dlq_message_of_a_finished_job_is_only_deleted(app, send, worker, relay, clock):
    """A done job's message can reach the DLQ when its deletes kept failing; the job must stay done."""
    job_id = send()
    drain(app, worker)
    with app.app_context():
        adapters.queue()._dead.append(_Stored(job_id, f'{{"job_id": "{job_id}"}}', "m-1", "late-copy"))
    stats = run_relay(app, relay, "sweep_dead_letters")
    assert stats["already_finished"] == 1
    with app.app_context():
        assert db.session.get(DetectionJob, uuid.UUID(job_id)).status == JobStatus.DONE
        assert adapters.queue().receive_dead_letters() == []


def test_dlq_message_of_a_job_still_being_judged_waits(app, send, worker, relay, clock):
    job_id = uuid.UUID(send())
    _take_messages(app)
    with app.app_context():
        worker()._claim(job_id)                          # a worker holds a valid lease right now
        adapters.queue()._dead.append(_Stored(str(job_id), f'{{"job_id": "{job_id}"}}', "m-1", "racing"))
    assert run_relay(app, relay, "sweep_dead_letters")["busy"] == 1
    with app.app_context():
        assert db.session.get(DetectionJob, job_id).status == JobStatus.PROCESSING
        assert adapters.queue()._dead                    # kept for the next sweep


def test_unusable_dlq_messages_are_kept_and_traced(app, env, relay, clock, caplog):
    unknown = str(uuid.uuid4())
    with app.app_context():
        dead = adapters.queue()._dead
        dead.append(_Stored("x", "not json", "m-1", "bad-body"))
        dead.append(_Stored(unknown, f'{{"job_id": "{unknown}"}}', "m-2", "no-such-job"))
    stats = run_relay(app, relay, "sweep_dead_letters")
    assert stats["kept"] == 2
    assert "bad-body" in caplog.text and "no-such-job" in caplog.text
    assert hashlib.sha256(b"not json").hexdigest()[:16] in caplog.text   # traceable without logging the body
    assert "not json" not in caplog.text
    with app.app_context():
        assert len(adapters.queue()._dead) == 2           # preserved in the DLQ
    caplog.clear()
    assert run_relay(app, relay, "sweep_dead_letters")["kept"] == 0   # held out of sight, not re-logged
    clock.advance(hours=1, seconds=1)
    assert run_relay(app, relay, "sweep_dead_letters")["kept"] == 2


def test_redrive_waits_out_the_deduplication_window_then_judges(app, send, worker, relay, clock, env):
    job_id = _to_dlq(app, send, worker, clock, env)
    run_relay(app, relay, "sweep_dead_letters")
    result = app.test_cli_runner().invoke(args=["jobs", "redrive", job_id])
    assert result.exit_code == 0 and "1 job" in result.output
    with app.app_context():
        job = db.session.get(DetectionJob, uuid.UUID(job_id))
        assert job.status == JobStatus.PENDING_PUBLISH
        # never inside SQS's 5-minute deduplication window of the last send (a resend there would vanish)
        assert as_utc(job.next_publish_at) >= as_utc(job.published_at) + timedelta(minutes=5)
    clock.advance(minutes=5)
    assert run_relay(app, relay, "publish_pending") == 1
    assert drain(app, worker) == ["done"]


def test_redrive_all_dead(app, send, worker, relay, clock, env):
    _to_dlq(app, send, worker, clock, env)
    run_relay(app, relay, "sweep_dead_letters")
    result = app.test_cli_runner().invoke(args=["jobs", "redrive", "--all-dead"])
    assert result.exit_code == 0 and "1 job" in result.output
    result = app.test_cli_runner().invoke(args=["jobs", "redrive", str(uuid.uuid4())])
    assert result.exit_code != 0


# --- effects ----------------------------------------------------------------------------------------------

def _violation_with_failing_storage(app, send, worker, monkeypatch, method="save"):
    a = uuid.uuid4()
    send(session=a, seq=1)
    send(session=a, seq=2, event="confirm")
    with app.app_context():
        storage = adapters.storage()

    def broken(*args, **kwargs):
        raise OSError("bucket unavailable")

    monkeypatch.setattr(storage, method, broken)
    drain(app, worker)
    monkeypatch.undo()


def test_failed_evidence_copy_is_retried_by_the_relay_without_a_new_violation(app, send, worker, relay, clock,
                                                                             monkeypatch):
    _violation_with_failing_storage(app, send, worker, monkeypatch)
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert violation.image_status == ImageStatus.FAILED
        job = db.session.scalars(select(DetectionJob).where(DetectionJob.seq == 2)).one()
        assert job.effects_status == "pending"            # kept for a retry, not failed after one attempt
        notifications = db.session.scalar(select(func.count()).select_from(Notification))
    assert run_relay(app, relay, "retry_effects") == 0   # not before 5 minutes
    clock.advance(minutes=6)
    assert run_relay(app, relay, "retry_effects") == 1
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert violation.image_status == ImageStatus.UPLOADED
        assert db.session.scalars(select(DetectionJob.effects_status).where(DetectionJob.seq == 2)).one() == "done"
        assert db.session.scalar(select(func.count()).select_from(Notification)) == notifications
        assert len(_results()) == 2


def test_failed_notification_is_retried_without_copying_the_photo_again(app, send, worker, relay, clock,
                                                                        monkeypatch):
    a = uuid.uuid4()
    send(session=a, seq=1)
    send(session=a, seq=2, event="confirm")
    with app.app_context():
        publisher = adapters.publisher()
    monkeypatch.setattr(publisher, "publish_violation_detected",
                        lambda vid: (_ for _ in ()).throw(RuntimeError("smtp down")))
    drain(app, worker)
    monkeypatch.undo()
    with app.app_context():
        storage = adapters.storage()
    saves = []
    real_save = storage.save
    monkeypatch.setattr(storage, "save", lambda key, data: (saves.append(key), real_save(key, data)))
    clock.advance(minutes=6)
    assert run_relay(app, relay, "retry_effects") == 1
    assert saves == []                                    # evidence was already uploaded
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert db.session.scalar(select(func.count()).select_from(Notification).where(
            Notification.violation_id == violation.id)) >= 1
        assert db.session.scalars(select(DetectionJob.effects_status).where(DetectionJob.seq == 2)).one() == "done"


def test_effects_give_up_after_three_failed_attempts(app, send, worker, relay, clock, monkeypatch):
    _violation_with_failing_storage(app, send, worker, monkeypatch)
    with app.app_context():
        storage = adapters.storage()
    monkeypatch.setattr(storage, "save", lambda key, data: (_ for _ in ()).throw(OSError("still down")))
    for _ in range(2):
        clock.advance(minutes=6)
        run_relay(app, relay, "retry_effects")
    with app.app_context():
        job = db.session.scalars(select(DetectionJob).where(DetectionJob.seq == 2)).one()
        assert job.effects_status == "failed"
        [violation] = _new_violations(_machine().id)
        failures = db.session.scalar(select(func.count()).select_from(ViolationHistory).where(
            ViolationHistory.violation_id == violation.id,
            ViolationHistory.action == HistoryAction.IMAGE_UPLOAD_FAILED))
        assert failures == 3
    clock.advance(minutes=6)
    assert run_relay(app, relay, "retry_effects") == 0


def test_relay_dies_during_effects_and_the_next_sweep_finishes(app, send, worker, relay, clock, monkeypatch):
    from app.services import worker as worker_module

    _violation_with_failing_storage(app, send, worker, monkeypatch)
    real = worker_module.JudgementWorker._run_effects
    monkeypatch.setattr(worker_module.JudgementWorker, "_run_effects",
                        lambda self, job_id: (_ for _ in ()).throw(SystemExit("relay killed")))
    clock.advance(minutes=6)
    with pytest.raises(SystemExit):
        run_relay(app, relay, "retry_effects")
    monkeypatch.setattr(worker_module.JudgementWorker, "_run_effects", real)
    with app.app_context():
        db.session.rollback()
    clock.advance(minutes=6)
    assert run_relay(app, relay, "retry_effects") == 1
    with app.app_context():
        assert db.session.scalars(select(DetectionJob.effects_status).where(DetectionJob.seq == 2)).one() == "done"
        assert len(_new_violations(_machine().id)) == 1


# --- retention (design 6.12) --------------------------------------------------------------------------------

def _age(app, model, days, **where):
    with app.app_context():
        columns = {c for c in ("created_at", "updated_at", "first_received_at") if hasattr(model, c)}
        values = {c: utcnow() - timedelta(days=days) for c in columns}
        statement = update(model).values(**values)
        for name, value in where.items():
            statement = statement.where(getattr(model, name) == value)
        db.session.execute(statement)
        db.session.commit()


def test_retention_keeps_references_valid_and_in_flight_work(app, send, worker, relay, clock, env):
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    send(session=a, seq=1)
    send(session=a, seq=2, event="confirm")
    send(session=a, seq=3, event="left", verdict="novehicle")
    drain(app, worker)
    _to_dlq(app, send, worker, clock, env, session=b, seq=1)
    run_relay(app, relay, "sweep_dead_letters")
    send(session=c, seq=1, verdict="novehicle")           # C becomes the machine's current session
    drain(app, worker)
    restore = _fail_sends(app, 1)
    send(PANGYO)                                          # in flight (pending_publish), also old
    restore()
    with app.app_context():
        machine = _machine()
        db.session.add(DeviceCommand(machine_id=machine.id, session_id=a, type=CommandType.CAPTURE_CONFIRM,
                                     due_at=utcnow(), expires_at=utcnow(), status=CommandStatus.EXPIRED))
        db.session.commit()
    for model in (DetectionJob, DeviceSession, DeviceCommand):
        _age(app, model, 31)
    _age(app, DetectionResult, 31)

    stats = run_relay(app, relay, "clean_up")
    assert stats == {"commands": 1, "jobs": 4, "sessions": 1, "results": 0}
    with app.app_context():
        statuses = sorted(j.status for j in _jobs())
        assert statuses == [JobStatus.DEAD_LETTERED, JobStatus.PENDING_PUBLISH]
        results = _results()
        assert len(results) == 4 and all(r.job_id is None for r in results)   # trace kept, job gone
        assert [r.violation_id is not None for r in results] == [True, True, True, False]
        # A is gone; B is kept (its dead_lettered job), C is the current session, PANGYO's has a pending job
        assert db.session.get(DeviceSession, a) is None
        assert db.session.get(DeviceSession, b) is not None and db.session.get(DeviceSession, c) is not None
        assert _machine().current_session_id == c

    _age(app, DetectionResult, 91)
    assert run_relay(app, relay, "clean_up")["results"] == 4
    with app.app_context():
        assert _results() == [] and len(_new_violations(_machine().id)) == 1   # violations are never deleted


def test_retention_never_deletes_a_machines_current_session_or_pending_effects(app, send, worker, relay, clock,
                                                                              monkeypatch):
    _violation_with_failing_storage(app, send, worker, monkeypatch)    # effects pending
    for model in (DetectionJob, DeviceSession, DeviceCommand):
        _age(app, model, 31)
    stats = run_relay(app, relay, "clean_up")
    with app.app_context():
        machine = _machine()
        assert db.session.get(DeviceSession, machine.current_session_id) is not None
        pending = db.session.scalars(select(DetectionJob).where(DetectionJob.effects_status == "pending")).all()
        assert len(pending) == 1
    assert stats["sessions"] == 0 and stats["jobs"] == 1   # seq 1 (no effects) goes, seq 2 waits for effects


# --- lease 55 s versus visibility 60 s ----------------------------------------------------------------------

def test_slow_worker_after_redelivery_loses_its_claim(app, send, worker, clock, env):
    """t0 receive+claim (lease to t0+55); inference takes 61 s; at t0+60 the message is visible again and w2
    claims attempt 2. w1's late result is dropped by fencing, w2's is applied: exactly one result."""
    send()
    with app.app_context():
        [message] = adapters.queue().receive()
        w1 = worker("w1")
        attempt = w1._claim(message.job_id)
    clock.advance(seconds=60)
    assert drain(app, worker) == ["done"]                 # w2 (drain's worker)
    with app.app_context():
        assert w1._apply(message.job_id, attempt, env.analyze(b"\xff\xd8\xff\xe0violation-")) is None
        assert len(_results()) == 1 and db.session.get(DetectionJob, message.job_id).claim_attempt == 2


def test_worker_finishing_between_lease_end_and_redelivery_still_applies(app, send, worker, clock, env):
    """Lease expired at t0+55 but nobody re-claimed: w1 finishing at t0+57 applies and deletes normally."""
    send()
    with app.app_context():
        [message] = adapters.queue().receive()
        w1 = worker("w1")
        attempt = w1._claim(message.job_id)               # lease until t0+55
        clock.advance(seconds=57)                         # inference took 57 s
        assert w1._apply(message.job_id, attempt, env.analyze(b"\xff\xd8\xff\xe0violation-")).status == "done"
        w1._delete(message)                               # receipt still valid until t0+60
        assert adapters.queue().depth() == 0
    clock.advance(seconds=10)
    assert drain(app, worker) == []
    with app.app_context():
        assert len(_results()) == 1


def test_worker_finishing_after_redelivery_but_before_the_reclaim(app, send, worker, clock, env):
    """t0+60 w2 receives the message again but has not claimed yet; w1 then applies (its claim is still the
    current one). w2's claim finds the job done -> 'already' and deletes with its own receipt; w1's delete of
    the stale receipt only logs."""
    send()
    with app.app_context():
        [first] = adapters.queue().receive()
        w1 = worker("w1")
        attempt = w1._claim(first.job_id)
        clock.advance(seconds=61)
        [second] = adapters.queue().receive()
        assert w1._apply(first.job_id, attempt, env.analyze(b"\xff\xd8\xff\xe0violation-")).status == "done"
        w1._delete(first)                                 # stale receipt: logged, no error
        assert worker("w2").process(second) == "already"
        assert adapters.queue().depth() == 0 and len(_results()) == 1


# --- PostgreSQL: several relays at once -------------------------------------------------------------------

def _require_postgres(app):
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("PostgreSQL only (set TEST_DATABASE_URL)")


def test_postgres_parallel_relays_send_each_job_once_and_in_order(app, send, relay, clock):
    _require_postgres(app)
    restore = _fail_sends(app, 100)
    jobs = {GANGNAM: [], PANGYO: []}
    for serial in (GANGNAM, PANGYO):
        session = uuid.uuid4()
        for seq in range(1, 6):
            jobs[serial].append(send(serial, session=session, seq=seq, event="refresh" if seq > 1 else "arrived"))
    restore()
    with app.app_context():
        queue = adapters.queue()
    counter = SendCounter(queue)
    queue.send = counter
    clock.advance(seconds=2)
    results, barrier = [], threading.Barrier(4)

    def run():
        with app.app_context():
            barrier.wait()
            results.append(OutboxRelay(clock=clock).publish_pending())

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    for _ in range(3):   # a machine skipped because another relay held it is picked up by a later round
        run_relay(app, relay, "publish_pending")
    queue.send = counter.real
    assert sorted(counter.calls) == sorted(jobs[GANGNAM] + jobs[PANGYO])   # each job sent exactly once
    order = _queued_order(app)
    for serial in (GANGNAM, PANGYO):
        assert [j for j in order if j in jobs[serial]] == jobs[serial]
    with app.app_context():
        assert {j.status for j in _jobs()} == {JobStatus.QUEUED}


def test_postgres_relay_effects_retry_and_worker_redelivery_do_not_double_up(app, send, worker, relay, clock,
                                                                           monkeypatch):
    _require_postgres(app)
    _violation_with_failing_storage(app, send, worker, monkeypatch)
    clock.advance(minutes=6)
    counts, barrier = [], threading.Barrier(3)

    def run():
        with app.app_context():
            barrier.wait()
            counts.append(OutboxRelay(clock=clock).retry_effects())

    threads = [threading.Thread(target=run) for _ in range(3)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(counts) == [0, 0, 1]                    # one relay won the effects claim
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert violation.image_status == ImageStatus.UPLOADED
        uploads_failed = db.session.scalar(select(func.count()).select_from(ViolationHistory).where(
            ViolationHistory.violation_id == violation.id,
            ViolationHistory.action == HistoryAction.IMAGE_UPLOAD_FAILED))
        assert uploads_failed == 1
