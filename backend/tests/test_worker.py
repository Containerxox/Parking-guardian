"""Phase 2 judgement worker (ADR 0019 decisions 5-8; design 6.5, 6.8, 6.11, 6.13; decisions of 2026-11-02).

    receive -> claim (conditional UPDATE, claim_attempt fencing) -> read inbox photo -> inference OUTSIDE any
    transaction -> one transaction: lock machine, re-check the claim, order guard, record_verdict(commit=False),
    result row (+ trace, violation links), job done/stale, confirm command -> commit -> effects (evidence copy,
    notification) -> delete message

Photos travel through the real ingest endpoint into the in-memory queue and local inbox; inference is scripted
from the photo bytes. Nothing touches AWS. PostgreSQL-only tests at the end run real concurrent workers.
"""
import io
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app import adapters
from app.adapters.inference import InferenceError, InferenceResult, InvalidImageError
from app.adapters.queue import FakeJobQueue, QueueMessage
from app.cli import dev_api_key
from app.extensions import db
from app.models import (CommandStatus, DetectionJob, DetectionResult, DeviceCommand, EndReason, HistoryAction,
                        ImageStatus, JobStatus, Machine, Notification, Violation, ViolationHistory, as_utc, utcnow)
from app.services.worker import JudgementWorker

from .conftest import API

GANGNAM = "RPI-A-GANGNAM-01"
PANGYO = "RPI-A-PANGYO-01"
JPEG = b"\xff\xd8\xff\xe0"
PHOTO = {verdict: JPEG + verdict.encode() + b"-" for verdict in ("violation", "permitted", "novehicle", "fail",
                                                                 "broken")}


@pytest.fixture
def app_overrides():
    return {"QUEUE_BACKEND": "fake", "VIOLATION_CONFIRM_COUNT": 2, "VEHICLE_CLEAR_COUNT": 3,
            "EPISODE_GAP_MINUTES": 10}


class Clock:
    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self):
        return utcnow() + self.offset

    def advance(self, **kwargs):
        self.offset += timedelta(**kwargs)


class ScriptedInference:
    """Verdict from the photo bytes: ...violation- / ...permitted- / ...novehicle-; ...fail- raises InferenceError
    (transient), ...broken- raises InvalidImageError (permanent)."""

    name = "scripted"
    state = "ready"

    def __init__(self):
        self.calls = 0
        self.fail_times = 0   # make the next N analyses fail with InferenceError

    def warm_up(self):
        pass

    def analyze(self, image_bytes, filename=""):
        self.calls += 1
        if self.fail_times:
            self.fail_times -= 1
            raise InferenceError("model crashed")
        if b"broken" in image_bytes:
            raise InvalidImageError("cannot decode")
        if b"fail" in image_bytes:
            raise InferenceError("model crashed")
        window = {"coords": [0, 0, 100, 100], "confidence": 0.9}
        if b"novehicle" in image_bytes:
            return InferenceResult(has_window=False, backend=self.name, elapsed_ms=5)
        stickers = [{"coords": [1, 1, 2, 2], "confidence": 0.8}] if b"permitted" in image_bytes else []
        return InferenceResult(has_window=True, window=window, stickers=stickers, backend=self.name, elapsed_ms=7)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def env(app, clock):
    """Queue on the test clock, scripted inference."""
    inference = ScriptedInference()
    app.extensions["pg_inference"] = inference
    app.extensions["pg_queue"] = FakeJobQueue(clock=clock, visibility_timeout=60, max_receive_count=3)
    return inference


@pytest.fixture
def send(client, env):
    def post(serial=GANGNAM, event="arrived", session=None, seq=1, verdict="violation", event_id=None, **extra):
        session = session or uuid.uuid4()
        form = {"file": (io.BytesIO(PHOTO[verdict] + uuid.uuid4().hex.encode()), "p.jpg"), "event": event,
                "event_id": str(event_id or uuid.uuid4()), "session_id": str(session), "seq": str(seq),
                **{k: str(v) for k, v in extra.items()}}
        response = client.post(f"{API}/device/detection-jobs", data=form, content_type="multipart/form-data",
                               headers={"X-Device-Key": dev_api_key(serial)})
        assert response.status_code == 202, response.get_json()
        return response.get_json()["job_id"]

    return post


@pytest.fixture
def worker(app, clock):
    def make(worker_id="w1"):
        return JudgementWorker(worker_id=worker_id, clock=clock, lease_seconds=55)

    return make


def drain(app, worker, limit=50):
    """Run the worker until the queue gives nothing; returns the outcome of every message."""
    outcomes = []
    with app.app_context():
        w = worker()
        for _ in range(limit):
            outcome = w.run_once()
            if outcome is None:
                break
            outcomes.append(outcome)
    return outcomes


def _machine(serial=GANGNAM):
    return db.session.scalars(select(Machine).where(Machine.serial_number == serial)).one()


def _new_violations(machine_id):
    return db.session.scalars(select(Violation).where(Violation.machine_id == machine_id,
                                                      Violation.created_at >= utcnow() - timedelta(hours=1))
                              .order_by(Violation.id)).all()


def _results():
    return db.session.scalars(select(DetectionResult).order_by(DetectionResult.received_at,
                                                               DetectionResult.id)).all()


def _message(job_id, receipt="manual"):
    """A queue message built by hand: lets a test deliver jobs in an order SQS would not (redrive, reorder)."""
    return QueueMessage(job_id=uuid.UUID(str(job_id)), receipt_handle=receipt, receive_count=1,
                        message_id="manual", group_id=None, body="")


def _take_messages(app):
    """Empty the queue without processing, so a test can deliver the jobs itself in any order."""
    with app.app_context():
        queue = adapters.queue()
        while queue.receive():
            pass
        queue._messages.clear()


# --- the backlog scenario that motivated the guard ownership change -----------------------------------------

def test_two_sessions_queued_behind_each_other_give_two_violations(app, send, worker):
    a, b = uuid.uuid4(), uuid.uuid4()
    send(session=a, seq=1, event="arrived")
    send(session=a, seq=2, event="confirm")
    send(session=a, seq=3, event="left", verdict="novehicle")
    send(session=b, seq=1, event="arrived")      # A is already ended (its left), so nothing is superseded
    send(session=b, seq=2, event="confirm")

    assert drain(app, worker) == ["done"] * 5
    with app.app_context():
        machine = _machine()
        first, second = _new_violations(machine.id)
        assert first.end_reason == EndReason.VEHICLE_LEFT and second.ended_at is None
        results = _results()
        assert [r.applied for r in results] == [True] * 5
        assert [r.violation_id for r in results] == [first.id, first.id, first.id, second.id, second.id]
        assert machine.current_session_id == b and machine.last_applied_seq == 2
        assert {j.status for j in db.session.scalars(select(DetectionJob))} == {JobStatus.DONE}


def test_session_superseded_at_ingest_still_has_its_queued_jobs_judged(app, send, worker):
    """Device reboot: A's arrived is still queued when B starts (A superseded at ingest). A's verdict counts,
    and with B's sighting it confirms one violation linked to both results (counts continue, design 6.10)."""
    a, b = uuid.uuid4(), uuid.uuid4()
    send(session=a, seq=1)
    send(session=b, seq=1)
    assert drain(app, worker) == ["done", "done"]
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert violation.detection_count == 2
        assert [r.violation_id for r in _results()] == [violation.id, violation.id]
        # A was superseded at ingest, so no confirm command was created for it
        assert db.session.scalar(select(func.count()).select_from(DeviceCommand)) == 0


def test_left_received_before_the_arrived_was_judged(app, send, worker):
    """The session is already ended when the worker judges its arrived: the verdict still counts, but no
    confirm command is created for a car that has left."""
    a = uuid.uuid4()
    send(session=a, seq=1)
    send(session=a, seq=2, event="left", verdict="novehicle")
    assert drain(app, worker) == ["done", "done"]
    with app.app_context():
        assert db.session.scalar(select(func.count()).select_from(DeviceCommand)) == 0
        assert _new_violations(_machine().id) == []
        machine = _machine()
        assert machine.pending_violation_count == 0 and machine.last_applied_seq == 2


def test_pending_verdict_of_an_active_session_creates_a_confirm_command(app, send, worker):
    a = uuid.uuid4()
    job_id = send(session=a, seq=1)
    drain(app, worker)
    with app.app_context():
        [command] = db.session.scalars(select(DeviceCommand)).all()
        job = db.session.get(DetectionJob, uuid.UUID(job_id))
        assert command.status == CommandStatus.PENDING and command.session_id == a
        assert as_utc(command.due_at) == as_utc(job.received_at) + timedelta(seconds=60)
        assert as_utc(command.expires_at) == as_utc(command.due_at) + timedelta(minutes=9)
        assert command.source_job_id == job.id


# --- order guard: reverse order and stale ----------------------------------------------------------------

def test_lower_seq_after_a_higher_one_in_the_same_session_is_stale(app, send, worker):
    a = uuid.uuid4()
    first = send(session=a, seq=1)
    second = send(session=a, seq=2, event="refresh", verdict="novehicle")
    _take_messages(app)
    with app.app_context():
        w = worker()
        assert w.process(_message(second)) == "done"
        assert w.process(_message(first)) == "stale"
        stale = db.session.get(DetectionJob, uuid.UUID(first))
        result = db.session.get(DetectionResult, stale.result_id)
        assert stale.status == JobStatus.STALE and result.applied is False and result.violation_id is None
        assert result.verdict == "violation"            # the verdict is kept for audit
        machine = _machine()
        assert machine.pending_violation_count == 0 and machine.last_applied_seq == 2


def test_older_session_after_a_newer_one_is_stale_by_first_received_at(app, send, worker):
    """Redrive can deliver an old session's job after a newer session was applied. The guard compares when the
    server first saw each session, not the delivery order."""
    a, b = uuid.uuid4(), uuid.uuid4()
    old = send(session=a, seq=5, event="refresh")
    new = send(session=b, seq=1)
    _take_messages(app)
    with app.app_context():
        w = worker()
        assert w.process(_message(new)) == "done"
        assert w.process(_message(old)) == "stale"
        assert _machine().current_session_id == b and _machine().last_applied_seq == 1


def test_a_stale_left_does_not_close_the_current_episode(app, send, worker):
    a, b = uuid.uuid4(), uuid.uuid4()
    send(session=a, seq=1)
    left_a = send(session=a, seq=2, event="left", verdict="novehicle")
    send(session=b, seq=1)
    send(session=b, seq=2, event="confirm")
    _take_messages(app)
    with app.app_context():
        jobs = db.session.scalars(select(DetectionJob).order_by(DetectionJob.created_at)).all()
        w = worker()
        for job in (jobs[0], jobs[2], jobs[3]):       # A.arrived, B.arrived, B.confirm -> violation open
            w.process(_message(job.id))
        assert w.process(_message(left_a)) == "stale"
        [violation] = _new_violations(_machine().id)
        assert violation.ended_at is None


# --- SIGNAL_LOST ------------------------------------------------------------------------------------------

def _silence(app, minutes):
    """Pretend the device has been silent: move its last photo/heartbeat into the past."""
    with app.app_context():
        machine = _machine()
        machine.last_detection_at = machine.last_heartbeat_at = utcnow() - timedelta(minutes=minutes)
        db.session.commit()


def _age_episode(app, minutes):
    with app.app_context():
        for violation in _new_violations(_machine().id):
            violation.last_seen_at = utcnow() - timedelta(minutes=minutes)
        db.session.commit()


def test_job_with_a_silence_before_it_ends_the_episode_as_signal_lost(app, send, worker):
    a = uuid.uuid4()
    send(session=a, seq=1)
    send(session=a, seq=2, event="confirm")
    drain(app, worker)
    _age_episode(app, 12)
    _silence(app, 12)
    send(session=a, seq=3, event="refresh")     # snapshot at ingest: 12 silent minutes
    assert drain(app, worker) == ["done"]
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert violation.end_reason == EndReason.SIGNAL_LOST
        refresh = _results()[-1]
        assert refresh.applied is True and refresh.violation_id is None   # starts a new count, not the old episode


def test_known_limit_a_stale_job_carrying_the_silence_misses_signal_lost(app, send, worker):
    """ADR 0019 change record (2026-11-02): if the job that carries the silence is applied after a newer one
    (redrive), it is stale and the episode is not closed. This test pins the documented limitation."""
    a = uuid.uuid4()
    send(session=a, seq=1)
    send(session=a, seq=2, event="confirm")
    drain(app, worker)
    _age_episode(app, 12)
    _silence(app, 12)
    gap_job = send(session=a, seq=3, event="refresh")      # carries the 12-minute silence
    after = send(session=a, seq=4, event="refresh")        # snapshot = gap_job's receipt: no silence
    _take_messages(app)
    with app.app_context():
        w = worker()
        assert w.process(_message(after)) == "done"
        assert w.process(_message(gap_job)) == "stale"
        [violation] = _new_violations(_machine().id)
        assert violation.ended_at is None                   # missed (known limitation)


# --- duplicates, crashes, failures --------------------------------------------------------------------------

def test_the_same_message_twice_applies_once(app, send, worker):
    job_id = send()
    _take_messages(app)
    with app.app_context():
        w = worker()
        assert w.process(_message(job_id)) == "done"
        assert w.process(_message(job_id)) == "already"
        assert len(_results()) == 1 and _machine().pending_violation_count == 1


def test_worker_dies_during_inference_and_another_takes_over(app, send, worker, clock, env):
    job_id = uuid.UUID(send())
    _take_messages(app)
    with app.app_context():
        first = worker("w1")
        attempt = first._claim(job_id)            # w1 claims, then dies before applying
        assert attempt == 1
        assert worker("w2").process(_message(job_id)) == "busy"   # lease still valid: hands off
        clock.advance(seconds=56)                  # lease (55 s) expired, message visible again
        assert worker("w2").process(_message(job_id)) == "done"
        job = db.session.get(DetectionJob, job_id)
        assert job.claim_attempt == 2 and job.worker_id == "w2"
        # w1 comes back and tries to apply its old result: fencing refuses it
        assert first._apply(job_id, attempt, env.analyze(PHOTO["violation"])) is None
        assert len(_results()) == 1 and _machine().pending_violation_count == 1


def test_transient_inference_error_releases_the_job_for_redelivery(app, send, worker, clock, env):
    send()
    env.fail_times = 1
    assert drain(app, worker) == ["retry"]
    with app.app_context():
        job = db.session.scalars(select(DetectionJob)).one()
        assert job.status == JobStatus.PROCESSING and "InferenceError" in job.last_error
        assert adapters.queue().depth() == 1      # not deleted
        assert _results() == []
    clock.advance(seconds=61)                     # visibility timeout
    assert drain(app, worker) == ["done"]


def test_repeated_inference_errors_end_in_the_dlq(app, send, worker, clock, env):
    send()
    env.fail_times = 3
    for _ in range(3):
        assert drain(app, worker) == ["retry"]
        clock.advance(seconds=61)
    with app.app_context():
        assert adapters.queue().receive() == []
        [dead] = adapters.queue().receive_dead_letters()
        job = db.session.scalars(select(DetectionJob)).one()
        assert dead.job_id == job.id and job.status == JobStatus.PROCESSING   # relay marks dead_lettered (step 6)


def test_transaction_failure_leaves_no_partial_result_or_violation(app, send, worker, monkeypatch):
    from app.services import worker as worker_module

    a = uuid.uuid4()
    send(session=a, seq=1)
    drain(app, worker)                             # pending count 1
    send(session=a, seq=2, event="confirm")        # this one would create the violation

    def explode(*args, **kwargs):
        raise RuntimeError("database went away mid-transaction")

    monkeypatch.setattr(worker_module, "_finish_job", explode)
    assert drain(app, worker) == ["retry"]
    with app.app_context():
        assert _new_violations(_machine().id) == []
        assert len(_results()) == 1                # only the first photo's result
        machine = _machine()
        assert machine.pending_violation_count == 1 and machine.last_applied_seq == 1
        confirm = db.session.scalars(select(DetectionJob).where(DetectionJob.seq == 2)).one()
        assert confirm.status == JobStatus.PROCESSING and confirm.result_id is None
        history = db.session.scalar(select(func.count()).select_from(ViolationHistory))
    monkeypatch.undo()
    with app.app_context():
        adapters.queue()._messages[0].visible_at = None   # make it deliverable now
        db.session.get(DetectionJob, confirm.id).lease_until = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert drain(app, worker) == ["done"]
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert db.session.scalar(select(func.count()).select_from(ViolationHistory)) > history


def test_missing_inbox_photo_and_undecodable_photo_are_rejected(app, send, worker):
    import pathlib

    gone = send()
    broken = send(PANGYO, verdict="broken")
    with app.app_context():
        job = db.session.get(DetectionJob, uuid.UUID(gone))
        (pathlib.Path(app.config["IMAGE_STORAGE_DIR"]) / job.s3_key).unlink()
    assert sorted(drain(app, worker)) == ["rejected", "rejected"]
    with app.app_context():
        jobs = {str(j.id): j for j in db.session.scalars(select(DetectionJob))}
        assert jobs[gone].last_error == "missing_image" and jobs[broken].last_error == "invalid_image"
        assert all(j.status == JobStatus.REJECTED for j in jobs.values())
        assert adapters.queue().depth() == 0 and _results() == []


def test_unreadable_message_is_isolated_to_the_dlq_not_deleted(app, worker, env):
    from app.adapters.queue import _Stored

    with app.app_context():   # raw messages as a broken producer would send them
        queue = adapters.queue()
        queue._messages.append(_Stored("x", "not json", "m-1", "fake-bad"))
        queue._messages.append(_Stored("y", '{"job_id": "' + str(uuid.uuid4()) + '"}', "m-2", "fake-unknown"))
    outcomes = drain(app, worker, limit=10)
    assert outcomes.count("isolated") == 6          # 2 messages x maxReceiveCount 3, then redriven
    with app.app_context():
        assert adapters.queue().depth() == 0
        dead = adapters.queue().receive_dead_letters()
        assert sorted(m.message_id for m in dead) == ["fake-bad", "fake-unknown"]
        assert db.session.scalar(select(func.count()).select_from(DetectionResult)) == 0


# --- effects after commit ---------------------------------------------------------------------------------

def test_new_violation_gets_its_evidence_photo_and_notification(app, send, worker):
    import pathlib

    a = uuid.uuid4()
    send(session=a, seq=1)
    confirm_id = send(session=a, seq=2, event="confirm")
    drain(app, worker)
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        job = db.session.get(DetectionJob, uuid.UUID(confirm_id))
        assert job.violation_id == violation.id and job.effects_status == "done"
        assert violation.image_status == ImageStatus.UPLOADED and violation.image_key.startswith("violations/")
        root = pathlib.Path(app.config["IMAGE_STORAGE_DIR"])
        assert (root / violation.image_key).read_bytes() == (root / job.s3_key).read_bytes()   # same bytes
        assert db.session.scalar(select(func.count()).select_from(Notification).where(
            Notification.violation_id == violation.id)) >= 1


def test_crash_after_commit_finishes_effects_on_redelivery(app, send, worker, clock, monkeypatch):
    from app.services import worker as worker_module

    a = uuid.uuid4()
    send(session=a, seq=1)
    drain(app, worker)
    send(session=a, seq=2, event="confirm")
    real = worker_module.JudgementWorker._run_effects

    def crash(self, job_id):
        raise RuntimeError("killed after commit")

    monkeypatch.setattr(worker_module.JudgementWorker, "_run_effects", crash)
    assert drain(app, worker) == ["retry"]
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert violation.image_status == ImageStatus.PENDING
        assert db.session.scalars(select(DetectionJob.effects_status).where(DetectionJob.seq == 2)).one() == "pending"
    monkeypatch.setattr(worker_module.JudgementWorker, "_run_effects", real)
    clock.advance(seconds=61)
    assert drain(app, worker) == ["already"]
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert violation.image_status == ImageStatus.UPLOADED
        assert db.session.scalars(select(DetectionJob.effects_status).where(DetectionJob.seq == 2)).one() == "done"
        assert len(_new_violations(_machine().id)) == 1 and len(_results()) == 2


def test_evidence_copy_failure_keeps_the_violation(app, send, worker, monkeypatch):
    a = uuid.uuid4()
    send(session=a, seq=1)
    send(session=a, seq=2, event="confirm")
    with app.app_context():
        storage = adapters.storage()

    def broken(key, data):
        raise OSError("bucket unavailable")

    monkeypatch.setattr(storage, "save", broken)
    assert drain(app, worker) == ["done", "done"]
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert violation.image_status == ImageStatus.FAILED
        actions = db.session.scalars(select(ViolationHistory.action).where(
            ViolationHistory.violation_id == violation.id)).all()
        assert HistoryAction.IMAGE_UPLOAD_FAILED in actions
        assert db.session.scalars(select(DetectionJob.effects_status).where(DetectionJob.seq == 2)).one() == "pending"   # retried by the relay (step 6)


# --- violation_id links (option (나), decision 2026-11-02) -------------------------------------------------

def test_violation_links_follow_the_episode_not_the_session(app, send, worker):
    """Episode 1: pending, confirm (creates), refresh (ongoing), left (ends) -> all four linked to episode 1.
    A no_vehicle before it resets the count, so that earlier violation verdict is NOT linked.
    Episode 2 in a new session: its own results only."""
    a, b = uuid.uuid4(), uuid.uuid4()
    send(session=a, seq=1)                                          # pending, then reset by:
    send(session=a, seq=2, event="refresh", verdict="novehicle")
    send(session=a, seq=3, event="refresh")                         # pending 1
    send(session=a, seq=4, event="confirm")                         # creates episode 1
    send(session=a, seq=5, event="refresh")                         # ongoing
    send(session=a, seq=6, event="left", verdict="novehicle")       # ends it
    send(session=b, seq=1)
    send(session=b, seq=2, event="confirm")                         # episode 2
    drain(app, worker)
    with app.app_context():
        first, second = _new_violations(_machine().id)
        links = [(r.seq, r.verdict, r.violation_id) for r in _results()]
        assert links == [
            (1, "violation", None), (2, "no_vehicle", None),
            (3, "violation", first.id), (4, "violation", first.id), (5, "violation", first.id),
            (6, "no_vehicle", first.id),
            (1, "violation", second.id), (2, "violation", second.id),
        ]


def test_other_machines_results_are_never_linked(app, send, worker):
    send(PANGYO, seq=1)                                             # PANGYO pending, unlinked
    a = uuid.uuid4()
    send(session=a, seq=1)
    send(session=a, seq=2, event="confirm")
    drain(app, worker)
    with app.app_context():
        pangyo = _machine(PANGYO)
        assert db.session.scalars(select(DetectionResult.violation_id).where(
            DetectionResult.machine_id == pangyo.id)).all() == [None]


# --- PostgreSQL: real concurrent workers -----------------------------------------------------------------

def _require_postgres(app):
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("PostgreSQL only (set TEST_DATABASE_URL)")


def _parallel(app, functions):
    results = [None] * len(functions)
    barrier = threading.Barrier(len(functions))

    def run(i, fn):
        with app.app_context():
            barrier.wait()
            results[i] = fn()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(functions)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    return results


def test_postgres_two_workers_on_the_same_message_apply_once(app, send, worker):
    _require_postgres(app)
    for _ in range(5):
        job_id = send(PANGYO, session=uuid.uuid4())
        _take_messages(app)
        outcomes = _parallel(app, [lambda: worker("w1").process(_message(job_id)),
                                   lambda: worker("w2").process(_message(job_id))])
        assert sorted(outcomes) in (["busy", "done"], ["already", "done"]), outcomes
    with app.app_context():
        assert len(_results()) == 5


def test_postgres_two_jobs_of_one_machine_in_parallel_keep_the_guard_consistent(app, send, worker):
    _require_postgres(app)
    a = uuid.uuid4()
    first = send(session=a, seq=1)
    second = send(session=a, seq=2, event="confirm")
    _take_messages(app)
    outcomes = _parallel(app, [lambda: worker("w1").process(_message(first)),
                               lambda: worker("w2").process(_message(second))])
    assert sorted(outcomes) in (["done", "done"], ["done", "stale"]), outcomes
    with app.app_context():
        machine = _machine()
        assert machine.last_applied_seq == 2
        violations = _new_violations(machine.id)
        if outcomes == ["done", "done"] or sorted(outcomes) == ["done", "done"]:
            assert len(violations) == 1 and violations[0].detection_count == 2
        else:   # seq 2 won the race: seq 1 is stale, one sighting only
            assert violations == [] and machine.pending_violation_count == 1
        assert len(_results()) == 2


def test_postgres_worker_and_ingest_of_the_same_machine_do_not_deadlock(app, send, worker, client):
    _require_postgres(app)
    a = uuid.uuid4()
    first = send(session=a, seq=1)
    _take_messages(app)

    def ingest_more():
        return send(session=a, seq=2, event="refresh")

    outcomes = _parallel(app, [lambda: worker("w1").process(_message(first)), ingest_more])
    assert outcomes[0] == "done" and outcomes[1] is not None
    with app.app_context():
        assert db.session.scalar(select(func.count()).select_from(DetectionJob)) == 2


# --- entry point --------------------------------------------------------------------------------------------

def test_judge_worker_command_drains_the_queue(app, send):
    a = uuid.uuid4()
    send(session=a, seq=1)
    send(session=a, seq=2, event="confirm")
    result = app.test_cli_runner().invoke(args=["judge-worker", "--until-empty"])
    assert result.exit_code == 0, result.output
    assert "handled 2 message(s)" in result.output
    with app.app_context():
        assert len(_new_violations(_machine().id)) == 1


@pytest.mark.parametrize("app_overrides", [{}])
def test_judge_worker_refuses_to_run_without_a_queue(app, app_overrides):
    result = app.test_cli_runner().invoke(args=["judge-worker", "--until-empty"])
    assert result.exit_code != 0 and "QUEUE_BACKEND=none" in result.output
