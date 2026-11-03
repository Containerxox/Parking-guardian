"""Phase 2 confirm commands carried by the heartbeat (ADR 0019 decision 8; design 6.9, 6.10).

    POST /device/heartbeat   {session_id?, occupied?, last_seq?, acked_commands?}   (body optional: Phase 1 sends none)
    200 {ok, server_time, violation_active, commands: [{command_id, type, session_id, due_at, expires_at}]}

Lifecycle: pending (worker) -> delivered (put in a heartbeat response; repeated on every heartbeat until done,
so a lost response loses nothing) -> fulfilled (a confirm photo naming it was received; set by ingest)
| cancelled (session ended / superseded, or the device acked it as not executable) | expired (expires_at passed).
Only commands of THIS machine and of the session the device reports, while that session is active, go out.
"""
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.cli import dev_api_key
from app.extensions import db
from app.models import CommandStatus, CommandType, DeviceCommand, DeviceSession, Violation, as_utc, iso, utcnow

from .conftest import API
from .test_worker import (GANGNAM, PANGYO, _machine, _new_violations, app_overrides, clock, drain,  # noqa: F401
                          env, send, worker)


def beat(client, serial=GANGNAM, session=None, acked=None, **extra):
    body = {**extra}
    if session is not None:
        body["session_id"] = str(session)
    if acked is not None:
        body["acked_commands"] = [str(a) for a in acked]
    return client.post(f"{API}/device/heartbeat", json=body, headers={"X-Device-Key": dev_api_key(serial)})


def _command(app, session):
    with app.app_context():
        return db.session.scalars(select(DeviceCommand).where(DeviceCommand.session_id == session)).one()


def _status(app, command_id):
    with app.app_context():
        return db.session.get(DeviceCommand, command_id).status


@pytest.fixture
def pending_command(app, send, worker):
    """A real confirm command: an arrived photo judged violation_pending in an active session."""
    session = uuid.uuid4()
    send(session=session, seq=1)
    drain(app, worker)
    command = _command(app, session)
    assert command.status == CommandStatus.PENDING
    return session, command.command_id


# --- Phase 1 compatibility -----------------------------------------------------------------------------

def test_phase1_heartbeat_without_a_body_still_works(app, client):
    response = client.post(f"{API}/device/heartbeat", headers={"X-Device-Key": dev_api_key(GANGNAM)})
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True and isinstance(body["server_time"], str)
    assert body["commands"] == [] and body["violation_active"] in (True, False)   # additive fields only


def test_heartbeat_without_a_session_gets_no_commands_and_leaves_them_pending(app, client, pending_command):
    session, command_id = pending_command
    assert beat(client).get_json()["commands"] == []
    assert _status(app, command_id) == CommandStatus.PENDING


# --- delivery -------------------------------------------------------------------------------------------

def test_command_of_the_reported_session_is_delivered(app, client, pending_command):
    session, command_id = pending_command
    [command] = beat(client, session=session).get_json()["commands"]
    with app.app_context():
        stored = db.session.get(DeviceCommand, command_id)
        assert command == {"command_id": str(command_id), "type": CommandType.CAPTURE_CONFIRM,
                           "session_id": str(session), "due_at": iso(stored.due_at),
                           "expires_at": iso(stored.expires_at)}
        assert stored.status == CommandStatus.DELIVERED   # in a response: delivered, NOT executed


def test_command_is_repeated_until_fulfilled_so_a_lost_response_loses_nothing(app, client, send, pending_command):
    session, command_id = pending_command
    first = beat(client, session=session).get_json()["commands"]
    second = beat(client, session=session).get_json()["commands"]   # e.g. the first response never arrived
    assert first == second and len(second) == 1
    assert _status(app, command_id) == CommandStatus.DELIVERED

    send(session=session, seq=2, event="confirm", command_id=command_id)   # the device executed it
    assert _status(app, command_id) == CommandStatus.FULFILLED
    assert beat(client, session=session).get_json()["commands"] == []


def test_fulfilment_is_only_by_the_confirm_photo_not_by_delivery(app, client, pending_command):
    session, command_id = pending_command
    for _ in range(3):
        beat(client, session=session)
    assert _status(app, command_id) == CommandStatus.DELIVERED


def test_command_of_another_session_is_not_delivered(app, client, pending_command):
    session, command_id = pending_command
    assert beat(client, session=uuid.uuid4()).get_json()["commands"] == []
    assert _status(app, command_id) == CommandStatus.PENDING


def test_ended_session_command_is_cancelled_and_never_delivered(app, client, send, pending_command):
    session, command_id = pending_command
    send(session=session, seq=2, event="left", verdict="novehicle")      # ingest cancels the session's commands
    assert _status(app, command_id) == CommandStatus.CANCELLED
    assert beat(client, session=session).get_json()["commands"] == []


def test_superseded_session_command_never_reaches_the_new_session(app, client, send, pending_command):
    old, command_id = pending_command
    new = uuid.uuid4()
    send(session=new, seq=1, verdict="novehicle")                          # reboot: a new session starts
    assert _status(app, command_id) == CommandStatus.CANCELLED
    assert beat(client, session=new).get_json()["commands"] == []
    assert beat(client, session=old).get_json()["commands"] == []          # the old session is no longer active


def test_active_command_of_an_ended_session_is_still_not_delivered(app, client, pending_command):
    """Defence in depth. Normally impossible (worker and ingest both lock the machine row, so a command is never
    created for a session that ingest has just ended), but if such a row existed it must not go out."""
    session, command_id = pending_command
    with app.app_context():
        db.session.execute(update(DeviceSession).where(DeviceSession.session_id == session)
                           .values(status="ended"))   # command left pending on purpose
        db.session.commit()
    assert beat(client, session=session).get_json()["commands"] == []
    assert _status(app, command_id) == CommandStatus.PENDING


def test_expired_command_is_marked_and_not_delivered(app, client, pending_command):
    session, command_id = pending_command
    with app.app_context():
        db.session.execute(update(DeviceCommand).where(DeviceCommand.command_id == command_id)
                           .values(expires_at=utcnow() - timedelta(seconds=1)))
        db.session.commit()
    assert beat(client, session=session).get_json()["commands"] == []
    assert _status(app, command_id) == CommandStatus.EXPIRED


def test_acked_command_is_cancelled_and_others_machines_commands_are_untouched(app, client, send, worker,
                                                                              pending_command):
    session, command_id = pending_command
    pangyo_session = uuid.uuid4()
    send(PANGYO, session=pangyo_session, seq=1)
    drain(app, worker)
    pangyo_command = _command(app, pangyo_session).command_id

    response = beat(client, session=session, acked=[command_id, pangyo_command])
    assert response.status_code == 200 and response.get_json()["commands"] == []
    assert _status(app, command_id) == CommandStatus.CANCELLED
    assert _status(app, pangyo_command) == CommandStatus.PENDING   # GANGNAM cannot touch PANGYO's command


def test_commands_never_cross_machines(app, client, pending_command):
    session, command_id = pending_command
    # PANGYO reports GANGNAM's session id: nothing is delivered and nothing changes
    assert beat(client, PANGYO, session=session).get_json()["commands"] == []
    assert _status(app, command_id) == CommandStatus.PENDING


def test_a_second_pending_verdict_does_not_create_a_second_active_command(app, client, send, worker,
                                                                         pending_command):
    session, command_id = pending_command
    send(session=session, seq=2, event="refresh", verdict="novehicle")     # resets the count
    send(session=session, seq=3, event="refresh")                          # pending again
    drain(app, worker)
    with app.app_context():
        commands = db.session.scalars(select(DeviceCommand).where(DeviceCommand.session_id == session)).all()
        assert [c.command_id for c in commands] == [command_id]           # the active one blocks a duplicate
    [delivered] = beat(client, session=session).get_json()["commands"]
    assert delivered["command_id"] == str(command_id)


# --- violation_active (LED) ----------------------------------------------------------------------------

def test_violation_active_follows_the_open_episode_and_the_left_report(app, client, send, worker):
    session = uuid.uuid4()
    send(session=session, seq=1)
    send(session=session, seq=2, event="confirm")
    drain(app, worker)
    assert beat(client, session=session).get_json()["violation_active"] is True
    send(session=session, seq=3, event="left", verdict="novehicle")       # not judged yet: episode still open
    with app.app_context():
        assert _new_violations(_machine().id)[0].ended_at is None
    assert beat(client, session=session).get_json()["violation_active"] is False   # design 6.2: left reported


# --- validation ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"session_id": "not-a-uuid"},
    {"session_id": 123},
    {"acked_commands": "x"},
    {"acked_commands": ["not-a-uuid"]},
])
def test_malformed_phase2_fields_are_400(app, client, body):
    response = client.post(f"{API}/device/heartbeat", json=body, headers={"X-Device-Key": dev_api_key(GANGNAM)})
    assert response.status_code == 400


def test_unknown_fields_are_ignored(app, client, pending_command):
    session, _ = pending_command
    response = beat(client, session=session, occupied=True, last_seq=1, something_new="x")
    assert response.status_code == 200 and len(response.get_json()["commands"]) == 1


# --- PostgreSQL: races ---------------------------------------------------------------------------------

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


def test_postgres_concurrent_heartbeats_all_carry_the_command(app, pending_command):
    _require_postgres(app)
    session, command_id = pending_command
    responses = _parallel(app, [lambda: beat(app.test_client(), session=session) for _ in range(5)])
    assert [r.status_code for r in responses] == [200] * 5
    assert all([c["command_id"] for c in r.get_json()["commands"]] == [str(command_id)] for r in responses)
    assert _status(app, command_id) == CommandStatus.DELIVERED


def test_postgres_heartbeat_racing_a_cancel_never_revives_the_command(app, send):
    """A heartbeat delivering while ingest cancels (left): the command must end cancelled, never delivered."""
    _require_postgres(app)
    from app.services.worker import JudgementWorker

    for _ in range(5):
        session = uuid.uuid4()
        send(session=session, seq=1)
        with app.app_context():
            assert JudgementWorker(worker_id="w").run_once() == "done"
        command_id = _command(app, session).command_id
        _parallel(app, [lambda: beat(app.test_client(), session=session),
                        lambda: send(session=session, seq=2, event="left", verdict="novehicle")])
        assert _status(app, command_id) == CommandStatus.CANCELLED
        with app.app_context():
            from app.adapters import queue

            q = queue()
            while (messages := q.receive()):
                for m in messages:
                    JudgementWorker(worker_id="w").process(m)


def test_postgres_worker_creating_a_command_while_heartbeats_arrive(app, send):
    """Heartbeats keep coming while the worker judges and creates the command: no deadlock, exactly one command,
    and it is delivered by a heartbeat once it is committed."""
    _require_postgres(app)
    from app.services.worker import JudgementWorker

    session = uuid.uuid4()
    send(session=session, seq=1)
    stop = threading.Event()
    seen = []

    def heartbeats():
        client = app.test_client()
        while not stop.is_set():
            seen.extend(beat(client, session=session).get_json()["commands"])

    thread = threading.Thread(target=heartbeats)
    thread.start()
    try:
        with app.app_context():
            assert JudgementWorker(worker_id="w").run_once() == "done"
    finally:
        threading.Event().wait(0.5)
        stop.set()
        thread.join(timeout=15)
    command = _command(app, session)
    assert command.status == CommandStatus.DELIVERED
    assert {c["command_id"] for c in seen} == {str(command.command_id)}
