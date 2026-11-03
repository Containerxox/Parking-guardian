"""Phase 2 asynchronous judgement (ADR 0019): device_sessions, detection_jobs, detection_results,
device_commands, and the order-guard columns machines.current_session_id / last_applied_seq

Revision ID: c5e8a1f3b7d2
Revises: a3c7e51d9b02
Create Date: 2026-11-02 12:00:00

Existing rows are not changed: the new machine columns start as NULL / 0 and the new tables start empty.
Downgrade drops the four tables and the two columns, so any Phase 2 rows are lost (Phase 1 rows are kept).

Two foreign key cycles (machines <-> device_sessions, detection_jobs <-> detection_results) mean one key of
each pair is added after both tables exist. On SQLite that needs a table rebuild (batch mode); SQLite's
DROP TABLE inside the rebuild would trip the foreign keys of rows that reference the rebuilt table, so
enforcement is switched off around the rebuild and checked again afterwards.
"""
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c5e8a1f3b7d2'
down_revision = 'a3c7e51d9b02'
branch_labels = None
depends_on = None

ACTIVE_COMMAND = "status IN ('pending', 'delivered')"


@contextmanager
def _sqlite_foreign_keys_off():
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return
    # Only takes effect outside a transaction; Alembic does not open one for SQLite DDL.
    op.execute("PRAGMA foreign_keys=OFF")
    try:
        yield
        problems = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"foreign key check failed after table rebuild: {problems}")
    finally:
        op.execute("PRAGMA foreign_keys=ON")


def upgrade():
    op.create_table(
        'device_sessions',
        sa.Column('session_id', sa.Uuid(), nullable=False),
        sa.Column('machine_id', sa.Integer(), nullable=False),
        sa.Column('first_received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('left_reported_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'ended', 'superseded')", name='ck_device_sessions_status'),
        sa.ForeignKeyConstraint(['machine_id'], ['machines.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('session_id'),
    )
    op.create_index('ix_device_sessions_machine_first_received', 'device_sessions',
                    ['machine_id', 'first_received_at'], unique=False)

    op.create_table(
        'detection_jobs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('machine_id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.Uuid(), nullable=False),
        sa.Column('session_id', sa.Uuid(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('event', sa.String(length=20), nullable=False),
        sa.Column('detected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('s3_key', sa.String(length=500), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('publish_attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('next_publish_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('claim_attempt', sa.Integer(), server_default='0', nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('worker_id', sa.String(length=100), nullable=True),
        sa.Column('result_id', sa.Integer(), nullable=True),   # foreign key added below
        sa.Column('violation_id', sa.Integer(), nullable=True),
        sa.Column('effects_status', sa.String(length=20), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_publish', 'queued', 'processing', 'done', 'stale', 'rejected', 'dead_lettered')",
            name='ck_detection_jobs_status'),
        sa.CheckConstraint("event IN ('arrived', 'confirm', 'refresh', 'left')", name='ck_detection_jobs_event'),
        sa.CheckConstraint('seq >= 1', name='ck_detection_jobs_seq'),
        sa.CheckConstraint("effects_status IN ('pending', 'done', 'failed')",
                           name='ck_detection_jobs_effects_status'),
        sa.CheckConstraint("effects_status IS NULL OR (status = 'done' AND violation_id IS NOT NULL)",
                           name='ck_detection_jobs_effects_only_for_new_violation'),
        sa.ForeignKeyConstraint(['machine_id'], ['machines.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['session_id'], ['device_sessions.session_id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['violation_id'], ['violations.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('machine_id', 'event_id', name='uq_detection_jobs_machine_event'),
    )
    op.create_index('ix_detection_jobs_status_next_publish', 'detection_jobs',
                    ['status', 'next_publish_at'], unique=False)
    op.create_index('ix_detection_jobs_machine_session_seq', 'detection_jobs',
                    ['machine_id', 'session_id', 'seq'], unique=False)

    op.create_table(
        'detection_results',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Uuid(), nullable=False),
        sa.Column('verdict', sa.String(length=20), nullable=False),
        sa.Column('window', sa.JSON(), nullable=True),
        sa.Column('stickers', sa.JSON(), nullable=False),
        sa.Column('elapsed_ms', sa.Integer(), nullable=False),
        sa.Column('model_version', sa.String(length=100), nullable=True),
        sa.Column('applied', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("verdict IN ('violation', 'permitted', 'no_vehicle')",
                           name='ck_detection_results_verdict'),
        sa.ForeignKeyConstraint(['job_id'], ['detection_jobs.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id'),
    )

    op.create_table(
        'device_commands',
        sa.Column('command_id', sa.Uuid(), nullable=False),
        sa.Column('machine_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Uuid(), nullable=False),
        sa.Column('type', sa.String(length=30), nullable=False),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('source_job_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("type IN ('capture_confirm')", name='ck_device_commands_type'),
        sa.CheckConstraint("status IN ('pending', 'delivered', 'fulfilled', 'cancelled', 'expired')",
                           name='ck_device_commands_status'),
        sa.ForeignKeyConstraint(['machine_id'], ['machines.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['session_id'], ['device_sessions.session_id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['source_job_id'], ['detection_jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('command_id'),
    )
    op.create_index('ix_device_commands_machine_status', 'device_commands', ['machine_id', 'status'], unique=False)
    op.create_index('uq_device_commands_active', 'device_commands', ['machine_id', 'session_id', 'type'],
                    unique=True, sqlite_where=sa.text(ACTIVE_COMMAND), postgresql_where=sa.text(ACTIVE_COMMAND))

    with _sqlite_foreign_keys_off():
        with op.batch_alter_table('detection_jobs', schema=None) as batch_op:
            batch_op.create_foreign_key('fk_detection_jobs_result_id', 'detection_results',
                                        ['result_id'], ['id'], ondelete='SET NULL')

        with op.batch_alter_table('machines', schema=None) as batch_op:
            batch_op.add_column(sa.Column('current_session_id', sa.Uuid(), nullable=True))
            batch_op.add_column(sa.Column('last_applied_seq', sa.Integer(), server_default='0', nullable=False))
            batch_op.create_foreign_key('fk_machines_current_session_id', 'device_sessions',
                                        ['current_session_id'], ['session_id'], ondelete='SET NULL')


def downgrade():
    with _sqlite_foreign_keys_off():
        with op.batch_alter_table('machines', schema=None) as batch_op:
            batch_op.drop_constraint('fk_machines_current_session_id', type_='foreignkey')
            batch_op.drop_column('last_applied_seq')
            batch_op.drop_column('current_session_id')

        with op.batch_alter_table('detection_jobs', schema=None) as batch_op:
            batch_op.drop_constraint('fk_detection_jobs_result_id', type_='foreignkey')

    op.drop_index('uq_device_commands_active', table_name='device_commands',
                  sqlite_where=sa.text(ACTIVE_COMMAND), postgresql_where=sa.text(ACTIVE_COMMAND))
    op.drop_index('ix_device_commands_machine_status', table_name='device_commands')
    op.drop_table('device_commands')
    op.drop_table('detection_results')
    op.drop_index('ix_detection_jobs_machine_session_seq', table_name='detection_jobs')
    op.drop_index('ix_detection_jobs_status_next_publish', table_name='detection_jobs')
    op.drop_table('detection_jobs')
    op.drop_index('ix_device_sessions_machine_first_received', table_name='device_sessions')
    op.drop_table('device_sessions')
