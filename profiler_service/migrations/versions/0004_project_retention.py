"""project retention

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-19 23:47:06.607779
"""

from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('retention_days', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('retention_last_purge_json', sa.Text(), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('retention_last_purge_json')
        batch_op.drop_column('retention_days')

