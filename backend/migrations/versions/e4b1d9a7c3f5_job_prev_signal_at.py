"""detection_jobs.prev_signal_at: the SIGNAL_LOST snapshot taken at ingest (decision 2026-11-02)

Revision ID: e4b1d9a7c3f5
Revises: d8f2b6c4a913
Create Date: 2026-11-02 18:00:00

Nullable, no default: jobs written before this revision (none outside tests) and a machine's first-ever photo
have no previous signal. Earlier revisions are not edited (c5e8a1f3b7d2 and d8f2b6c4a913 are applied to the
local development database).
"""
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e4b1d9a7c3f5'
down_revision = 'd8f2b6c4a913'
branch_labels = None
depends_on = None


def _set_sqlite_foreign_keys(value: str) -> None:
    """Same as d8f2b6c4a913: PRAGMA foreign_keys only applies outside a transaction."""
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
        batch_op.add_column(sa.Column('prev_signal_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with _sqlite_foreign_keys_off():
        with op.batch_alter_table('detection_jobs', schema=None) as batch_op:
            batch_op.drop_column('prev_signal_at')
