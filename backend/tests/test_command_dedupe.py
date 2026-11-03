"""One confirm command = at most one confirm job (decision 2026-11-02, ADR 0019 "confirm 명령 중복 반영 보호", option A).

    detection_jobs.command_id + UNIQUE(machine_id, command_id) WHERE command_id IS NOT NULL

A confirm photo's `command_id` is a link to a command only when it is valid: the command exists, belongs to THIS
machine and to the photo's session, and the photo is a `confirm`. A valid link that already has a job makes the
photo a duplicate: 200 with the existing job, and nothing new is stored (no job, no inbox object, no queue message,
no signal update). An invalid claim is ignored: the photo is judged like any other and the link stays NULL.
A retried `event_id` whose command differs from the stored one is a 409 `event_id_conflict`.

The harness is test_worker's: real ingest endpoint, in-memory queue on a test clock, scripted inference.
"""
import io
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app import adapters
from app.cli import dev_api_key
from app.extensions import db
from app.models import (CommandStatus, CommandType, DetectionJob, DeviceCommand, DeviceSession, JobStatus, as_utc,
                        utcnow)
from app.services.relay import OutboxRelay

from .conftest import API
from .test_ingest import memory_storage  # noqa: F401  (fixture: thread-safe in-memory inbox for the PostgreSQL race)
from .test_worker import (GANGNAM, PANGYO, PHOTO, _machine, _new_violations, _parallel, _require_postgres,  # noqa: F401
                          _results, app_overrides, clock, drain, env, worker)

URL = f"{API}/device/detection-jobs"


def post(client, session, seq, event="confirm", command_id=None, event_id=None, serial=GANGNAM, data=None):
    body = data if data is not None else PHOTO["violation"] + uuid.uuid4().hex.encode()
    form = {"file": (io.BytesIO(body), "p.jpg"), "event": event, "event_id": str(event_id or uuid.uuid4()),
            "session_id": str(session), "seq": str(seq)}
    if command_id is not None:
        form["command_id"] = str(command_id)
    return client.post(URL, data=form, content_type="multipart/form-data",
                       headers={"X-Device-Key": dev_api_key(serial)})


def heartbeat(client, session, serial=GANGNAM, acked=()):
    body = {"session_id": str(session)}
    if acked:
        body["acked_commands"] = [str(a) for a in acked]
    return client.post(f"{API}/device/heartbeat", json=body, headers={"X-Device-Key": dev_api_key(serial)})


@pytest.fixture
def started(app, client, env, worker):
    """arrived judged violation_pending -> a confirm command, delivered by a heartbeat."""
    session = uuid.uuid4()
    assert post(client, session, 1, "arrived").status_code == 202
    drain(app, worker)
    with app.app_context():
        command_id = db.session.scalars(select(DeviceCommand)).one().command_id
    assert heartbeat(client, session).status_code == 200
    return session, command_id


def confirm_jobs(app):
    with app.app_context():
        rows = db.session.scalars(select(DetectionJob).where(DetectionJob.event == "confirm")
                                  .order_by(DetectionJob.created_at, DetectionJob.seq)).all()
        return [(str(j.id), str(j.event_id), j.status, str(j.command_id) if j.command_id else None) for j in rows]


def command_status(app, command_id):
    with app.app_context():
        return db.session.get(DeviceCommand, command_id).status


def inbox_objects(app):
    import pathlib

    root = pathlib.Path(app.config["IMAGE_STORAGE_DIR"]) / "inbox"
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())


def set_job_status(app, job_id, status):
    with app.app_context():
        db.session.execute(update(DetectionJob).where(DetectionJob.id == uuid.UUID(job_id)).values(status=status))
        db.session.commit()


# --- T1: the second photo is a duplicate and nothing new is stored -------------------------------------------

def test_second_confirm_for_the_same_command_returns_the_first_job_and_stores_nothing(app, client, started):
    session, command_id = started
    first = post(client, session, 2, command_id=command_id)
    assert first.status_code == 202
    with app.app_context():
        queue_depth = adapters.queue().depth()
        signal = as_utc(_machine().last_detection_at)
    objects_before = inbox_objects(app)

    second_event = uuid.uuid4()
    second = post(client, session, 3, command_id=command_id, event_id=second_event)

    assert second.status_code == 200
    body = second.get_json()
    assert body["duplicate"] is True and body["reason"] == "command_already_confirmed"
    assert body["job_id"] == first.get_json()["job_id"] and body["command_id"] == str(command_id)
    assert body["event_id"] == str(second_event) and body["existing_event_id"] == first.get_json()["event_id"]
    assert len(confirm_jobs(app)) == 1                                   # no second job
    assert inbox_objects(app) == objects_before                          # no second photo
    with app.app_context():
        assert adapters.queue().depth() == queue_depth                   # no second queue message
        assert as_utc(_machine().last_detection_at) == signal            # the duplicate is not a new signal


@pytest.mark.parametrize("judged_before_the_second", [False, True])
def test_the_duplicate_adds_no_sighting_and_no_second_judgement(app, client, started, worker, judged_before_the_second):
    """T2: before the worker ran (V1) or after the violation already existed (V2): detection_count stays 2."""
    session, command_id = started
    assert post(client, session, 2, command_id=command_id).status_code == 202
    if judged_before_the_second:
        drain(app, worker)
    assert post(client, session, 3, command_id=command_id).status_code == 200
    drain(app, worker)
    with app.app_context():
        [violation] = _new_violations(_machine().id)
        assert violation.detection_count == 2                            # arrived + one confirm (was 3 before the fix)
        results = _results()
        assert len(results) == 2 and all(r.applied for r in results)
    assert len(confirm_jobs(app)) == 1
    assert command_status(app, command_id) == CommandStatus.FULFILLED    # the duplicate did not touch the command


@pytest.mark.parametrize("state", ["delivered", "cancelled", "expired"])
def test_the_protection_does_not_depend_on_the_command_status(app, client, started, worker, state):
    """T3/T4: V3 and V4 of the probe (cancelled / expired) were the cases a status-only check could not catch."""
    session, command_id = started
    if state == "cancelled":
        heartbeat(client, session, acked=[command_id])
    if state == "expired":
        with app.app_context():
            db.session.execute(update(DeviceCommand).where(DeviceCommand.command_id == command_id)
                               .values(expires_at=utcnow() - timedelta(seconds=1)))
            db.session.commit()
        heartbeat(client, session)
    assert command_status(app, command_id) == ("expired" if state == "expired" else state)

    first = post(client, session, 2, command_id=command_id)
    second = post(client, session, 3, command_id=command_id)
    assert (first.status_code, second.status_code) == (202, 200)
    assert second.get_json()["reason"] == "command_already_confirmed"
    assert command_status(app, command_id) == (CommandStatus.FULFILLED if state == "delivered" else state)
    drain(app, worker)
    with app.app_context():
        assert _new_violations(_machine().id)[0].detection_count == 2
    assert len(confirm_jobs(app)) == 1


def test_whichever_confirm_arrives_first_wins_when_they_arrive_out_of_order(app, client, started, worker):
    """T5: the older photo (seq 2) arrives after the newer one (seq 3): it is the duplicate."""
    session, command_id = started
    newer = post(client, session, 3, command_id=command_id)
    older = post(client, session, 2, command_id=command_id)
    assert (newer.status_code, older.status_code) == (202, 200)
    assert older.get_json()["job_id"] == newer.get_json()["job_id"]
    drain(app, worker)
    with app.app_context():
        violations = _new_violations(_machine().id)
        assert len(violations) == 1 and violations[0].detection_count == 2
    assert len(confirm_jobs(app)) == 1


# --- the existing job's fate is reported, not hidden ---------------------------------------------------------

@pytest.mark.parametrize("status", [JobStatus.PENDING_PUBLISH, JobStatus.QUEUED, JobStatus.PROCESSING,
                                    JobStatus.DONE, JobStatus.STALE, JobStatus.REJECTED, JobStatus.DEAD_LETTERED])
def test_a_duplicate_reports_what_became_of_the_first_photo(app, client, started, status):
    """200 means: the server already holds a photo for this command, sending again changes nothing. `status` says
    what became of it. rejected = final, no confirm verdict for this command (the next refresh covers it);
    dead_lettered = an operator can still redrive it (flask jobs redrive)."""
    session, command_id = started
    first = post(client, session, 2, command_id=command_id)
    set_job_status(app, first.get_json()["job_id"], status)
    second = post(client, session, 3, command_id=command_id)
    assert second.status_code == 200
    body = second.get_json()
    assert body["status"] == status and body["job_id"] == first.get_json()["job_id"]
    assert body["reason"] == "command_already_confirmed"
    assert len(confirm_jobs(app)) == 1
    with app.app_context():
        assert db.session.get(DetectionJob, uuid.UUID(body["job_id"])).status == status   # the duplicate changed nothing


def test_a_dead_lettered_first_job_can_still_be_redriven_after_a_duplicate(app, client, started, worker):
    session, command_id = started
    first = post(client, session, 2, command_id=command_id)
    job_id = first.get_json()["job_id"]
    set_job_status(app, job_id, JobStatus.DEAD_LETTERED)
    assert post(client, session, 3, command_id=command_id).status_code == 200
    result = app.test_cli_runner().invoke(args=["jobs", "redrive", job_id])
    assert result.exit_code == 0 and "1 job" in result.output


# --- an explicit command_id must name a valid confirm command of this machine and session -------------------
# Decision 2026-11-02 (replaces "ignore invalid claims"): anything else is a device bug and is refused with 409
# command_conflict, which the device treats as not retryable. A refused request leaves no trace.

def state_snapshot(app):
    """Everything a refused request must leave untouched."""
    with app.app_context():
        machine = _machine()
        return {
            "jobs": [(str(j.id), j.status) for j in db.session.scalars(
                select(DetectionJob).order_by(DetectionJob.created_at, DetectionJob.id))],
            "inbox": inbox_objects(app),
            "queue": adapters.queue().depth(),
            "signal": (as_utc(machine.last_detection_at), as_utc(machine.last_heartbeat_at)),
            "commands": sorted((str(c.command_id), c.status) for c in db.session.scalars(select(DeviceCommand))),
            "sessions": sorted((str(s.session_id), s.status) for s in db.session.scalars(select(DeviceSession))),
        }


def assert_refused(response, app, before):
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["error"]["code"] == "command_conflict"
    assert state_snapshot(app) == before     # no job, no inbox object, no queue message, no session, command untouched


def test_an_unknown_command_id_is_refused_and_nothing_is_stored(app, client, started):
    session, command_id = started
    before = state_snapshot(app)
    unknown = uuid.uuid4()
    assert_refused(post(client, session, 2, command_id=unknown), app, before)
    assert_refused(post(client, session, 3, command_id=unknown), app, before)    # refused again, still nothing stored
    assert command_status(app, command_id) == CommandStatus.DELIVERED
    assert post(client, session, 2, command_id=command_id).status_code == 202   # the real confirm is unaffected


def test_another_machines_command_is_refused(app, client, started):
    session, command_id = started
    other_session = uuid.uuid4()
    assert post(client, other_session, 1, "arrived", serial=PANGYO).status_code == 202
    before = state_snapshot(app)
    assert_refused(post(client, other_session, 2, command_id=command_id, serial=PANGYO), app, before)
    assert command_status(app, command_id) == CommandStatus.DELIVERED           # the owner's command is untouched
    assert post(client, session, 2, command_id=command_id).status_code == 202   # and still usable by its owner


def test_another_machines_command_and_session_are_refused_before_anything_is_stored(app, client, started):
    """The session check alone would not catch this one: PANGYO reuses GANGNAM's session id AND its command id, so the
    command's session matches the photo's. It must be refused as a command conflict up front, not later as a
    session conflict after the photo has already been written to the inbox."""
    session, command_id = started
    before = state_snapshot(app)
    assert_refused(post(client, session, 2, command_id=command_id, serial=PANGYO), app, before)
    assert command_status(app, command_id) == CommandStatus.DELIVERED


def test_a_command_of_another_session_is_refused_and_that_session_is_not_registered(app, client, started):
    session, command_id = started
    before = state_snapshot(app)
    assert_refused(post(client, uuid.uuid4(), 1, command_id=command_id), app, before)   # also: no new session row


def test_a_job_of_another_session_holding_the_command_is_a_conflict_not_a_duplicate(app, client, started):
    session, command_id = started
    assert post(client, session, 2, command_id=command_id).status_code == 202          # the owner job, session S1
    before = state_snapshot(app)
    assert_refused(post(client, uuid.uuid4(), 1, command_id=command_id), app, before)  # S2 naming it: not a 200


@pytest.mark.parametrize("event", ["arrived", "refresh", "left"])
def test_only_a_confirm_photo_may_carry_a_command_id(app, client, started, event):
    session, command_id = started
    before = state_snapshot(app)
    assert_refused(post(client, session, 2, event=event, command_id=command_id), app, before)
    # a refused `left` neither ended the session nor cancelled the command; the command is not fulfilled either
    assert command_status(app, command_id) == CommandStatus.DELIVERED
    assert post(client, session, 2, command_id=command_id).status_code == 202          # the real confirm still works


def test_confirms_without_a_command_are_never_deduplicated(app, client, started):
    session, _ = started
    assert [post(client, session, seq).status_code for seq in (2, 3, 4)] == [202, 202, 202]


def test_other_event_types_without_a_command_are_unaffected(app, client, started):
    session, _ = started
    assert [post(client, session, seq, event=event).status_code
            for seq, event in ((2, "refresh"), (3, "left"))] == [202, 202]


def test_a_duplicate_of_the_command_is_answered_the_same_every_time(app, client, started):
    """Idempotency of the deduplicated request itself: the device may retry it after a lost 200."""
    session, command_id = started
    first = post(client, session, 2, command_id=command_id)
    retried_event = uuid.uuid4()
    answers = [post(client, session, 3, command_id=command_id, event_id=retried_event) for _ in range(3)]
    assert [a.status_code for a in answers] == [200, 200, 200]
    assert {a.get_json()["job_id"] for a in answers} == {first.get_json()["job_id"]}
    assert {a.get_json()["reason"] for a in answers} == {"command_already_confirmed"}
    assert len(confirm_jobs(app)) == 1


def test_the_same_command_on_two_machines_is_two_different_things(app, client, started, worker):
    """The unique index is per machine. Machine B's own command (a different id) is unaffected by machine A's."""
    session, command_id = started
    assert post(client, session, 2, command_id=command_id).status_code == 202
    other_session = uuid.uuid4()
    assert post(client, other_session, 1, "arrived", serial=PANGYO).status_code == 202
    drain(app, worker)
    with app.app_context():
        other_command = db.session.scalars(select(DeviceCommand).where(
            DeviceCommand.session_id == other_session)).one().command_id
    assert other_command != command_id
    assert post(client, other_session, 2, command_id=other_command, serial=PANGYO).status_code == 202


# --- T7: the same event retried -------------------------------------------------------------------------------

def test_the_same_event_retried_with_the_same_command_is_an_ordinary_duplicate(app, client, started):
    session, command_id = started
    event_id, data = uuid.uuid4(), PHOTO["violation"] + b"fixed"
    first = post(client, session, 2, command_id=command_id, event_id=event_id, data=data)
    retry = post(client, session, 2, command_id=command_id, event_id=event_id, data=data)
    assert (first.status_code, retry.status_code) == (202, 200)
    body = retry.get_json()
    assert body["duplicate"] is True and body["job_id"] == first.get_json()["job_id"]
    assert "reason" not in body                                          # the step-4 event-duplicate answer, unchanged
    assert len(confirm_jobs(app)) == 1


def _second_command(app, session):
    """Another real command for the same session (the first is no longer active once fulfilled)."""
    with app.app_context():
        machine = _machine()
        due = utcnow() + timedelta(seconds=60)
        command = DeviceCommand(machine_id=machine.id, session_id=session, type=CommandType.CAPTURE_CONFIRM,
                                due_at=due, expires_at=due + timedelta(minutes=9), status=CommandStatus.PENDING)
        db.session.add(command)
        db.session.commit()
        return command.command_id


def test_the_same_event_with_a_different_command_is_a_conflict(app, client, started):
    session, command_id = started
    event_id, data = uuid.uuid4(), PHOTO["violation"] + b"fixed"
    assert post(client, session, 2, command_id=command_id, event_id=event_id, data=data).status_code == 202
    other = _second_command(app, session)
    response = post(client, session, 2, command_id=other, event_id=event_id, data=data)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "event_id_conflict"
    assert len(confirm_jobs(app)) == 1


def test_the_same_event_with_the_command_added_or_dropped_is_a_conflict(app, client, started):
    session, command_id = started
    data = PHOTO["violation"] + b"fixed"
    with_command, without_command = uuid.uuid4(), uuid.uuid4()
    assert post(client, session, 2, command_id=command_id, event_id=with_command, data=data).status_code == 202
    assert post(client, session, 2, event_id=with_command, data=data).status_code == 409        # command dropped

    assert post(client, session, 3, event_id=without_command, data=data).status_code == 202      # sent without one
    other = _second_command(app, session)
    assert post(client, session, 3, command_id=other, event_id=without_command, data=data).status_code == 409   # added


def test_a_retried_event_that_adds_an_invalid_command_is_refused(app, client, started):
    """The first request had no command and was accepted; the retry names a command that is not valid: 409."""
    session, _ = started
    event_id, data = uuid.uuid4(), PHOTO["violation"] + b"fixed"
    assert post(client, session, 2, event_id=event_id, data=data).status_code == 202
    response = post(client, session, 2, command_id=uuid.uuid4(), event_id=event_id, data=data)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "command_conflict"
    assert len(confirm_jobs(app)) == 1


def test_a_retried_event_whose_command_became_invalid_is_refused_not_duplicated(app, client, started):
    """The event was accepted with a valid command; the retry swaps in an unknown one: refused, the job is untouched."""
    session, command_id = started
    event_id, data = uuid.uuid4(), PHOTO["violation"] + b"fixed"
    first = post(client, session, 2, command_id=command_id, event_id=event_id, data=data)
    assert first.status_code == 202
    response = post(client, session, 2, command_id=uuid.uuid4(), event_id=event_id, data=data)
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "command_conflict"
    assert [row[3] for row in confirm_jobs(app)] == [str(command_id)]


# --- the retention limit (jobs 30 days): what the protection remembers and for how long ----------------------

def _age(app, model, days):
    with app.app_context():
        values = {c: utcnow() - timedelta(days=days) for c in ("created_at", "updated_at") if hasattr(model, c)}
        db.session.execute(update(model).values(**values))
        db.session.commit()


def test_the_protection_outlives_the_command_row_while_the_job_exists(app, client, started, worker):
    session, command_id = started
    assert post(client, session, 2, command_id=command_id).status_code == 202
    drain(app, worker)
    _age(app, DeviceCommand, 31)
    with app.app_context():
        assert OutboxRelay().clean_up()["commands"] == 1                 # commands go first (design 6.12)
        assert db.session.get(DeviceCommand, command_id) is None
    response = post(client, session, 3, command_id=command_id)
    assert response.status_code == 200 and response.get_json()["reason"] == "command_already_confirmed"


def test_after_the_job_is_cleaned_up_a_replay_naming_the_forgotten_command_is_refused(app, client, started, worker):
    """ADR 0019 risk table: the memory is the job row (30 days after it finished). Once it is gone, so is the command
    row, so a replayed photo naming that id is no longer a valid claim. With invalid claims refused (409) the replay
    cannot be counted as another sighting any more: it is rejected, not judged."""
    session, command_id = started
    assert post(client, session, 2, command_id=command_id).status_code == 202
    drain(app, worker)
    _age(app, DeviceCommand, 31)
    _age(app, DetectionJob, 31)
    with app.app_context():
        stats = OutboxRelay().clean_up()
        assert stats["commands"] == 1 and stats["jobs"] >= 1
    before = state_snapshot(app)
    assert_refused(post(client, session, 4, command_id=command_id), app, before)


# --- Phase 1 is untouched -----------------------------------------------------------------------------------

def test_the_phase1_synchronous_endpoint_is_unaffected(app, client, upload):
    first, second = upload(GANGNAM), upload(GANGNAM)
    assert (first.status_code, second.status_code) == (200, 201)        # pending, then confirmed by the second sighting
    with app.app_context():
        assert db.session.scalars(select(DetectionJob)).all() == []


# --- PostgreSQL: real concurrency and the index as the last defence -----------------------------------------

def test_postgres_concurrent_confirms_for_one_command_create_exactly_one_job(app, client, started, worker,
                                                                           memory_storage):
    _require_postgres(app)
    session, command_id = started
    responses = _parallel(app, [lambda: post(app.test_client(), session, 2 + i, command_id=command_id)
                                for i in range(4)])
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 200, 200, 202], codes
    job_ids = {r.get_json()["job_id"] for r in responses}
    assert len(job_ids) == 1
    assert len(confirm_jobs(app)) == 1
    drain(app, worker)
    with app.app_context():
        assert _new_violations(_machine().id)[0].detection_count == 2


def test_postgres_the_index_alone_refuses_a_second_job_for_the_command(app, client, started):
    """Without the application check (two raw inserts on two connections), the database still says no."""
    _require_postgres(app)
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    session, command_id = started
    with app.app_context():
        machine_id = _machine().id
        insert = text("INSERT INTO detection_jobs (id, machine_id, event_id, session_id, seq, event, received_at, "
                      "s3_key, status, publish_attempts, claim_attempt, command_id, created_at, updated_at) VALUES "
                      "(:id, :m, :e, :s, :q, 'confirm', now(), 'inbox/x', 'queued', 0, 0, :c, now(), now())")
        params = {"m": machine_id, "s": session, "c": command_id}
        first, second = db.engine.connect(), db.engine.connect()
        try:
            first.execute(insert, {**params, "id": uuid.uuid4(), "e": uuid.uuid4(), "q": 7})
            first.commit()
            with pytest.raises(IntegrityError):
                second.execute(insert, {**params, "id": uuid.uuid4(), "e": uuid.uuid4(), "q": 8})
            second.rollback()
        finally:
            first.close()
            second.close()
