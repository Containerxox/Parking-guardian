"""Result trace columns and delete rules (decision 2026-11-02, ADR 0019 change record)

Revision ID: d8f2b6c4a913
Revises: c5e8a1f3b7d2
Create Date: 2026-11-02 15:00:00

c5e8a1f3b7d2 is already applied to the local development database, so it is not edited; this revision
changes what was decided after it:

- detection_results keeps 90 days, jobs 30 days. Deleting a job sets results.job_id to NULL, so the job's trace
  (machine_id, violation_id, event_id, session_id, seq, event, received_at) is copied onto the result.
  Existing results are filled from their job. UNIQUE(machine_id, event_id) keeps one result per event after
  job_id became NULL.
- detection_results.job_id: RESTRICT -> SET NULL (nullable)
- detection_jobs.result_id: SET NULL -> RESTRICT
- machines.current_session_id: SET NULL -> RESTRICT

Downgrade refuses to run while a result has no job (job_id NULL): the older schema cannot hold it, and
deleting audit data silently is worse than stopping.
"""
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd8f2b6c4a913'
down_revision = 'c5e8a1f3b7d2'
branch_labels = None
depends_on = None

TRACE_FROM_JOB = ("machine_id", "violation_id", "event_id", "session_id", "seq", "event", "received_at")
# c5e8a1f3b7d2 created results.job_id's foreign key without a name. PostgreSQL named it itself; on SQLite the
# batch naming convention below gives the unnamed key this name so it can be dropped.
UNNAMED_FK_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s"}


def _set_sqlite_foreign_keys(value: str) -> None:
    """PRAGMA foreign_keys is silently ignored inside an open transaction, and both the UPDATE above and every
    batch table rebuild (INSERT ... SELECT) open one. autocommit_block commits first, so the PRAGMA applies.
    (c5e8a1f3b7d2 lacks this: there, ON is not restored for the rest of that connection. Left unedited because
    it is already applied; `flask db upgrade` runs in its own process, which limits the effect.)"""
    with op.get_context().autocommit_block():
        op.execute(f"PRAGMA foreign_keys={value}")
    state = op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar()
    expected = 1 if value == "ON" else 0
    if state != expected:
        raise RuntimeError(f"PRAGMA foreign_keys={value} did not take effect (still {state})")


@contextmanager
def _sqlite_foreign_keys_off():
    """SQLite table rebuilds must not trip foreign keys of referencing rows; checked again afterwards."""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return
    _set_sqlite_foreign_keys("OFF")
    try:
        yield
        problems = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"foreign key check failed after table rebuild: {problems}")
    finally:
        _set_sqlite_foreign_keys("ON")


def _old_results_job_fk_name():
    return "detection_results_job_id_fkey" if op.get_bind().dialect.name == "postgresql" \
        else "fk_detection_results_job_id"


def upgrade():
    # 1. trace columns, nullable until filled
    with op.batch_alter_table('detection_results', schema=None) as batch_op:
        batch_op.add_column(sa.Column('machine_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('violation_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('event_id', sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column('session_id', sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column('seq', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('event', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('received_at', sa.DateTime(timezone=True), nullable=True))

    # 2. fill existing results from their job (job_id is NOT NULL at this point, so every row has one)
    assignments = ", ".join(
        f"{col} = (SELECT j.{col} FROM detection_jobs j WHERE j.id = detection_results.job_id)"
        for col in TRACE_FROM_JOB)
    op.execute(f"UPDATE detection_results SET {assignments}")

    with _sqlite_foreign_keys_off():
        # 3. results: constraints, job_id nullable with SET NULL
        with op.batch_alter_table('detection_results', schema=None,
                                  naming_convention=UNNAMED_FK_CONVENTION) as batch_op:
            batch_op.drop_constraint(_old_results_job_fk_name(), type_='foreignkey')
            batch_op.alter_column('job_id', existing_type=sa.Uuid(), nullable=True)
            for col, col_type in (('machine_id', sa.Integer()), ('event_id', sa.Uuid()), ('session_id', sa.Uuid()),
                                  ('seq', sa.Integer()), ('event', sa.String(length=20)),
                                  ('received_at', sa.DateTime(timezone=True))):
                batch_op.alter_column(col, existing_type=col_type, nullable=False)
            batch_op.create_foreign_key('fk_detection_results_job_id', 'detection_jobs', ['job_id'], ['id'],
                                        ondelete='SET NULL')
            batch_op.create_foreign_key('fk_detection_results_machine_id', 'machines', ['machine_id'], ['id'],
                                        ondelete='RESTRICT')
            batch_op.create_foreign_key('fk_detection_results_violation_id', 'violations', ['violation_id'], ['id'],
                                        ondelete='RESTRICT')
            batch_op.create_unique_constraint('uq_detection_results_machine_event', ['machine_id', 'event_id'])
            batch_op.create_check_constraint('ck_detection_results_event',
                                             "event IN ('arrived', 'confirm', 'refresh', 'left')")
            batch_op.create_check_constraint('ck_detection_results_seq', 'seq >= 1')
            batch_op.create_check_constraint('ck_detection_results_violation_only_if_applied',
                                             'violation_id IS NULL OR applied')
            batch_op.create_index('ix_detection_results_machine_received', ['machine_id', 'received_at'],
                                  unique=False)
            batch_op.create_index('ix_detection_results_violation', ['violation_id'], unique=False)

        # 4. jobs.result_id: SET NULL -> RESTRICT
        with op.batch_alter_table('detection_jobs', schema=None) as batch_op:
            batch_op.drop_constraint('fk_detection_jobs_result_id', type_='foreignkey')
            batch_op.create_foreign_key('fk_detection_jobs_result_id', 'detection_results', ['result_id'], ['id'],
                                        ondelete='RESTRICT')

        # 5. machines.current_session_id: SET NULL -> RESTRICT
        with op.batch_alter_table('machines', schema=None) as batch_op:
            batch_op.drop_constraint('fk_machines_current_session_id', type_='foreignkey')
            batch_op.create_foreign_key('fk_machines_current_session_id', 'device_sessions',
                                        ['current_session_id'], ['session_id'], ondelete='RESTRICT')


def downgrade():
    # Checked before any change, so a refusal leaves the database exactly as it was.
    orphans = op.get_bind().execute(sa.text("SELECT count(*) FROM detection_results WHERE job_id IS NULL")).scalar()
    if orphans:
        raise RuntimeError(
            f"detection_results has {orphans} row(s) whose job was already deleted (job_id IS NULL). "
            "Revision c5e8a1f3b7d2 requires job_id, so downgrading would have to delete this audit data. "
            "Export or delete those rows deliberately first, then downgrade.")

    with _sqlite_foreign_keys_off():
        with op.batch_alter_table('machines', schema=None) as batch_op:
            batch_op.drop_constraint('fk_machines_current_session_id', type_='foreignkey')
            batch_op.create_foreign_key('fk_machines_current_session_id', 'device_sessions',
                                        ['current_session_id'], ['session_id'], ondelete='SET NULL')

        with op.batch_alter_table('detection_jobs', schema=None) as batch_op:
            batch_op.drop_constraint('fk_detection_jobs_result_id', type_='foreignkey')
            batch_op.create_foreign_key('fk_detection_jobs_result_id', 'detection_results', ['result_id'], ['id'],
                                        ondelete='SET NULL')

        with op.batch_alter_table('detection_results', schema=None) as batch_op:
            batch_op.drop_index('ix_detection_results_violation')
            batch_op.drop_index('ix_detection_results_machine_received')
            batch_op.drop_constraint('ck_detection_results_violation_only_if_applied', type_='check')
            batch_op.drop_constraint('ck_detection_results_seq', type_='check')
            batch_op.drop_constraint('ck_detection_results_event', type_='check')
            batch_op.drop_constraint('uq_detection_results_machine_event', type_='unique')
            batch_op.drop_constraint('fk_detection_results_violation_id', type_='foreignkey')
            batch_op.drop_constraint('fk_detection_results_machine_id', type_='foreignkey')
            batch_op.drop_constraint('fk_detection_results_job_id', type_='foreignkey')
            batch_op.alter_column('job_id', existing_type=sa.Uuid(), nullable=False)
            for col in reversed(TRACE_FROM_JOB):
                batch_op.drop_column(col)
            # back to c5e8a1f3b7d2's rule; named like PostgreSQL's own default so a later upgrade finds it
            batch_op.create_foreign_key(_old_results_job_fk_name(), 'detection_jobs', ['job_id'], ['id'],
                                        ondelete='RESTRICT')
