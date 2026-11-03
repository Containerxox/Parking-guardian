"""Phase 2 photo ingest: POST /device/detection-jobs (ADR 0019 decisions 1-6, design 6.2, 6.4, 6.5, 6.7, 6.10,
6.11, 6.13; SIGNAL_LOST snapshot decision 2026-11-02).

    validate -> duplicate? (200, nothing written) -> inbox photo (S3 key from event_id) -> one transaction:
    lock machine row, snapshot previous signal into prev_signal_at, register/supersede session, left/confirm
    state, job row (pending_publish), signal times -> commit -> one immediate send (unless an older job of the
    machine is still unpublished) -> 202

The queue is the in-memory FakeJobQueue and the inbox is the local storage adapter: nothing touches AWS.
PostgreSQL-only tests at the end check the real row lock and concurrent requests.
"""
import io
import threading
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app import adapters
from app.adapters.queue import QueueError
from app.cli import dev_api_key
from app.extensions import db
from app.models import (CommandStatus, CommandType, DetectionJob, DeviceCommand, DeviceSession, JobStatus, Machine,
                        SessionStatus, as_utc, utcnow)

from .conftest import API, JPEG_BYTES

GANGNAM = "RPI-A-GANGNAM-01"
PANGYO = "RPI-A-PANGYO-01"
URL = f"{API}/device/detection-jobs"


@pytest.fixture
def app_overrides():
    return {"QUEUE_BACKEND": "fake"}


@pytest.fixture
def send(client):
    """send(serial, event="arrived", session=..., seq=1, event_id=..., **extra) -> response."""

    def post(serial=GANGNAM, event="arrived", session=None, seq=1, event_id=None, data=JPEG_BYTES, **extra):
        form = {"file": (io.BytesIO(data), "photo.jpg"), "event": event,
                "event_id": str(event_id or uuid.uuid4()), "session_id": str(session or uuid.uuid4()),
                "seq": str(seq), **{k: str(v) for k, v in extra.items()}}
        return client.post(URL, data=form, content_type="multipart/form-data",
                           headers={"X-Device-Key": dev_api_key(serial)})

    return post


def _machine(serial=GANGNAM) -> Machine:
    return db.session.scalars(select(Machine).where(Machine.serial_number == serial)).one()


def _jobs():
    return db.session.scalars(select(DetectionJob).order_by(DetectionJob.created_at)).all()


def _inbox_files(app):
    root = app.config["IMAGE_STORAGE_DIR"]
    import pathlib

    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in pathlib.Path(root).rglob("*") if p.is_file())


# --- normal flow ------------------------------------------------------------------------------------------

def test_accepts_a_photo_stores_it_creates_a_job_and_sends_it(app, send):
    session, event_id = uuid.uuid4(), uuid.uuid4()
    response = send(session=session, event_id=event_id, detected_at="2026-11-02T10:00:00+09:00")
    assert response.status_code == 202, response.get_json()
    body = response.get_json()
    assert body["event_id"] == str(event_id) and body["status"] == JobStatus.QUEUED and body["duplicate"] is False

    with app.app_context():
        [job] = _jobs()
        machine = _machine()
        assert str(job.id) == body["job_id"]
        assert (job.machine_id, job.event_id, job.session_id, job.seq, job.event) == \
               (machine.id, event_id, session, 1, "arrived")
        assert job.status == JobStatus.QUEUED and job.publish_attempts == 1 and job.published_at is not None
        # decision 2026-11-02: inbox key = machine + event id only (no date, no extension), so every retry of the
        # same event names the same object whatever the clock, the day or the file type says
        assert job.s3_key == f"inbox/m-{machine.id}/{event_id}"
        assert as_utc(job.detected_at).isoformat() == "2026-11-02T01:00:00+00:00"   # device clock, display only
        assert job.received_at is not None
    assert _inbox_files(app) == [f"inbox/m-{_id(app)}/{event_id}"]
    with app.app_context():
        assert adapters.queue().depth() == 1
        [message] = adapters.queue().receive()
        assert str(message.job_id) == body["job_id"] and message.group_id == f"m-{_id(app)}"


def _id(app, serial=GANGNAM):
    with app.app_context():
        return _machine(serial).id


def test_inbox_bytes_are_stored_unchanged(app, send):
    data = b"\x89PNG\r\n\x1a\n" + b"png-bytes"
    event_id = uuid.uuid4()
    assert send(event_id=event_id, data=data).status_code == 202
    root = app.config["IMAGE_STORAGE_DIR"]
    [path] = _inbox_files(app)
    assert path == f"inbox/m-{_id(app)}/{event_id}"
    import pathlib

    assert (pathlib.Path(root) / path).read_bytes() == data   # ADR 0006: never re-encoded


def test_first_photo_of_a_session_registers_it_but_leaves_the_order_guard_alone(app, send):
    """Decision 2026-11-02: current_session_id / last_applied_seq belong to the worker (it applies jobs in
    order); ingest only records that the session exists and when the server first saw it."""
    session = uuid.uuid4()
    send(session=session)
    with app.app_context():
        machine = _machine()
        stored = db.session.get(DeviceSession, session)
        assert stored.machine_id == machine.id and stored.status == SessionStatus.ACTIVE
        assert stored.first_received_at is not None
        assert machine.current_session_id is None and machine.last_applied_seq == 0


def test_any_event_can_open_a_session_the_server_has_not_seen(app, send):
    """The device may have given up on `arrived` (30 min); a later refresh still registers the session."""
    session = uuid.uuid4()
    assert send(event="refresh", session=session, seq=4).status_code == 202
    with app.app_context():
        assert db.session.get(DeviceSession, session).status == SessionStatus.ACTIVE


def test_new_session_supersedes_the_previous_one_and_cancels_its_commands(app, send):
    old, new = uuid.uuid4(), uuid.uuid4()
    send(session=old)
    with app.app_context():
        machine = _machine()
        machine.last_applied_seq = 2
        command = DeviceCommand(machine_id=machine.id, session_id=old, type=CommandType.CAPTURE_CONFIRM,
                                due_at=utcnow(), expires_at=utcnow() + timedelta(minutes=9))
        db.session.add(command)
        db.session.commit()
        command_id = command.command_id

    send(session=new)
    with app.app_context():
        machine = _machine()
        # the worker's guard state is untouched: old's queued jobs must still be judged normally
        assert machine.last_applied_seq == 2 and machine.current_session_id is None
        assert db.session.get(DeviceSession, old).status == SessionStatus.SUPERSEDED
        assert db.session.get(DeviceSession, new).status == SessionStatus.ACTIVE
        assert db.session.get(DeviceCommand, command_id).status == CommandStatus.CANCELLED


def test_late_photo_of_an_older_session_is_accepted_without_reviving_it(app, send):
    old, new = uuid.uuid4(), uuid.uuid4()
    send(session=old)
    send(session=new)
    assert send(event="refresh", session=old, seq=2).status_code == 202   # the worker decides (stale)
    with app.app_context():
        assert db.session.get(DeviceSession, old).status == SessionStatus.SUPERSEDED
        assert db.session.get(DeviceSession, new).status == SessionStatus.ACTIVE
        assert len(_jobs()) == 3


# --- inbox key: same machine + event id = same object, never silently replaced (decision 2026-11-02) --------

def _inbox_bytes(app, machine_id, event_id):
    import pathlib

    return (pathlib.Path(app.config["IMAGE_STORAGE_DIR"]) / f"inbox/m-{machine_id}/{event_id}").read_bytes()


def _fail_first_commit(monkeypatch):
    from app.services import ingest

    real_commit, calls = ingest._commit_ingest, {"n": 0}

    def failing_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("COMMIT", {}, Exception("connection lost"))
        real_commit()

    monkeypatch.setattr(ingest, "_commit_ingest", failing_once)


def test_retry_after_a_database_failure_across_midnight_uses_the_same_object(app, send, monkeypatch):
    _fail_first_commit(monkeypatch)
    session, event_id = uuid.uuid4(), uuid.uuid4()
    first = send(session=session, event_id=event_id, detected_at="2026-11-02T23:59:59+09:00")
    retry = send(session=session, event_id=event_id, detected_at="2026-11-03T00:00:30+09:00")  # clock moved on
    assert (first.status_code, retry.status_code) == (503, 202)
    assert _inbox_files(app) == [f"inbox/m-{_id(app)}/{event_id}"]
    assert _inbox_bytes(app, _id(app), event_id) == JPEG_BYTES


def test_retry_with_another_file_type_for_the_same_event_is_a_conflict(app, send, monkeypatch):
    """The key has no extension, so a PNG resent under a JPEG's event id lands on the same key and is refused."""
    _fail_first_commit(monkeypatch)
    session, event_id = uuid.uuid4(), uuid.uuid4()
    assert send(session=session, event_id=event_id).status_code == 503       # JPEG stored, job not recorded
    png = b"\x89PNG\r\n\x1a\n" + b"other"
    response = send(session=session, event_id=event_id, data=png)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "event_id_conflict"
    assert _inbox_bytes(app, _id(app), event_id) == JPEG_BYTES                # not replaced
    with app.app_context():
        assert _jobs() == []


def test_other_photo_content_under_an_accepted_event_id_is_a_conflict_and_keeps_the_original(app, send):
    session, event_id = uuid.uuid4(), uuid.uuid4()
    assert send(session=session, event_id=event_id).status_code == 202
    other = b"\xff\xd8\xff\xe0" + b"a different photo"
    response = send(session=session, event_id=event_id, data=other)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "event_id_conflict"
    assert _inbox_bytes(app, _id(app), event_id) == JPEG_BYTES
    with app.app_context():
        assert len(_jobs()) == 1


def test_duplicate_whose_inbox_photo_has_expired_is_still_a_duplicate(app, send):
    """After the 7-day inbox lifecycle the bytes cannot be compared; the job already exists, so answer 200."""
    import pathlib

    session, event_id = uuid.uuid4(), uuid.uuid4()
    send(session=session, event_id=event_id)
    (pathlib.Path(app.config["IMAGE_STORAGE_DIR"]) / f"inbox/m-{_id(app)}/{event_id}").unlink()
    assert send(session=session, event_id=event_id).status_code == 200


def test_left_ends_the_session_at_once_and_cancels_its_commands(app, send):
    """Design 6.2: the state part of `left` is applied immediately; the episode ends only on the AI verdict."""
    session = uuid.uuid4()
    send(session=session)
    with app.app_context():
        machine = _machine()
        db.session.add(DeviceCommand(machine_id=machine.id, session_id=session, type=CommandType.CAPTURE_CONFIRM,
                                     due_at=utcnow(), expires_at=utcnow() + timedelta(minutes=9),
                                     status=CommandStatus.DELIVERED))
        db.session.commit()
    assert send(event="left", session=session, seq=2).status_code == 202
    with app.app_context():
        stored = db.session.get(DeviceSession, session)
        assert stored.status == SessionStatus.ENDED and stored.left_reported_at is not None
        assert db.session.scalars(select(DeviceCommand.status)).all() == [CommandStatus.CANCELLED]
        assert [j.event for j in _jobs()] == ["arrived", "left"]   # the empty-space photo is still judged


def test_confirm_photo_fulfils_its_command(app, send):
    session = uuid.uuid4()
    send(session=session)
    with app.app_context():
        machine = _machine()
        command = DeviceCommand(machine_id=machine.id, session_id=session, type=CommandType.CAPTURE_CONFIRM,
                                due_at=utcnow(), expires_at=utcnow() + timedelta(minutes=9),
                                status=CommandStatus.DELIVERED)
        db.session.add(command)
        db.session.commit()
        command_id = command.command_id
    assert send(event="confirm", session=session, seq=2, command_id=command_id).status_code == 202
    with app.app_context():
        assert db.session.get(DeviceCommand, command_id).status == CommandStatus.FULFILLED


def test_confirm_naming_a_foreign_or_unknown_command_is_refused_with_409(app, client, send):
    """Decision 2026-11-02 (was: the photo was accepted and the command_id ignored). An explicit command_id that is not
    a valid confirm command of this machine and session is a device bug: 409 command_conflict, not retryable."""
    pangyo_session, session = uuid.uuid4(), uuid.uuid4()
    send(PANGYO, session=pangyo_session)
    send(session=session)
    with app.app_context():
        command = DeviceCommand(machine_id=_machine(PANGYO).id, session_id=pangyo_session,
                                type=CommandType.CAPTURE_CONFIRM, due_at=utcnow(),
                                expires_at=utcnow() + timedelta(minutes=9))
        db.session.add(command)
        db.session.commit()
        command_id = command.command_id

    def confirm(seq, command):
        form = {"file": (io.BytesIO(JPEG_BYTES + b"%d" % seq), "p.jpg"), "event": "confirm",
                "event_id": str(uuid.uuid4()), "session_id": str(session), "seq": str(seq), "command_id": str(command)}
        return client.post(URL, data=form, content_type="multipart/form-data",
                           headers={"X-Device-Key": dev_api_key(GANGNAM)})

    for response in (confirm(2, command_id), confirm(3, uuid.uuid4())):
        assert response.status_code == 409 and response.get_json()["error"]["code"] == "command_conflict"
    with app.app_context():
        assert db.session.get(DeviceCommand, command_id).status == CommandStatus.PENDING   # another machine's, untouched
        assert [j.event for j in _jobs()] == ["arrived", "arrived"]                        # no confirm job was created


# --- SIGNAL_LOST snapshot (decision 2026-11-02) ------------------------------------------------------------

def test_previous_signal_is_snapshotted_before_the_signal_is_updated(app, send):
    with app.app_context():
        machine = _machine()
        machine.last_detection_at = utcnow() - timedelta(minutes=30)
        machine.last_heartbeat_at = utcnow() - timedelta(minutes=3)
        db.session.commit()
        heartbeat = as_utc(machine.last_heartbeat_at)
    send()
    with app.app_context():
        [job] = _jobs()
        machine = _machine()
        assert as_utc(job.prev_signal_at) == heartbeat   # the later of photo and heartbeat
        received = as_utc(job.received_at)
        assert as_utc(machine.last_heartbeat_at) == received and as_utc(machine.last_detection_at) == received


def test_first_ever_signal_has_no_previous_signal(app, send):
    with app.app_context():
        machine = _machine()
        machine.last_detection_at = machine.last_heartbeat_at = None
        db.session.commit()
    send()
    with app.app_context():
        assert _jobs()[0].prev_signal_at is None


def test_heartbeat_between_photos_becomes_the_next_snapshot(app, send, client):
    send(seq=1, session=(session := uuid.uuid4()))
    assert client.post(f"{API}/device/heartbeat", headers={"X-Device-Key": dev_api_key(GANGNAM)}).status_code == 200
    with app.app_context():
        heartbeat = as_utc(_machine().last_heartbeat_at)
    send(event="refresh", seq=2, session=session)
    with app.app_context():
        assert as_utc(_jobs()[1].prev_signal_at) == heartbeat


def test_signal_times_never_move_backwards_at_ingest(app, send):
    future = utcnow() + timedelta(minutes=5)   # e.g. a heartbeat recorded with a slightly later clock reading
    with app.app_context():
        machine = _machine()
        machine.last_heartbeat_at = future
        db.session.commit()
    send()
    with app.app_context():
        assert as_utc(_machine().last_heartbeat_at) == future


# --- duplicates ---------------------------------------------------------------------------------------------

def test_same_event_twice_returns_the_same_job_with_200(app, send):
    session, event_id = uuid.uuid4(), uuid.uuid4()
    first = send(session=session, event_id=event_id)
    second = send(session=session, event_id=event_id)
    assert (first.status_code, second.status_code) == (202, 200)
    assert second.get_json()["job_id"] == first.get_json()["job_id"] and second.get_json()["duplicate"] is True
    with app.app_context():
        assert len(_jobs()) == 1 and adapters.queue().depth() == 1
    assert len(_inbox_files(app)) == 1


def test_same_event_id_on_another_machine_is_a_different_event(app, send):
    event_id = uuid.uuid4()
    assert send(GANGNAM, event_id=event_id).status_code == 202
    assert send(PANGYO, event_id=event_id).status_code == 202


def test_same_event_id_with_different_content_is_a_conflict(app, send):
    session, event_id = uuid.uuid4(), uuid.uuid4()
    send(session=session, event_id=event_id, seq=1)
    response = send(session=session, event_id=event_id, seq=2)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "event_id_conflict"
    with app.app_context():
        assert len(_jobs()) == 1


def test_duplicate_detected_by_the_unique_constraint_is_still_a_200(app, send, monkeypatch):
    """Two requests passed the duplicate check at the same time; the database keeps one job (design 6.13)."""
    from app.services import ingest

    session, event_id = uuid.uuid4(), uuid.uuid4()
    first = send(session=session, event_id=event_id)
    real_find = ingest._find_job
    calls = {"n": 0}

    def miss_first_check(machine_id, eid):   # the early check misses it, as if the other request had not committed
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(machine_id, eid)

    monkeypatch.setattr(ingest, "_find_job", miss_first_check)
    second = send(session=session, event_id=event_id)
    assert second.status_code == 200 and second.get_json()["job_id"] == first.get_json()["job_id"]
    with app.app_context():
        assert len(_jobs()) == 1


# --- failures ----------------------------------------------------------------------------------------------

def test_inbox_failure_is_503_and_writes_nothing(app, send, monkeypatch):
    with app.app_context():
        before = (as_utc(_machine().last_heartbeat_at), as_utc(_machine().last_detection_at))
        storage = adapters.storage()

    def broken(key, data):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_new", broken)
    response = send()
    assert response.status_code == 503 and response.get_json()["error"]["code"] == "storage_unavailable"
    with app.app_context():
        assert _jobs() == [] and db.session.scalars(select(DeviceSession)).all() == []
        assert (as_utc(_machine().last_heartbeat_at), as_utc(_machine().last_detection_at)) == before
        assert adapters.queue().depth() == 0


def test_database_failure_is_503_and_a_retry_reuses_the_same_inbox_key(app, send, monkeypatch):
    from app.services import ingest

    session, event_id = uuid.uuid4(), uuid.uuid4()
    real_commit = ingest._commit_ingest
    calls = {"n": 0}

    def failing_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("COMMIT", {}, Exception("connection lost"))
        real_commit()

    monkeypatch.setattr(ingest, "_commit_ingest", failing_once)
    response = send(session=session, event_id=event_id)
    assert response.status_code == 503 and response.get_json()["error"]["code"] == "database_unavailable"
    with app.app_context():
        assert _jobs() == [] and db.session.scalars(select(DeviceSession)).all() == []
        assert adapters.queue().depth() == 0
    assert len(_inbox_files(app)) == 1   # left behind; inbox lifecycle (7 d) removes it if never retried

    retry = send(session=session, event_id=event_id)
    assert retry.status_code == 202
    assert len(_inbox_files(app)) == 1   # same key overwritten, not a second object
    with app.app_context():
        assert len(_jobs()) == 1


def test_queue_failure_still_202_and_the_job_waits_for_the_relay(app, send):
    with app.app_context():
        adapters.queue().fail_next("send", code="ServiceUnavailable")
    response = send()
    assert response.status_code == 202 and response.get_json()["status"] == JobStatus.PENDING_PUBLISH
    with app.app_context():
        [job] = _jobs()
        assert job.status == JobStatus.PENDING_PUBLISH and job.publish_attempts == 1
        assert job.last_error == "send: ServiceUnavailable" and job.published_at is None
        assert as_utc(job.next_publish_at) > as_utc(job.received_at)   # backoff before the relay retries
        assert adapters.queue().depth() == 0


def test_ingest_uses_the_fast_fail_send_and_nothing_else_does(app, send):
    """The intake request must not be held by a dead queue (9단계 finding: ~39 s). Only this send is fast."""
    calls = []
    with app.app_context():
        queue = adapters.queue()
        original = queue.send

        def spy(job_id, machine_id, fast=False):
            calls.append(fast)
            return original(job_id, machine_id, fast=fast)

        queue.send = spy
    assert send().status_code == 202
    assert calls == [True]


def test_a_late_bookkeeping_write_never_moves_a_job_back(app, send):
    """The ingest's bookkeeping is conditional on pending_publish: if the relay (or a worker) already moved the job
    on, a late 'send failed' or 'queued' record changes nothing."""
    from app.services import ingest

    assert send().status_code == 202
    with app.app_context():
        [job] = _jobs()
        job_id = job.id
        job.status, job.publish_attempts, job.last_error = JobStatus.DONE, 3, None
        db.session.commit()
        ingest._record_send_failure(job_id, QueueError("send", "ServiceUnavailable", "late", True), utcnow())
        ingest._mark_queued(job_id, utcnow())
        job = db.session.get(DetectionJob, job_id)
        assert job.status == JobStatus.DONE and job.publish_attempts == 3 and job.last_error is None


def _relay_publish_once(now):
    """What the outbox relay (step 6, design 6.7 job 1) will do, written out here only to prove the job row
    carries everything needed: pending_publish due jobs, per machine in order, send, mark queued."""
    due = db.session.scalars(select(DetectionJob).where(
        DetectionJob.status == JobStatus.PENDING_PUBLISH, DetectionJob.next_publish_at <= now)
        .order_by(DetectionJob.machine_id, DetectionJob.created_at)).all()
    for job in due:
        adapters.queue().send(job.id, job.machine_id)
        job.status, job.published_at = JobStatus.QUEUED, now
        job.publish_attempts += 1
    db.session.commit()
    return len(due)


def test_a_job_left_by_a_queue_failure_is_recoverable_by_the_relay(app, send):
    with app.app_context():
        adapters.queue().fail_next("send")
    body = send().get_json()
    with app.app_context():
        assert _relay_publish_once(utcnow() + timedelta(seconds=5)) == 1
        [message] = adapters.queue().receive()
        assert str(message.job_id) == body["job_id"]
        assert _jobs()[0].status == JobStatus.QUEUED


def test_sent_but_not_marked_queued_is_deduplicated_when_the_relay_sends_again(app, send, monkeypatch):
    """Design 6.11: SQS accepted the message but updating the row failed. The relay sends it again; the
    deduplication id (job id) keeps one message."""
    from app.services import ingest

    def broken(job_id, now):
        raise OperationalError("UPDATE", {}, Exception("connection lost"))

    monkeypatch.setattr(ingest, "_mark_queued", broken)
    assert send().status_code == 202
    with app.app_context():
        assert _jobs()[0].status == JobStatus.PENDING_PUBLISH and adapters.queue().depth() == 1
        _relay_publish_once(utcnow() + timedelta(seconds=5))
        assert adapters.queue().depth() == 1


def test_an_older_unpublished_job_holds_back_the_immediate_send(app, send):
    """Design 6.5: keep per-machine order at the queue too. A newer job waits for the relay when an older job
    of the same machine is still pending_publish; another machine is not affected."""
    session = uuid.uuid4()
    with app.app_context():
        adapters.queue().fail_next("send")
    send(session=session, seq=1)
    second = send(event="refresh", session=session, seq=2)
    assert second.get_json()["status"] == JobStatus.PENDING_PUBLISH
    with app.app_context():
        jobs = _jobs()
        assert [j.status for j in jobs] == [JobStatus.PENDING_PUBLISH] * 2
        assert jobs[1].publish_attempts == 0 and jobs[1].last_error is None
        assert adapters.queue().depth() == 0
    assert send(PANGYO).get_json()["status"] == JobStatus.QUEUED


# --- validation and switches ----------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("event", ""), ("event", "parked"), ("event_id", "not-a-uuid"), ("session_id", "x"),
    ("seq", "0"), ("seq", "abc"), ("command_id", "nope"), ("detected_at", "yesterday"),
])
def test_invalid_fields_are_400_and_write_nothing(app, send, field, value):
    kwargs = {"event": "arrived", "session": uuid.uuid4(), "seq": 1, "event_id": uuid.uuid4()}
    form_names = {"event": "event", "session_id": "session", "event_id": "event_id", "seq": "seq"}
    if field in form_names:
        kwargs[form_names[field]] = value
    else:
        kwargs[field] = value
    response = send(**kwargs)
    assert response.status_code == 400, response.get_json()
    with app.app_context():
        assert _jobs() == []
    assert _inbox_files(app) == []


def test_missing_or_empty_file_is_400(app, client):
    headers = {"X-Device-Key": dev_api_key(GANGNAM)}
    form = {"event": "arrived", "event_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()), "seq": "1"}
    assert client.post(URL, data=form, content_type="multipart/form-data", headers=headers).status_code == 400
    empty = {**form, "file": (io.BytesIO(b""), "x.jpg")}
    assert client.post(URL, data=empty, content_type="multipart/form-data", headers=headers).status_code == 400


def test_device_key_is_required(client):
    assert client.post(URL, data={}, content_type="multipart/form-data").status_code == 401


def test_session_of_another_machine_is_rejected(app, send):
    session = uuid.uuid4()
    send(PANGYO, session=session)
    response = send(GANGNAM, session=session)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "session_conflict"
    with app.app_context():
        assert len(_jobs()) == 1


# --- PostgreSQL: real row lock and concurrent requests -------------------------------------------------

def _require_postgres(app):
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            pytest.skip("PostgreSQL only (set TEST_DATABASE_URL)")


@pytest.fixture
def memory_storage(app, monkeypatch):
    """These tests are about the row lock and database deduplication, so the inbox is a thread-safe dict.
    (The local disk adapter once failed a concurrent first write on Windows with "invalid storage key";
    root cause unconfirmed, not reproduced in isolation. Production inbox is S3.)"""
    objects, lock = {}, threading.Lock()

    def save_new(key, data):   # create only, like the real adapters
        with lock:
            if key in objects:
                return False
            objects[key] = bytes(data)
            return True

    def read(key):
        with lock:
            if key not in objects:
                raise FileNotFoundError(key)
            return objects[key]

    with app.app_context():
        monkeypatch.setattr(adapters.storage(), "save_new", save_new)
        monkeypatch.setattr(adapters.storage(), "read", read)
    return objects


def _in_thread(fn):
    result = {}

    def run():
        result["value"] = fn()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_postgres_ingest_waits_for_the_machine_row_lock(app, send, memory_storage):
    _require_postgres(app)
    machine_id = _id(app)
    with app.app_context():
        holder = db.engine.connect()
        tx = holder.begin()
        holder.execute(text("SELECT id FROM machines WHERE id = :m FOR UPDATE"), {"m": machine_id})
        try:
            thread, result = _in_thread(lambda: send().status_code)
            time.sleep(1.0)
            assert thread.is_alive(), "ingest did not wait for the machine row lock"
        finally:
            tx.commit()
            holder.close()
    thread.join(timeout=15)
    assert result["value"] == 202


# --- bookkeeping after the immediate send must never wait for the relay's row lock (9단계 follow-up) ---------------
# The relay locks a pending_publish job (FOR UPDATE) while it sends with the slow client. In a queue outage the ingest's
# "send failed" UPDATE of the same row used to wait ~37 s for that lock. Now the ingest skips a locked row: the lock
# holder is the relay, which records its own result, and the job is pending_publish either way.

def _relay_standin_locks(job_id, holders):
    """Another connection takes the job row lock the way the relay does and keeps its transaction open."""
    conn = db.engine.connect()
    tx = conn.begin()
    conn.execute(text("SELECT id FROM detection_jobs WHERE id = :j FOR UPDATE"), {"j": job_id})
    holders.append((conn, tx))


def _release(holders, final_sql=None, **params):
    for conn, tx in holders:
        if final_sql:
            conn.execute(text(final_sql), params)
        tx.commit()
        conn.close()


def test_postgres_a_failed_send_does_not_wait_for_the_relay_row_lock(app, send, memory_storage):
    _require_postgres(app)
    holders, seen = [], {}
    with app.app_context():
        queue = adapters.queue()

        def failing_send(job_id, machine_id, fast=False):
            seen["job_id"] = job_id
            _relay_standin_locks(job_id, holders)       # the relay got the row first and is now "sending"
            raise QueueError("send", "EndpointConnectionError", "down", True)

        queue.send = failing_send
    thread, result = _in_thread(lambda: send().status_code)
    thread.join(timeout=5)
    finished_while_locked = not thread.is_alive()
    with app.app_context():
        _release(holders, "UPDATE detection_jobs SET status = 'queued', last_error = NULL WHERE id = :j", j=seen["job_id"])
    thread.join(timeout=60)
    assert finished_while_locked, "the ingest request waited for the relay's row lock"
    assert result["value"] == 202
    with app.app_context():   # the relay's later outcome stands: the late failure record did not revert it
        [job] = _jobs()
        assert job.status == JobStatus.QUEUED and job.last_error is None


def test_postgres_a_failed_send_on_a_free_row_is_recorded_as_before(app, send, memory_storage):
    _require_postgres(app)
    with app.app_context():
        adapters.queue().fail_next("send", code="ServiceUnavailable")
    assert send().status_code == 202
    with app.app_context():
        [job] = _jobs()
        assert job.status == JobStatus.PENDING_PUBLISH and job.publish_attempts == 1
        assert job.last_error == "send: ServiceUnavailable"


def test_postgres_marking_queued_does_not_wait_for_the_relay_row_lock(app, send, memory_storage):
    _require_postgres(app)
    holders, seen = [], {}
    with app.app_context():
        queue = adapters.queue()
        real = queue.send

        def send_then_the_relay_locks(job_id, machine_id, fast=False):
            result = real(job_id, machine_id, fast=fast)    # the message is in the queue...
            seen["job_id"] = job_id
            _relay_standin_locks(job_id, holders)           # ...and the relay is already holding the row
            return result

        queue.send = send_then_the_relay_locks
    thread, result = _in_thread(lambda: send().status_code)
    thread.join(timeout=5)
    finished_while_locked = not thread.is_alive()
    with app.app_context():
        _release(holders, "UPDATE detection_jobs SET status = 'queued' WHERE id = :j", j=seen["job_id"])
    thread.join(timeout=60)
    assert finished_while_locked, "the ingest request waited for the relay's row lock"
    assert result["value"] == 202
    with app.app_context():
        [job] = _jobs()
        assert job.status == JobStatus.QUEUED and adapters.queue().depth() == 1   # one message, no duplicate


def test_postgres_concurrent_duplicates_create_one_job(app, send, memory_storage):
    _require_postgres(app)
    session, event_id = uuid.uuid4(), uuid.uuid4()
    machine_id = _id(app)
    with app.app_context():
        holder = db.engine.connect()
        tx = holder.begin()
        holder.execute(text("SELECT id FROM machines WHERE id = :m FOR UPDATE"), {"m": machine_id})
        try:   # both requests pass the duplicate check, then queue up behind the lock
            threads = [_in_thread(lambda: send(session=session, event_id=event_id)) for _ in range(2)]
            time.sleep(1.0)
        finally:
            tx.commit()
            holder.close()
    for thread, _ in threads:
        thread.join(timeout=15)
    codes = sorted(result["value"].status_code for _, result in threads)
    job_ids = {result["value"].get_json()["job_id"] for _, result in threads}
    assert codes == [200, 202] and len(job_ids) == 1
    with app.app_context():
        assert len(_jobs()) == 1 and adapters.queue().depth() == 1


def test_postgres_heartbeat_racing_an_ingest(app, send, client, memory_storage):
    """A heartbeat and a photo of the same machine at the same moment: both succeed, one job, and the snapshot
    is either the signal before the heartbeat or the heartbeat itself (whichever committed first)."""
    _require_postgres(app)
    machine_id = _id(app)
    with app.app_context():
        machine = db.session.get(Machine, machine_id)
        machine.last_heartbeat_at = machine.last_detection_at = utcnow() - timedelta(minutes=2)
        db.session.commit()
        before = as_utc(machine.last_heartbeat_at)
        holder = db.engine.connect()
        tx = holder.begin()
        holder.execute(text("SELECT id FROM machines WHERE id = :m FOR UPDATE"), {"m": machine_id})
        try:
            heartbeat_client = app.test_client()
            photo = _in_thread(lambda: send())
            beat = _in_thread(lambda: heartbeat_client.post(
                f"{API}/device/heartbeat", headers={"X-Device-Key": dev_api_key(GANGNAM)}))
            time.sleep(1.0)
        finally:
            tx.commit()
            holder.close()
    for thread, _ in (photo, beat):
        thread.join(timeout=15)
    assert photo[1]["value"].status_code == 202 and beat[1]["value"].status_code == 200
    with app.app_context():
        [job] = _jobs()
        machine = db.session.get(Machine, machine_id)
        snapshot = as_utc(job.prev_signal_at)
        assert snapshot == before or snapshot > before
        assert as_utc(machine.last_heartbeat_at) > before


# --- Phase 1 stays as it was --------------------------------------------------------------------------------

@pytest.mark.parametrize("app_overrides", [{}])   # QUEUE_BACKEND left at its default: none
def test_ingest_is_off_when_no_queue_is_configured(app, send, app_overrides):
    response = send()
    assert response.status_code == 404 and response.get_json()["error"]["code"] == "async_ingest_disabled"
    assert _inbox_files(app) == []


def test_phase1_sync_endpoint_is_unchanged_with_the_queue_on(app, upload):
    response = upload(GANGNAM)
    assert response.status_code == 201
    with app.app_context():
        assert _jobs() == [] and adapters.queue().depth() == 0


# --- heartbeat only moves the signal forward (decision 2026-11-02) ------------------------------------------

def _heartbeat(client, serial=GANGNAM):
    return client.post(f"{API}/device/heartbeat", headers={"X-Device-Key": dev_api_key(serial)})


def test_heartbeat_still_updates_the_signal_as_in_phase1(app, client):
    with app.app_context():
        machine = _machine()
        machine.last_heartbeat_at = utcnow() - timedelta(minutes=5)
        db.session.commit()
    before = utcnow()
    response = _heartbeat(client)
    assert response.status_code == 200 and response.get_json()["ok"] is True
    with app.app_context():
        assert as_utc(_machine().last_heartbeat_at) >= before


def test_heartbeat_does_not_move_a_later_signal_backwards(app, client):
    later = utcnow() + timedelta(seconds=30)   # e.g. a photo received a moment after this heartbeat was read
    with app.app_context():
        machine = _machine()
        machine.last_heartbeat_at = later
        db.session.commit()
    assert _heartbeat(client).status_code == 200
    with app.app_context():
        assert as_utc(_machine().last_heartbeat_at) == later


def test_postgres_heartbeat_waiting_on_the_row_lock_does_not_rewind_the_signal(app, client):
    """The heartbeat reads its time, then waits behind an ingest that writes a LATER receipt time. When the lock
    is released the heartbeat must not overwrite it with its earlier time."""
    _require_postgres(app)
    machine_id = _id(app)
    later = utcnow() + timedelta(seconds=5)
    with app.app_context():
        holder = db.engine.connect()
        tx = holder.begin()
        holder.execute(text("SELECT id FROM machines WHERE id = :m FOR UPDATE"), {"m": machine_id})
        holder.execute(text("UPDATE machines SET last_heartbeat_at = :t WHERE id = :m"), {"t": later, "m": machine_id})
        try:
            thread, result = _in_thread(lambda: _heartbeat(client).status_code)
            time.sleep(1.0)
            assert thread.is_alive(), "heartbeat did not wait for the row lock"
        finally:
            tx.commit()
            holder.close()
    thread.join(timeout=15)
    assert result["value"] == 200
    with app.app_context():
        assert as_utc(db.session.get(Machine, machine_id).last_heartbeat_at) == later
