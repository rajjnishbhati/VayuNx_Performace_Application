"""projects, teams and grants

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-19 23:28:01.179273
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('projects',
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('default_role', sa.String(length=16), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('project_id')
    )
    # every run and experiment recorded so far lands in the Default project; everyone signed in may use it
    projects = sa.table('projects', sa.column('project_id', sa.String), sa.column('name', sa.String),
                        sa.column('default_role', sa.String), sa.column('created_at', sa.DateTime))
    op.bulk_insert(projects, [{'project_id': 'default', 'name': 'Default project', 'default_role': 'editor',
                               'created_at': datetime.now(timezone.utc).replace(tzinfo=None)}])
    op.create_table('teams',
    sa.Column('team_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('team_id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('project_grants',
    sa.Column('project_id', sa.String(length=64), nullable=False),
    sa.Column('team_id', sa.String(length=32), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['teams.team_id'], ),
    sa.PrimaryKeyConstraint('project_id', 'team_id')
    )
    op.create_table('team_members',
    sa.Column('team_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.team_id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ),
    sa.PrimaryKeyConstraint('team_id', 'user_id')
    )
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(length=64), nullable=True))

    with op.batch_alter_table('experiments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(length=64), server_default='default', nullable=False))
        batch_op.create_index(batch_op.f('ix_experiments_project_id'), ['project_id'], unique=False)

    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(length=64), server_default='default', nullable=False))
        batch_op.create_index(batch_op.f('ix_runs_project_id'), ['project_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runs_project_id'))
        batch_op.drop_column('project_id')

    with op.batch_alter_table('experiments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_experiments_project_id'))
        batch_op.drop_column('project_id')

    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.drop_column('project_id')

    op.drop_table('team_members')
    op.drop_table('project_grants')
    op.drop_table('teams')
    op.drop_table('projects')
