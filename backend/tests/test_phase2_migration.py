"""The Phase 2 migration on a database that already holds Phase 1 data.

Checks: upgrade keeps every existing row unchanged, the migrated schema equals the models, downgrade
returns to the previous revision with the same rows, and upgrade works again after that.

SQLite always runs (temporary file). PostgreSQL runs when TEST_MIGRATION_DATABASE_URL points at a
database made only for this test; the test wipes its public schema:

    set TEST_MIGRATION_DATABASE_URL=postgresql+psycopg://parking:parking_dev_password@127.0.0.1:5432/parking_guardian_migtest

It does not use TEST_DATABASE_URL because the other tests create tables without migrations there.
"""
import os
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, text

from app import create_app
from app.extensions import db

MIGRATIONS = str(Path(__file__).resolve().parent.parent / "migrations")
PREVIOUS = "a3c7e51d9b02"   # last Phase 1 revision
PHASE2_TABLES_REVISION = "c5e8a1f3b7d2"   # Phase 2 tables (applied to the local dev database on 2026-11-02)
RESULT_TRACE_REVISION = "d8f2b6c4a913"    # result trace columns + delete rules (decision 2026-11-02)
PHASE2_TABLES = {"detection_jobs", "detection_results", "device_sessions", "device_commands"}

# Rows as Phase 1 left them: one machine in the middle of counting an unconfirmed sighting and one open episode.
SEED = [
    "INSERT INTO companies (id, name, status, created_at, updated_at) "
    "VALUES (1, 'A', 'ACTIVE', '2026-10-01 00:00:00+00', '2026-10-01 00:00:00+00')",
    "INSERT INTO parking_lots (id, company_id, name, address, status, created_at, updated_at) "
    "VALUES (1, 1, 'lot', 'addr', 'ACTIVE', '2026-10-01 00:00:00+00', '2026-10-01 00:00:00+00')",
    "INSERT INTO machines (id, parking_lot_id, serial_number, name, status, api_key_hash, installed_at, "
    "last_heartbeat_at, last_detection_at, pending_violation_count, pending_since, clear_count, created_at, updated_at) "
    "VALUES (1, 1, 'RPI-1', 'pi', 'ACTIVE', 'hash1', '2026-10-01 00:00:00+00', '2026-10-30 12:00:00+00', "
    "'2026-10-30 12:00:00+00', 1, '2026-10-30 11:59:00+00', 0, '2026-10-01 00:00:00+00', '2026-10-30 12:00:00+00')",
    "INSERT INTO machines (id, parking_lot_id, serial_number, name, status, api_key_hash, installed_at, "
    "created_at, updated_at) VALUES (2, 1, 'RPI-2', 'pi2', 'ACTIVE', 'hash2', '2026-10-01 00:00:00+00', "
    "'2026-10-01 00:00:00+00', '2026-10-01 00:00:00+00')",
    "INSERT INTO violations (id, company_id, parking_lot_id, machine_id, detected_at, status, last_seen_at, "
    "detection_count, image_key, image_status, created_at, updated_at) VALUES (1, 1, 1, 1, "
    "'2026-10-30 11:00:00+00', 'NOTIFIED', '2026-10-30 11:30:00+00', 3, 'violations/x.jpg', 'UPLOADED', "
    "'2026-10-30 11:00:00+00', '2026-10-30 11:30:00+00')",
]
PHASE1_COLUMNS = {
    "machines": "id, parking_lot_id, serial_number, name, status, api_key_hash, installed_at, last_heartbeat_at, "
                "last_detection_at, pending_violation_count, pending_since, clear_count, created_at, updated_at",
    "violations": "id, company_id, parking_lot_id, machine_id, detected_at, status, last_seen_at, detection_count, "
                  "ended_at, end_reason, image_key, image_status, image_expires_at, plate_number, plate_confidence, "
                  "created_at, updated_at",
    "companies": "id, name, status, created_at, updated_at",
    "parking_lots": "id, company_id, name, address, status, created_at, updated_at",
}


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_app(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    else:
        url = os.environ.get("TEST_MIGRATION_DATABASE_URL")
        if not url:
            pytest.skip("set TEST_MIGRATION_DATABASE_URL to run on PostgreSQL")
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url, "INFERENCE_BACKEND": "mock",
                      "JWT_SECRET": "test-secret-with-enough-length-for-hs256"})
    with app.app_context():
        if db.engine.dialect.name == "postgresql":
            with db.engine.begin() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))
        yield app
        db.session.remove()
        db.engine.dispose()


def _snapshot():
    with db.engine.connect() as conn:
        return {table: [tuple(row) for row in conn.execute(text(f"SELECT {cols} FROM {table} ORDER BY id"))]
                for table, cols in PHASE1_COLUMNS.items()}


def _revision():
    with db.engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _schema_diff():
    with db.engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(context, db.metadata)


def _diff_names(diff):
    """Readable names of what still differs between a partly migrated database and today's models."""
    names = set()
    for entry in diff:
        if isinstance(entry, tuple) and entry[0] == "add_column":
            names.add(f"add_column:{entry[2]}.{entry[3].name}")
        elif isinstance(entry, tuple) and entry[0] == "add_index":
            names.add(f"add_index:{entry[1].name}")
        else:
            names.add(repr(entry)[:80])
    return names


# What the command-link revision (below) adds on top of the prev_signal_at revision.
COMMAND_LINK_DIFF = {"add_column:detection_jobs.command_id", "add_index:uq_detection_jobs_machine_command"}


def test_upgrade_keeps_phase1_rows_and_matches_the_models(migration_app):
    upgrade(directory=MIGRATIONS, revision=PREVIOUS)
    with db.engine.begin() as conn:
        for statement in SEED:
            conn.execute(text(statement))
    before = _snapshot()

    upgrade(directory=MIGRATIONS)

    assert _revision() != PREVIOUS
    assert _snapshot() == before
    inspector = inspect(db.engine)
    assert PHASE2_TABLES <= set(inspector.get_table_names())
    with db.engine.connect() as conn:
        rows = conn.execute(text("SELECT id, current_session_id, last_applied_seq FROM machines ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [(1, None, 0), (2, None, 0)]
    assert _schema_diff() == []


def test_downgrade_returns_to_phase1_with_the_same_rows_and_upgrade_again(migration_app):
    upgrade(directory=MIGRATIONS, revision=PREVIOUS)
    with db.engine.begin() as conn:
        for statement in SEED:
            conn.execute(text(statement))
    before = _snapshot()
    upgrade(directory=MIGRATIONS)
    # Phase 2 rows exist at downgrade time; they belong to the new tables and are dropped with them.
    with db.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO device_sessions (session_id, machine_id, first_received_at, status, created_at, updated_at) "
            "VALUES (:s, 1, '2026-11-02 00:00:00+00', 'active', '2026-11-02 00:00:00+00', '2026-11-02 00:00:00+00')"),
            {"s": "0b4f6f3c9d2e4a1b8c7d6e5f4a3b2c1d" if db.engine.dialect.name == "sqlite"
             else "0b4f6f3c-9d2e-4a1b-8c7d-6e5f4a3b2c1d"})
        conn.execute(text("UPDATE machines SET current_session_id = (SELECT session_id FROM device_sessions), "
                          "last_applied_seq = 2 WHERE id = 1"))

    downgrade(directory=MIGRATIONS, revision=PREVIOUS)

    assert _revision() == PREVIOUS
    assert _snapshot() == before
    inspector = inspect(db.engine)
    assert not PHASE2_TABLES & set(inspector.get_table_names())
    machine_columns = {c["name"] for c in inspector.get_columns("machines")}
    assert not {"current_session_id", "last_applied_seq"} & machine_columns

    upgrade(directory=MIGRATIONS)
    assert _snapshot() == before
    assert _schema_diff() == []


def _uuid(value: str):
    """Literal for a Uuid column written with raw SQL: SQLite stores 32 hex chars, PostgreSQL a native uuid."""
    return value.replace("-", "") if db.engine.dialect.name == "sqlite" else value


SESSION = "0b4f6f3c-9d2e-4a1b-8c7d-6e5f4a3b2c1d"
JOB = "1c5a7b9d-2e4f-4a6b-8c0d-1e2f3a4b5c6d"
EVENT = "2d6b8cae-3f50-4b7c-9d1e-2f3a4b5c6d7e"


def _seed_phase2_rows_at_tables_revision():
    """One session, one done job that created violation 1, and its result, as written under c5e8a1f3b7d2."""
    ts = "'2026-11-02 00:00:00+00'"
    with db.engine.begin() as conn:
        for statement in SEED:
            conn.execute(text(statement))
        conn.execute(text(f"INSERT INTO device_sessions VALUES (:s, 1, {ts}, 'active', NULL, {ts}, {ts})"),
                     {"s": _uuid(SESSION)})
        conn.execute(text(
            "INSERT INTO detection_jobs (id, machine_id, event_id, session_id, seq, event, received_at, s3_key, "
            f"status, publish_attempts, claim_attempt, violation_id, effects_status, created_at, updated_at) VALUES "
            f"(:j, 1, :e, :s, 3, 'confirm', {ts}, 'inbox/m-1/x.jpg', 'done', 1, 1, 1, 'done', {ts}, {ts})"),
            {"j": _uuid(JOB), "e": _uuid(EVENT), "s": _uuid(SESSION)})
        result_id = conn.execute(text(
            'INSERT INTO detection_results (job_id, verdict, "window", stickers, elapsed_ms, model_version, applied, '
            f"created_at) VALUES (:j, 'violation', NULL, '[]', 500, 'm1', :t, {ts}) RETURNING id"),
            {"j": _uuid(JOB), "t": True}).scalar_one()
        conn.execute(text("UPDATE detection_jobs SET result_id = :r"), {"r": result_id})
        conn.execute(text("UPDATE machines SET current_session_id = :s, last_applied_seq = 3 WHERE id = 1"),
                     {"s": _uuid(SESSION)})


def _result_trace():
    with db.engine.connect() as conn:
        row = conn.execute(text("SELECT job_id, machine_id, violation_id, event_id, session_id, seq, event, "
                                "received_at FROM detection_results")).one()
    return row


def test_trace_revision_copies_existing_results_and_round_trips(migration_app):
    upgrade(directory=MIGRATIONS, revision=PHASE2_TABLES_REVISION)
    _seed_phase2_rows_at_tables_revision()
    before = _snapshot()

    upgrade(directory=MIGRATIONS, revision=RESULT_TRACE_REVISION)

    job_id, machine_id, violation_id, event_id, session_id, seq, event, received_at = _result_trace()
    assert str(job_id).replace("-", "") == JOB.replace("-", "")
    assert (machine_id, violation_id, seq, event) == (1, 1, 3, "confirm")
    assert str(event_id).replace("-", "") == EVENT.replace("-", "")
    assert str(session_id).replace("-", "") == SESSION.replace("-", "")
    assert received_at is not None
    assert _snapshot() == before
    # at this revision the only difference to today's models is what later revisions add
    assert _diff_names(_schema_diff()) == {"add_column:detection_jobs.prev_signal_at"} | COMMAND_LINK_DIFF

    downgrade(directory=MIGRATIONS, revision=PHASE2_TABLES_REVISION)
    assert _revision() == PHASE2_TABLES_REVISION
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM detection_results")).scalar_one() == 1
        assert conn.execute(text("SELECT last_applied_seq FROM machines WHERE id = 1")).scalar_one() == 3
    columns = {c["name"] for c in inspect(db.engine).get_columns("detection_results")}
    assert "machine_id" not in columns and "received_at" not in columns
    assert _snapshot() == before

    upgrade(directory=MIGRATIONS)
    _, machine_id, violation_id, _, _, seq, event, _ = _result_trace()
    assert (machine_id, violation_id, seq, event) == (1, 1, 3, "confirm")
    assert _schema_diff() == []


def test_downgrade_refuses_to_drop_results_whose_job_is_gone(migration_app, capfd):
    """Below the trace revision results.job_id is NOT NULL, so an orphaned result cannot be represented.
    Downgrade stops instead of deleting audit data."""
    upgrade(directory=MIGRATIONS, revision=PHASE2_TABLES_REVISION)
    _seed_phase2_rows_at_tables_revision()
    upgrade(directory=MIGRATIONS)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM detection_jobs"))   # 30-day cleanup; results.job_id becomes NULL
    assert _result_trace()[0] is None

    with pytest.raises(SystemExit):   # Flask-Migrate logs the error and exits with status 1
        downgrade(directory=MIGRATIONS, revision=PHASE2_TABLES_REVISION)
    assert "whose job was already deleted" in capfd.readouterr().err
    db.session.remove()
    db.engine.dispose()
    # The refusal comes from d8f2b6c4a913, after the two newer revisions (command link, prev_signal_at) were already
    # undone. PostgreSQL rolls the whole run back (stays at the head, both columns present); SQLite DDL is not
    # transactional, so it keeps the finished steps (d8f2b6c4a913, neither column). Either way the version stamp must
    # match the schema, and no result row is lost.
    revision = _revision()
    columns = {c["name"] for c in inspect(db.engine).get_columns("detection_jobs")}
    state = (revision, "prev_signal_at" in columns, "command_id" in columns)
    assert state in {(COMMAND_LINK_REVISION, True, True), (RESULT_TRACE_REVISION, False, False)}
    assert _result_trace()[1] == 1
    assert "machine_id" in {c["name"] for c in inspect(db.engine).get_columns("detection_results")}


PREV_SIGNAL_REVISION = "e4b1d9a7c3f5"     # detection_jobs.prev_signal_at (SIGNAL_LOST snapshot, decision 2026-11-02)
COMMAND_LINK_REVISION = "b7d3f1a9c5e2"    # detection_jobs.command_id + unique partial index (one command = one confirm job)


def test_prev_signal_revision_adds_a_nullable_column_and_round_trips(migration_app):
    upgrade(directory=MIGRATIONS, revision=RESULT_TRACE_REVISION)
    with db.engine.begin() as conn:
        for statement in SEED:
            conn.execute(text(statement))
    _ts = "'2026-11-02 00:00:00+00'"
    with db.engine.begin() as conn:
        conn.execute(text(f"INSERT INTO device_sessions VALUES (:s, 1, {_ts}, 'active', NULL, {_ts}, {_ts})"),
                     {"s": _uuid(SESSION)})
        conn.execute(text(
            "INSERT INTO detection_jobs (id, machine_id, event_id, session_id, seq, event, received_at, s3_key, "
            f"status, publish_attempts, claim_attempt, created_at, updated_at) VALUES "
            f"(:j, 1, :e, :s, 1, 'arrived', {_ts}, 'inbox/m-1/x.jpg', 'queued', 1, 0, {_ts}, {_ts})"),
            {"j": _uuid(JOB), "e": _uuid(EVENT), "s": _uuid(SESSION)})
    before = _snapshot()

    upgrade(directory=MIGRATIONS, revision=PREV_SIGNAL_REVISION)
    with db.engine.connect() as conn:   # jobs written before this revision have no snapshot
        assert conn.execute(text("SELECT prev_signal_at, status FROM detection_jobs")).one() == (None, "queued")
    assert _snapshot() == before
    assert _diff_names(_schema_diff()) == COMMAND_LINK_DIFF            # only the newer revision is still missing

    downgrade(directory=MIGRATIONS, revision=RESULT_TRACE_REVISION)
    assert "prev_signal_at" not in {c["name"] for c in inspect(db.engine).get_columns("detection_jobs")}
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM detection_jobs")).scalar_one() == 1
    assert _snapshot() == before

    upgrade(directory=MIGRATIONS)
    assert _schema_diff() == []
