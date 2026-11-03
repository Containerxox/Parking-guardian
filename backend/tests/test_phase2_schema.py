"""Phase 2 (ADR 0019) tables: detection_jobs, detection_results, device_sessions, device_commands,
and the order-guard columns on machines.

These tests only check the schema rules the database itself enforces (design doc 6.4, 6.5, 6.7, 6.10,
6.13). Nothing writes these tables yet: ingest, worker and relay are later steps.
They run on SQLite by default and on PostgreSQL with TEST_DATABASE_URL (see conftest.py).
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (CommandStatus, CommandType, DetectionJob, DetectionResult, DeviceCommand, DeviceSession,
                        JobStatus, Machine, SessionStatus, Violation, utcnow)

from .conftest import API

GANGNAM = "RPI-A-GANGNAM-01"
PANGYO = "RPI-A-PANGYO-01"


def _machine(serial: str) -> Machine:
    return db.session.scalars(select(Machine).where(Machine.serial_number == serial)).one()


def _session(machine: Machine, **fields) -> DeviceSession:
    row = DeviceSession(session_id=uuid.uuid4(), machine_id=machine.id, first_received_at=utcnow(), **fields)
    db.session.add(row)
    db.session.flush()
    return row


def _job(machine: Machine, session: DeviceSession, seq: int = 1, event: str = "arrived", **fields) -> DetectionJob:
    event_id = fields.pop("event_id", uuid.uuid4())
    row = DetectionJob(machine_id=machine.id, event_id=event_id, session_id=session.session_id, seq=seq,
                       event=event, received_at=utcnow(), s3_key=f"inbox/m-{machine.id}/{event_id}.jpg", **fields)
    db.session.add(row)
    db.session.flush()
    return row


def _command(machine: Machine, session: DeviceSession, status: str = "pending", **fields) -> DeviceCommand:
    due = utcnow() + timedelta(seconds=60)
    fields.setdefault("type", CommandType.CAPTURE_CONFIRM)
    row = DeviceCommand(machine_id=machine.id, session_id=session.session_id, due_at=due,
                        expires_at=due + timedelta(minutes=9), status=status, **fields)
    db.session.add(row)
    db.session.flush()
    return row


def _rejected(action) -> bool:
    """Run action inside a savepoint; True if the database refused it."""
    try:
        with db.session.begin_nested():
            action()
    except IntegrityError:
        return True
    return False


# --- shape -------------------------------------------------------------------------------------

EXPECTED_COLUMNS = {
    "detection_jobs": {
        "id", "machine_id", "event_id", "session_id", "seq", "event", "detected_at", "received_at", "s3_key",
        "status", "publish_attempts", "next_publish_at", "published_at", "claim_attempt", "lease_until",
        "worker_id", "result_id", "violation_id", "effects_status", "last_error", "created_at", "updated_at",
        "prev_signal_at",   # SIGNAL_LOST snapshot taken at ingest (decision 2026-11-02)
        "command_id",       # the confirm command this photo fulfils: one command = one confirm job (decision 2026-11-02)
    },
    "detection_results": {
        "id", "job_id", "verdict", "window", "stickers", "elapsed_ms", "model_version", "applied", "created_at",
        # trace copied from the job so the result stays auditable after the job is deleted (30 d vs 90 d)
        "machine_id", "violation_id", "event_id", "session_id", "seq", "event", "received_at",
    },
    "device_sessions": {
        "session_id", "machine_id", "first_received_at", "status", "left_reported_at", "created_at", "updated_at",
    },
    "device_commands": {
        "command_id", "machine_id", "session_id", "type", "due_at", "expires_at", "status", "source_job_id",
        "created_at", "updated_at",
    },
}


def test_phase2_tables_have_the_designed_columns(app):
    with app.app_context():
        inspector = inspect(db.engine)
        for table, columns in EXPECTED_COLUMNS.items():
            assert {c["name"] for c in inspector.get_columns(table)} == columns, table
        machine_columns = {c["name"] for c in inspector.get_columns("machines")}
        assert {"current_session_id", "last_applied_seq"} <= machine_columns


def test_event_and_verdict_values_match_the_code_that_produces_them():
    from app.adapters.inference import NO_VEHICLE, PERMITTED, VIOLATION
    from app.models import DeviceEvent, Verdict
    from app.services.detections import EVENTS

    assert set(DeviceEvent.ALL) == set(EVENTS)
    assert set(Verdict.ALL) == {VIOLATION, PERMITTED, NO_VEHICLE}


# Decision 2026-11-02 (ADR 0019 change record): delete rule of every foreign key the Phase 2 tables add.
EXPECTED_ON_DELETE = {
    ("detection_jobs", "result_id"): "RESTRICT",
    ("detection_results", "job_id"): "SET NULL",
    ("machines", "current_session_id"): "RESTRICT",
    ("device_commands", "source_job_id"): "SET NULL",
    ("detection_jobs", "machine_id"): "RESTRICT",
    ("detection_jobs", "session_id"): "RESTRICT",
    ("detection_jobs", "violation_id"): "RESTRICT",
    ("detection_results", "machine_id"): "RESTRICT",
    ("detection_results", "violation_id"): "RESTRICT",
    ("device_sessions", "machine_id"): "RESTRICT",
    ("device_commands", "machine_id"): "RESTRICT",
    ("device_commands", "session_id"): "RESTRICT",
}


def test_foreign_key_delete_rules_match_the_decision(app):
    with app.app_context():
        inspector = inspect(db.engine)
        actual = {}
        for table in ("detection_jobs", "detection_results", "device_sessions", "device_commands", "machines"):
            for fk in inspector.get_foreign_keys(table):
                if (table, fk["constrained_columns"][0]) in EXPECTED_ON_DELETE:
                    actual[(table, fk["constrained_columns"][0])] = (fk.get("options") or {}).get("ondelete")
        assert actual == EXPECTED_ON_DELETE
        # results.session_id has no foreign key on purpose: sessions are deleted after 30 days, results kept 90
        assert not [fk for fk in inspector.get_foreign_keys("detection_results")
                    if fk["constrained_columns"] == ["session_id"]]


def test_existing_machines_start_with_no_session_and_seq_zero(app):
    with app.app_context():
        for machine in db.session.scalars(select(Machine)):
            assert machine.current_session_id is None
            assert machine.last_applied_seq == 0


def test_new_job_defaults_match_the_state_machine_entry_point(app):
    with app.app_context():
        machine = _machine(GANGNAM)
        job = _job(machine, _session(machine))
        db.session.refresh(job)
        assert isinstance(job.id, uuid.UUID)
        assert job.status == JobStatus.PENDING_PUBLISH
        assert job.publish_attempts == 0 and job.claim_attempt == 0
        assert job.result_id is None and job.violation_id is None and job.effects_status is None


# --- detection_jobs ----------------------------------------------------------------------------

def test_event_id_is_unique_per_machine(app):
    with app.app_context():
        gangnam, pangyo = _machine(GANGNAM), _machine(PANGYO)
        session = _session(gangnam)
        event_id = uuid.uuid4()
        _job(gangnam, session, event_id=event_id)
        assert _rejected(lambda: _job(gangnam, session, seq=2, event_id=event_id))
        # another machine may happen to use the same event_id
        assert not _rejected(lambda: _job(pangyo, _session(pangyo), event_id=event_id))


def test_a_command_is_linked_to_at_most_one_job_per_machine(app):
    """UNIQUE(machine_id, command_id) WHERE command_id IS NOT NULL: one command = one confirm job."""
    with app.app_context():
        gangnam, pangyo = _machine(GANGNAM), _machine(PANGYO)
        session = _session(gangnam)
        command_id = uuid.uuid4()
        _job(gangnam, session, seq=2, event="confirm", command_id=command_id)
        assert _rejected(lambda: _job(gangnam, session, seq=3, event="confirm", command_id=command_id))
        # the index is per machine, and a NULL link (every job without a command) never collides
        assert not _rejected(lambda: _job(pangyo, _session(pangyo), seq=2, event="confirm", command_id=command_id))
        for seq in (4, 5, 6):
            assert not _rejected(lambda: _job(gangnam, session, seq=seq, event="confirm"))


def test_the_command_link_has_no_foreign_key_so_retention_can_delete_commands_first(app):
    with app.app_context():
        columns = {fk["constrained_columns"][0] for fk in inspect(db.engine).get_foreign_keys("detection_jobs")}
        assert "command_id" not in columns


def test_the_command_index_is_partial_on_both_databases(app):
    with app.app_context():
        if db.engine.dialect.name == "postgresql":
            definition = db.session.execute(text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_detection_jobs_machine_command'")).scalar_one()
        else:
            definition = db.session.execute(text(
                "SELECT sql FROM sqlite_master WHERE name = 'uq_detection_jobs_machine_command'")).scalar_one()
        text_ = definition.lower()
        assert "unique" in text_ and "machine_id" in text_ and "command_id" in text_
        assert "where" in text_ and "command_id is not null" in text_.split("where")[1]


@pytest.mark.parametrize("field,value", [
    ("status", "finished"),
    ("event", "parked"),
    ("seq", 0),
    ("effects_status", "maybe"),
])
def test_job_columns_reject_values_outside_the_design(app, field, value):
    with app.app_context():
        machine = _machine(GANGNAM)
        session = _session(machine)
        fields = {field: value}
        if field == "seq":
            assert _rejected(lambda: _job(machine, session, seq=value))
        else:
            assert _rejected(lambda: _job(machine, session, **fields))


@pytest.mark.parametrize("status", JobStatus.ALL)
def test_every_designed_job_status_is_accepted(app, status):
    with app.app_context():
        machine = _machine(GANGNAM)
        assert not _rejected(lambda: _job(machine, _session(machine), status=status))


def test_effects_status_only_on_a_done_job_that_created_a_violation(app):
    """Design 6.7: effects_status exists only when the job is done and created a violation."""
    with app.app_context():
        machine = _machine(GANGNAM)
        session = _session(machine)
        violation = Violation(company_id=machine.parking_lot.company_id, parking_lot_id=machine.parking_lot_id,
                              machine_id=machine.id, detected_at=utcnow())
        db.session.add(violation)
        db.session.flush()

        assert _rejected(lambda: _job(machine, session, seq=1, effects_status="pending"))  # not done
        assert _rejected(lambda: _job(machine, session, seq=2, status="done", effects_status="pending"))  # no violation
        assert not _rejected(lambda: _job(machine, session, seq=3, status="done", violation_id=violation.id,
                                          effects_status="pending"))
        # done without effects (a verdict that did not create a violation) is the normal case
        assert not _rejected(lambda: _job(machine, session, seq=4, status="done"))


@pytest.mark.parametrize("status", ["ended", "superseded"])
def test_job_may_belong_to_an_older_session(app, status):
    """Design 6.5: a late photo of a finished or superseded session is stored and judged as stale.
    The required session foreign key must allow that (the session row still exists)."""
    with app.app_context():
        machine = _machine(GANGNAM)
        old = _session(machine, status=status)
        machine.current_session_id = _session(machine).session_id
        db.session.flush()
        assert not _rejected(lambda: _job(machine, old, seq=5, event="refresh", status="stale"))


def test_session_with_jobs_cannot_be_deleted(app):
    """A dead_lettered job is kept until resolved (design 6.12); a redrive needs its session for the order
    guard, so retention cleanup must not be able to delete the session first."""
    with app.app_context():
        machine = _machine(GANGNAM)
        session = _session(machine, status="ended")
        _job(machine, session, status="dead_lettered")
        db.session.commit()

        def delete_session():
            db.session.delete(db.session.get(DeviceSession, session.session_id))
            db.session.flush()

        assert _rejected(delete_session)


def test_job_must_belong_to_a_known_session(app):
    with app.app_context():
        machine = _machine(GANGNAM)
        ghost = DeviceSession(session_id=uuid.uuid4(), machine_id=machine.id, first_received_at=utcnow())
        # not added to the database
        assert _rejected(lambda: _job(machine, ghost))


# --- detection_results -------------------------------------------------------------------------

def _result(job: DetectionJob, verdict: str = "violation", applied: bool = True, **fields) -> DetectionResult:
    """A result as the worker will write it: the job's trace columns copied in the same transaction."""
    trace = {"machine_id": job.machine_id, "event_id": job.event_id, "session_id": job.session_id,
             "seq": job.seq, "event": job.event, "received_at": job.received_at}
    trace.update(fields)
    row = DetectionResult(job_id=job.id, verdict=verdict, window={"coords": [0, 0, 1, 1], "confidence": 0.9},
                          stickers=[], elapsed_ms=500, model_version="test", applied=applied, **trace)
    db.session.add(row)
    db.session.flush()
    return row


def _violation(machine: Machine) -> Violation:
    row = Violation(company_id=machine.parking_lot.company_id, parking_lot_id=machine.parking_lot_id,
                    machine_id=machine.id, detected_at=utcnow())
    db.session.add(row)
    db.session.flush()
    return row


def _delete_jobs(*job_ids) -> int:
    """Retention cleanup deletes with plain SQL (no ORM cascades): the database policy alone must hold."""
    return db.session.execute(delete(DetectionJob).where(DetectionJob.id.in_(job_ids))).rowcount


def test_one_result_per_job(app):
    with app.app_context():
        machine = _machine(GANGNAM)
        job = _job(machine, _session(machine))
        _result(job)
        assert _rejected(lambda: _result(job))


def test_result_verdict_must_be_a_known_verdict(app):
    with app.app_context():
        machine = _machine(GANGNAM)
        job = _job(machine, _session(machine))
        assert _rejected(lambda: _result(job, verdict="maybe"))
        for verdict in ("violation", "permitted", "no_vehicle"):
            other = _job(machine, job.session, seq=2 + ("violation", "permitted", "no_vehicle").index(verdict))
            assert not _rejected(lambda: _result(other, verdict=verdict))


def test_job_and_result_point_at_each_other(app):
    """detection_jobs.result_id and detection_results.job_id form a cycle; both must be storable."""
    with app.app_context():
        machine = _machine(GANGNAM)
        job = _job(machine, _session(machine))
        result = _result(job)
        job.result_id = result.id
        db.session.commit()
        stored = db.session.get(DetectionJob, job.id)
        assert stored.result.id == result.id and stored.result.job.id == job.id
        assert stored.result.window == {"coords": [0, 0, 1, 1], "confidence": 0.9}


def test_deleting_a_job_keeps_its_result_with_the_trace(app):
    """Decision 2026-11-02: jobs 30 d, results 90 d. The job goes, results.job_id becomes NULL, and the
    result can still be found by machine, event and violation."""
    with app.app_context():
        machine = _machine(GANGNAM)
        violation = _violation(machine)
        job = _job(machine, _session(machine), status="done", violation_id=violation.id)
        result = _result(job, violation_id=violation.id)
        job.result_id = result.id
        db.session.commit()
        event_id, session_id, result_id = job.event_id, job.session_id, result.id

        assert _delete_jobs(job.id) == 1
        db.session.commit()
        db.session.expire_all()

        kept = db.session.get(DetectionResult, result_id)
        assert kept.job_id is None
        assert (kept.machine_id, kept.event_id, kept.session_id, kept.seq, kept.event) == \
               (machine.id, event_id, session_id, 1, "arrived")
        assert kept.received_at is not None and kept.verdict == "violation" and kept.applied is True
        by_machine = db.session.scalars(select(DetectionResult).where(DetectionResult.machine_id == machine.id)).all()
        by_event = db.session.scalars(select(DetectionResult).where(DetectionResult.machine_id == machine.id,
                                                                    DetectionResult.event_id == event_id)).all()
        by_violation = db.session.scalars(
            select(DetectionResult).where(DetectionResult.violation_id == violation.id)).all()
        assert [r.id for r in by_machine] == [r.id for r in by_event] == [r.id for r in by_violation] == [result_id]


def test_result_stays_unique_per_event_after_its_job_is_gone(app):
    """job_id UNIQUE no longer helps once it is NULL (NULLs never collide). UNIQUE(machine_id, event_id) does."""
    with app.app_context():
        gangnam, pangyo = _machine(GANGNAM), _machine(PANGYO)
        job = _job(gangnam, _session(gangnam))
        _result(job)
        db.session.commit()
        event_id, session_id, received_at = job.event_id, job.session_id, job.received_at
        _delete_jobs(job.id)
        db.session.commit()

        def orphan_duplicate():
            db.session.add(DetectionResult(job_id=None, verdict="violation", stickers=[], elapsed_ms=1, applied=False,
                                           machine_id=gangnam.id, event_id=event_id, session_id=session_id, seq=1,
                                           event="arrived", received_at=received_at))
            db.session.flush()

        assert _rejected(orphan_duplicate)
        # the same event_id on another machine is a different event
        other = _job(pangyo, _session(pangyo), event_id=event_id)
        assert not _rejected(lambda: _result(other))


def test_result_trace_columns_follow_the_job_rules(app):
    with app.app_context():
        machine = _machine(GANGNAM)
        session = _session(machine)
        assert _rejected(lambda: _result(_job(machine, session, seq=1), seq=0))
        assert _rejected(lambda: _result(_job(machine, session, seq=2), event="parked"))
        assert _rejected(lambda: _result(_job(machine, session, seq=3), machine_id=999999))


def test_result_names_a_violation_only_when_it_changed_the_episode(app):
    """A stale result (applied = False) never touched an episode, so it cannot name a violation."""
    with app.app_context():
        machine = _machine(GANGNAM)
        violation = _violation(machine)
        session = _session(machine)
        assert _rejected(lambda: _result(_job(machine, session, seq=1, status="stale"), applied=False,
                                         violation_id=violation.id))
        assert not _rejected(lambda: _result(_job(machine, session, seq=2, status="done"), applied=True,
                                             violation_id=violation.id))
        assert not _rejected(lambda: _result(_job(machine, session, seq=3, status="stale"), applied=False))
        assert _rejected(lambda: _result(_job(machine, session, seq=4, status="done"), violation_id=999999))


def test_result_cannot_be_deleted_while_its_job_exists(app):
    """jobs.result_id is RESTRICT: a done job must never silently lose its result."""
    with app.app_context():
        machine = _machine(GANGNAM)
        job = _job(machine, _session(machine), status="done")
        result = _result(job)
        job.result_id = result.id
        db.session.commit()

        def delete_result():
            db.session.execute(delete(DetectionResult).where(DetectionResult.id == result.id))

        assert _rejected(delete_result)
        db.session.expire_all()
        assert db.session.get(DetectionJob, job.id).result_id == result.id


def test_retention_cleanup_is_not_blocked_by_a_long_lived_dead_letter(app):
    """Design 6.12 cleanup in order: commands (30 d) -> jobs (30 d, not dead_lettered) -> sessions (30 d, no jobs,
    not current) -> results (90 d). A dead_lettered job stays for months; nothing else may be stuck behind it."""
    with app.app_context():
        machine = _machine(GANGNAM)
        violation = _violation(machine)
        old = _session(machine, status="ended")
        done_job = _job(machine, old, status="done", violation_id=violation.id, effects_status="done")
        done_result = _result(done_job, violation_id=violation.id)
        done_job.result_id = done_result.id
        command = _command(machine, old, status="fulfilled", source_job_id=done_job.id)
        stale_job = _job(machine, old, seq=2, event="refresh", status="stale")
        stale_result = _result(stale_job, applied=False)
        stale_job.result_id = stale_result.id

        dead_session = _session(machine, status="ended")
        dead_job = _job(machine, dead_session, status="dead_lettered", last_error="InferenceError x3")

        current = _session(machine)
        current_job = _job(machine, current, status="done")
        current_result = _result(current_job)
        current_job.result_id = current_result.id
        machine.current_session_id = current.session_id
        machine.last_applied_seq = 1
        db.session.commit()

        # 1. commands
        assert db.session.execute(delete(DeviceCommand).where(
            DeviceCommand.command_id == command.command_id)).rowcount == 1
        # 2. jobs, except dead_lettered: results lose job_id, keep their trace
        removed = db.session.execute(delete(DetectionJob).where(
            DetectionJob.status != JobStatus.DEAD_LETTERED)).rowcount
        assert removed == 3
        db.session.commit()
        db.session.expire_all()
        assert db.session.scalars(select(DetectionResult.job_id)).all() == [None, None, None]

        # 3. sessions: the dead letter's session and the current session must stay; the old one goes
        assert _rejected(lambda: db.session.execute(
            delete(DeviceSession).where(DeviceSession.session_id == dead_session.session_id)))
        assert _rejected(lambda: db.session.execute(
            delete(DeviceSession).where(DeviceSession.session_id == current.session_id)))
        assert db.session.execute(delete(DeviceSession).where(
            DeviceSession.session_id == old.session_id)).rowcount == 1
        db.session.commit()

        # 4. results (90 d): nothing points at them any more, the dead letter does not block them
        assert db.session.execute(delete(DetectionResult)).rowcount == 3
        db.session.commit()
        db.session.expire_all()

        assert db.session.get(DetectionJob, dead_job.id).status == JobStatus.DEAD_LETTERED
        assert db.session.get(DeviceSession, dead_session.session_id) is not None
        stored = db.session.get(Machine, machine.id)
        assert stored.current_session_id == current.session_id and stored.last_applied_seq == 1
        assert db.session.get(Violation, violation.id) is not None   # violations are never deleted


# --- device_sessions and machines ---------------------------------------------------------------

def test_session_status_values(app):
    with app.app_context():
        machine = _machine(GANGNAM)
        for status in SessionStatus.ALL:
            assert not _rejected(lambda: _session(machine, status=status))
        assert _rejected(lambda: _session(machine, status="paused"))
        assert _session(machine).status == SessionStatus.ACTIVE


def test_machine_current_session_must_exist(app):
    with app.app_context():
        machine = _machine(GANGNAM)
        session = _session(machine)

        def point_at(value):
            machine.current_session_id = value
            db.session.flush()

        assert _rejected(lambda: point_at(uuid.uuid4()))
        db.session.refresh(machine)
        assert not _rejected(lambda: point_at(session.session_id))
        machine.last_applied_seq = 3
        db.session.commit()
        stored = db.session.get(Machine, machine.id)
        assert stored.current_session.session_id == session.session_id and stored.last_applied_seq == 3


# --- device_commands ----------------------------------------------------------------------------

def test_one_active_command_per_machine_session_and_type(app):
    """Design 6.10: UNIQUE(machine, session, type) WHERE status IN (pending, delivered)."""
    with app.app_context():
        machine = _machine(GANGNAM)
        session = _session(machine)
        first = _command(machine, session, status="pending")
        assert _rejected(lambda: _command(machine, session, status="pending"))
        assert _rejected(lambda: _command(machine, session, status="delivered"))

        first.status = CommandStatus.FULFILLED
        db.session.flush()
        assert not _rejected(lambda: _command(machine, session, status="pending"))  # the old one is no longer active
        # finished commands never block each other
        for status in ("fulfilled", "cancelled", "expired", "cancelled"):
            assert not _rejected(lambda: _command(machine, session, status=status))
        # a different session is independent
        assert not _rejected(lambda: _command(machine, _session(machine), status="pending"))


def test_command_type_and_status_values(app):
    with app.app_context():
        machine = _machine(GANGNAM)
        session = _session(machine)
        assert _rejected(lambda: _command(machine, session, status="sent"))
        assert _rejected(lambda: _command(machine, session, type="reboot"))
        command = _command(machine, session)
        assert isinstance(command.command_id, uuid.UUID) and command.status == CommandStatus.PENDING
        assert command.type == CommandType.CAPTURE_CONFIRM


def test_command_may_reference_its_source_job(app):
    with app.app_context():
        machine = _machine(GANGNAM)
        session = _session(machine)
        job = _job(machine, session)
        command = _command(machine, session, source_job_id=job.id)
        db.session.commit()
        assert db.session.get(DeviceCommand, command.command_id).source_job.id == job.id


# --- Phase 1 compatibility ----------------------------------------------------------------------

def test_phase1_sync_upload_still_works_and_writes_no_phase2_rows(app, upload, client, auth):
    response = upload(GANGNAM)
    assert response.status_code == 201, response.get_json()
    with app.app_context():
        for model in (DetectionJob, DetectionResult, DeviceSession, DeviceCommand):
            assert db.session.scalars(select(model)).first() is None
        assert _machine(GANGNAM).current_session_id is None
    assert client.get(f"{API}/violations", headers=auth("kim@a.com")).get_json()["items"]


# --- PostgreSQL-only details ---------------------------------------------------------------------

def _is_postgres(app) -> bool:
    with app.app_context():
        return db.engine.dialect.name == "postgresql"


def test_postgres_uses_native_uuid_and_a_partial_unique_index(app):
    if not _is_postgres(app):
        pytest.skip("PostgreSQL only (set TEST_DATABASE_URL)")
    with app.app_context():
        uuid_columns = {"detection_jobs.id", "detection_jobs.event_id", "detection_jobs.session_id",
                        "detection_results.job_id", "detection_results.event_id", "detection_results.session_id",
"device_sessions.session_id", "device_commands.command_id",
                        "device_commands.session_id", "device_commands.source_job_id", "machines.current_session_id"}
        types = dict(db.session.execute(text(
            "SELECT table_name || '.' || column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema()")).all())
        assert {name: types[name] for name in uuid_columns} == {name: "uuid" for name in uuid_columns}
        indexdef = db.session.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_device_commands_active'")).scalar_one()
        assert "UNIQUE" in indexdef and "WHERE" in indexdef
        assert "pending" in indexdef and "delivered" in indexdef


def test_postgres_rejects_duplicate_active_command_from_another_connection(app):
    """A worker redelivery on another connection must not create a second active confirm command (6.13).

    Sequential, not a true race: the first transaction commits before the second inserts.
    """
    if not _is_postgres(app):
        pytest.skip("PostgreSQL only (set TEST_DATABASE_URL)")
    with app.app_context():
        machine = _machine(GANGNAM)
        session = _session(machine)
        db.session.commit()
        machine_id, session_id = machine.id, session.session_id
        due = utcnow() + timedelta(seconds=60)
        insert = text("INSERT INTO device_commands (command_id, machine_id, session_id, type, due_at, expires_at, "
                      "status, created_at, updated_at) VALUES (:c, :m, :s, 'capture_confirm', :d, :e, 'pending', "
                      ":d, :d)")
        params = {"m": machine_id, "s": session_id, "d": due, "e": due + timedelta(minutes=9)}
        first = db.engine.connect()
        second = db.engine.connect()
        try:
            first.begin()
            first.execute(insert, {**params, "c": uuid.uuid4()})
            first.commit()
            second.begin()
            with pytest.raises(IntegrityError):
                second.execute(insert, {**params, "c": uuid.uuid4()})
            second.rollback()
        finally:
            first.close()
            second.close()
