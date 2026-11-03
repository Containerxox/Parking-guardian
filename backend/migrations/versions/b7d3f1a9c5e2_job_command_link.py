"""detection_jobs.command_id + unique partial index: one command = at most one confirm job per machine

Revision ID: b7d3f1a9c5e2
Revises: e4b1d9a7c3f5
Create Date: 2026-11-02 21:00:00

Decision 2026-11-02 (ADR 0019 "confirm 명령 중복 반영 보호", option A). Existing jobs get NULL: before this revision a
job did not remember which command its photo named, and a NULL link never collides in the index. Earlier revisions
are not edited (they are applied to the local development database).

No foreign key to device_commands: commands are deleted after 30 days independently of jobs (design 6.12) and a
foreign key would also stop that clean-up. The link keeps protecting the command for as long as the job row exists.
"""
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b7d3f1a9c5e2'
down_revision = 'e4b1d9a7c3f5'
branch_labels = None
depends_on = None

INDEX = 'uq_detection_jobs_machine_command'


def _set_sqlite_foreign_keys(value: str) -> None:
    """Same as d8f2b6c4a913 / e4b1d9a7c3f5: PRAGMA foreign_keys only applies outside a transaction."""
    with op.get_context().autocommit_block():
        op.execute(f"PRAGMA foreign_keys={value}")
    state = op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar()
    if state != (1 if value == "ON" else 0):
        raise RuntimeError(f"PRAGMA foreign_keys={value} did not take effect (still {state})")


@contextmanager
def _sqlite_foreign_keys_off():
    """Dropping a column rebuilds detection_jobs on SQLite; results and commands reference it."""
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


def upgrade():
    with op.batch_alter_table('detection_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('command_id', sa.Uuid(), nullable=True))
    op.create_index(INDEX, 'detection_jobs', ['machine_id', 'command_id'], unique=True,
                    sqlite_where=sa.text('command_id IS NOT NULL'), postgresql_where=sa.text('command_id IS NOT NULL'))


def downgrade():
    op.drop_index(INDEX, table_name='detection_jobs',
                  sqlite_where=sa.text('command_id IS NOT NULL'), postgresql_where=sa.text('command_id IS NOT NULL'))
    with _sqlite_foreign_keys_off():
        with op.batch_alter_table('detection_jobs', schema=None) as batch_op:
            batch_op.drop_column('command_id')
