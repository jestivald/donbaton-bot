"""Add explicit legacy accounting snapshots and independent finite panel rights.

Revision ID: 0120
Revises: 0119

No existing entitlements are converted by the schema migration. The operator
imports a verified final snapshot separately after the old accounting drain.
"""

import sqlalchemy as sa
from alembic import op


revision = '0120'
down_revision = '0119'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column['name'] for column in inspector.get_columns('lte_quotas')}
    with op.batch_alter_table('lte_quotas') as batch:
        if 'legacy_state' not in columns:
            batch.add_column(sa.Column('legacy_state', sa.JSON(), nullable=True))
        if 'panel_access_cleared_at' not in columns:
            batch.add_column(sa.Column('panel_access_cleared_at', sa.DateTime(timezone=True), nullable=True))
        if 'trial_ends_at' not in columns:
            batch.add_column(sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=True))
        if 'pending_period' not in columns:
            batch.add_column(sa.Column('pending_period', sa.JSON(), nullable=True))
    if 'lte_orphan_quotas' not in inspector.get_table_names():
        op.create_table(
            'lte_orphan_quotas',
            sa.Column('panel_id', sa.BigInteger(), primary_key=True),
            sa.Column('end_date', sa.DateTime(timezone=True), nullable=False),
            sa.Column('original_status', sa.String(16), nullable=False),
            sa.Column('limit_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('next_limit_bytes', sa.BigInteger(), nullable=True),
            sa.Column('used_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('base_used_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('period_days', sa.Integer(), nullable=False, server_default='30'),
            sa.Column('period_starts_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('period_ends_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('counter_panel_id', sa.BigInteger(), nullable=True),
            sa.Column('counter_bytes', sa.BigInteger(), nullable=True),
            sa.Column('last_observed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('accounting_state', sa.String(24), nullable=False, server_default='prepared'),
            sa.Column('legacy_state', sa.JSON(), nullable=False),
            sa.Column('panel_access_cleared_at', sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                'limit_bytes >= 0 AND used_bytes >= 0 AND base_used_bytes >= 0', name='ck_lte_orphan_bytes'
            ),
            sa.CheckConstraint('period_days > 0', name='ck_lte_orphan_period'),
        )


def downgrade() -> None:
    connection = op.get_bind()
    imported = connection.execute(
        sa.text(
            'SELECT COUNT(*) FROM lte_quotas WHERE '
            "(legacy_state IS NOT NULL AND CAST(legacy_state AS TEXT) <> 'null') OR "
            "(pending_period IS NOT NULL AND CAST(pending_period AS TEXT) <> 'null') OR trial_ends_at IS NOT NULL"
        )
    ).scalar()
    orphans = connection.execute(sa.text('SELECT COUNT(*) FROM lte_orphan_quotas')).scalar()
    if imported or orphans:
        raise RuntimeError('Imported paid LTE rights require forward recovery; do not delete their accounting ledger')
    op.drop_table('lte_orphan_quotas')
    with op.batch_alter_table('lte_quotas') as batch:
        batch.drop_column('pending_period')
        batch.drop_column('trial_ends_at')
        batch.drop_column('panel_access_cleared_at')
        batch.drop_column('legacy_state')
