"""violation plate OCR (ADR 0015): plate_number and plate_confidence on violations

Revision ID: a3c7e51d9b02
Revises: fcc09ab94851
Create Date: 2026-10-24 15:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3c7e51d9b02'
down_revision = 'fcc09ab94851'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('violations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plate_number', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('plate_confidence', sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table('violations', schema=None) as batch_op:
        batch_op.drop_column('plate_confidence')
        batch_op.drop_column('plate_number')
