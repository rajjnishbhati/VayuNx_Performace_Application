"""share links

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20 00:02:38.490145
"""

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('share_links',
    sa.Column('share_id', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('target_json', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('views', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('share_id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('share_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_share_links_project_id'), ['project_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('share_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_share_links_project_id'))

    op.drop_table('share_links')
