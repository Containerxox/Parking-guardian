"""The command-link revision (detection_jobs.command_id + UNIQUE(machine_id, command_id) WHERE command_id IS NOT NULL).

Same harness as test_phase2_migration.py: SQLite always, PostgreSQL when TEST_MIGRATION_DATABASE_URL is set.
Checks that existing jobs keep their data and get a NULL link, the index behaves the same on both databases, the
revision undoes cleanly, and a second upgrade works.
"""
import uuid

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.extensions import db

from .test_phase2_migration import (COMMAND_LINK_REVISION, EVENT, JOB, MIGRATIONS, PREV_SIGNAL_REVISION, SEED,  # noqa: F401
                                    SESSION, _revision, _schema_diff, _snapshot, _uuid, migration_app)

TS = "'2026-11-02 00:00:00+00'"
INDEX = "uq_detection_jobs_machine_command"
OTHER_SESSION = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _insert_job(conn, machine_id, session, seq, event="confirm", command_id=None, job_id=None, event_id=None):
    conn.execute(text(
        "INSERT INTO detection_jobs (id, machine_id, event_id, session_id, seq, event, received_at, s3_key, status, "
        f"publish_attempts, claim_attempt, {'command_id, ' if command_id else ''}created_at, updated_at) VALUES "
        f"(:j, :m, :e, :s, :q, :ev, {TS}, 'inbox/x', 'queued', 1, 0, {':c, ' if command_id else ''}{TS}, {TS})"),
        {"j": _uuid(job_id or str(uuid.uuid4())), "m": machine_id, "e": _uuid(event_id or str(uuid.uuid4())),
         "s": _uuid(session), "q": seq, "ev": event, **({"c": _uuid(command_id)} if command_id else {})})


def _job_count():
    with db.engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM detection_jobs")).scalar_one()


def _index_names():
    return {i["name"] for i in inspect(db.engine).get_indexes("detection_jobs")}


def test_command_link_revision_round_trips_and_the_index_behaves_the_same_everywhere(migration_app):
    upgrade(directory=MIGRATIONS, revision=PREV_SIGNAL_REVISION)
    with db.engine.begin() as conn:
        for statement in SEED:
            conn.execute(text(statement))
        conn.execute(text(f"INSERT INTO device_sessions VALUES (:s, 1, {TS}, 'active', NULL, {TS}, {TS})"),
                     {"s": _uuid(SESSION)})
        conn.execute(text(f"INSERT INTO device_sessions VALUES (:s, 2, {TS}, 'active', NULL, {TS}, {TS})"),
                     {"s": _uuid(OTHER_SESSION)})
        _insert_job(conn, 1, SESSION, 1, "arrived", job_id=JOB, event_id=EVENT)
        _insert_job(conn, 1, SESSION, 2, "confirm")
    before = _snapshot()
    assert _job_count() == 2 and INDEX not in _index_names()

    upgrade(directory=MIGRATIONS, revision=COMMAND_LINK_REVISION)

    assert _revision() == COMMAND_LINK_REVISION
    assert _snapshot() == before                                           # Phase 1 tables untouched
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM detection_jobs WHERE command_id IS NULL")).scalar_one() == 2
    assert INDEX in _index_names()
    assert _schema_diff() == []                                            # the migration produces what the models say

    command = str(uuid.uuid4())
    with db.engine.begin() as conn:
        _insert_job(conn, 1, SESSION, 3, command_id=command)               # a first job for the command
        _insert_job(conn, 1, SESSION, 4)                                   # NULL links never collide
        _insert_job(conn, 1, SESSION, 5)
        _insert_job(conn, 2, OTHER_SESSION, 1, command_id=command)         # the index is per machine
    with pytest.raises(IntegrityError):
        with db.engine.begin() as conn:
            _insert_job(conn, 1, SESSION, 6, command_id=command)           # a second job for the same command
    assert _job_count() == 6

    downgrade(directory=MIGRATIONS, revision=PREV_SIGNAL_REVISION)
    assert _revision() == PREV_SIGNAL_REVISION
    assert "command_id" not in {c["name"] for c in inspect(db.engine).get_columns("detection_jobs")}
    assert INDEX not in _index_names()
    assert _job_count() == 6                                                # jobs are kept, only the link is gone
    assert _snapshot() == before

    upgrade(directory=MIGRATIONS)
    assert _revision() == COMMAND_LINK_REVISION
    assert INDEX in _index_names() and _schema_diff() == []
    with db.engine.connect() as conn:                                       # the old rows come back with no link
        assert conn.execute(text("SELECT count(*) FROM detection_jobs WHERE command_id IS NULL")).scalar_one() == 6


def test_the_command_link_revision_is_the_head(migration_app):
    """Anything newer would need this test (and the downgrade-refusal states in test_phase2_migration) revisited."""
    upgrade(directory=MIGRATIONS)
    assert _revision() == COMMAND_LINK_REVISION
