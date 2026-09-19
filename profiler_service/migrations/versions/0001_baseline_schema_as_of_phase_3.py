"""baseline: schema as of Phase 3

Revision ID: 0001
Revises: 
Create Date: 2026-09-19 22:46:37.142906
"""

from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('experiments',
    sa.Column('experiment_id', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=8), nullable=False),
    sa.Column('label', sa.String(length=256), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('params_json', sa.Text(), nullable=False),
    sa.Column('reference_preset', sa.String(length=64), nullable=True),
    sa.Column('progress_done', sa.Integer(), nullable=False),
    sa.Column('progress_total', sa.Integer(), nullable=False),
    sa.Column('current_json', sa.Text(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('env_json', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('experiment_id')
    )
    op.create_table('runs',
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('service', sa.String(length=128), nullable=False),
    sa.Column('label', sa.String(length=256), nullable=False),
    sa.Column('phase', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('metadata_json', sa.Text(), nullable=False),
    sa.Column('variant', sa.String(length=256), nullable=True),
    sa.Column('experiment_id', sa.String(length=64), nullable=True),
    sa.Column('trial_index', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=8), server_default='app', nullable=False),
    sa.Column('env_json', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('run_id')
    )
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runs_experiment_id'), ['experiment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runs_service'), ['service'], unique=False)

    op.create_table('op_stats',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('service', sa.String(length=128), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('op_name', sa.String(length=256), nullable=False),
    sa.Column('attributes_json', sa.Text(), nullable=False),
    sa.Column('interval_start', sa.DateTime(), nullable=False),
    sa.Column('interval_end', sa.DateTime(), nullable=False),
    sa.Column('count', sa.BigInteger(), nullable=False),
    sa.Column('sum_ns', sa.BigInteger(), nullable=False),
    sa.Column('min_ns', sa.BigInteger(), nullable=False),
    sa.Column('max_ns', sa.BigInteger(), nullable=False),
    sa.Column('p50_ns', sa.BigInteger(), nullable=True),
    sa.Column('p95_ns', sa.BigInteger(), nullable=True),
    sa.Column('p99_ns', sa.BigInteger(), nullable=True),
    sa.Column('histogram_json', sa.Text(), nullable=False),
    sa.Column('sdk_overhead_ns', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('op_stats', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_op_stats_run_id'), ['run_id'], unique=False)

    op.create_table('samples',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('service', sa.String(length=128), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('metric_name', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.Column('unit', sa.String(length=32), nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('samples', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_samples_run_id'), ['run_id'], unique=False)

    op.create_table('spans',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('span_id', sa.String(length=64), nullable=False),
    sa.Column('parent_span_id', sa.String(length=64), nullable=True),
    sa.Column('service', sa.String(length=128), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('span_name', sa.String(length=256), nullable=False),
    sa.Column('start_time', sa.DateTime(), nullable=False),
    sa.Column('end_time', sa.DateTime(), nullable=False),
    sa.Column('duration_ms', sa.Float(), nullable=False),
    sa.Column('attributes_json', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.run_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'span_id', name='uq_span_per_run')
    )
    with op.batch_alter_table('spans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_spans_run_id'), ['run_id'], unique=False)

    op.create_table('trial_results',
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('experiment_id', sa.String(length=64), nullable=False),
    sa.Column('preset_id', sa.String(length=64), nullable=False),
    sa.Column('trial_index', sa.Integer(), nullable=False),
    sa.Column('concurrency', sa.Integer(), nullable=False),
    sa.Column('ops', sa.BigInteger(), nullable=False),
    sa.Column('wall_s', sa.Float(), nullable=False),
    sa.Column('ops_per_s', sa.Float(), nullable=False),
    sa.Column('cpu_user_s', sa.Float(), nullable=False),
    sa.Column('cpu_system_s', sa.Float(), nullable=False),
    sa.Column('cpu_s_per_op', sa.Float(), nullable=False),
    sa.Column('cores_busy', sa.Float(), nullable=False),
    sa.Column('rss_before_bytes', sa.BigInteger(), nullable=False),
    sa.Column('peak_rss_bytes', sa.BigInteger(), nullable=False),
    sa.Column('peak_rss_method', sa.String(length=32), nullable=False),
    sa.Column('threads_max', sa.Integer(), nullable=False),
    sa.Column('ctx_switches', sa.BigInteger(), nullable=False),
    sa.Column('timer_overhead_ns', sa.Float(), nullable=False),
    sa.Column('measure_start', sa.DateTime(), nullable=False),
    sa.Column('measure_end', sa.DateTime(), nullable=False),
    sa.Column('quiet_json', sa.Text(), nullable=False),
    sa.Column('noisy', sa.Integer(), nullable=False),
    sa.Column('other_cores_busy_median', sa.Float(), nullable=True),
    sa.Column('other_cores_busy_trial', sa.Float(), nullable=True),
    sa.Column('sampler_overhead_ms', sa.Float(), nullable=True),
    sa.Column('result_json', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.run_id'], ),
    sa.PrimaryKeyConstraint('run_id')
    )
    with op.batch_alter_table('trial_results', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_trial_results_experiment_id'), ['experiment_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('trial_results', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_trial_results_experiment_id'))

    op.drop_table('trial_results')
    with op.batch_alter_table('spans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_spans_run_id'))

    op.drop_table('spans')
    with op.batch_alter_table('samples', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_samples_run_id'))

    op.drop_table('samples')
    with op.batch_alter_table('op_stats', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_op_stats_run_id'))

    op.drop_table('op_stats')
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runs_service'))
        batch_op.drop_index(batch_op.f('ix_runs_experiment_id'))

    op.drop_table('runs')
    op.drop_table('experiments')
