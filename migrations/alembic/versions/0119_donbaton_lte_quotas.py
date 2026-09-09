"""DonBaton network categories and independent LTE ledger (opt-in).

Revision ID: 0119
Revises: 0118
"""

import sqlalchemy as sa
from alembic import op


revision = '0119'
down_revision = '0118'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, columns in (
        (
            'tariffs',
            [
                sa.Column('network_category', sa.String(16), nullable=True),
                sa.Column('lte_traffic_limit_gb', sa.Integer(), nullable=True),
                sa.Column('lte_period_days', sa.Integer(), nullable=True),
            ],
        ),
        ('subscriptions', [sa.Column('network_category', sa.String(16), nullable=True)]),
    ):
        if table not in inspector.get_table_names():
            continue
        existing = {column['name'] for column in inspector.get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for column in columns:
                if column.name not in existing:
                    batch.add_column(column)

    if 'lte_quotas' not in inspector.get_table_names():
        op.create_table(
            'lte_quotas',
            sa.Column(
                'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), primary_key=True
            ),
            sa.Column('limit_bytes', sa.BigInteger(), nullable=False),
            sa.Column('next_limit_bytes', sa.BigInteger(), nullable=True),
            sa.Column('used_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('base_used_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('period_days', sa.Integer(), nullable=False),
            sa.Column('period_starts_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('period_ends_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('counter_panel_id', sa.BigInteger(), nullable=True),
            sa.Column('counter_bytes', sa.BigInteger(), nullable=True),
            sa.Column('last_observed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('accounting_state', sa.String(24), nullable=False, server_default='pending'),
            sa.CheckConstraint(
                'limit_bytes >= 0 AND used_bytes >= 0 AND base_used_bytes >= 0', name='ck_lte_quota_bytes'
            ),
            sa.CheckConstraint('period_days > 0', name='ck_lte_quota_period'),
        )
    if 'lte_topups' not in inspector.get_table_names():
        op.create_table(
            'lte_topups',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False
            ),
            sa.Column('operation_id', sa.String(128), nullable=False, unique=True),
            sa.Column('amount_bytes', sa.BigInteger(), nullable=False),
            sa.Column('used_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                'amount_bytes > 0 AND used_bytes >= 0 AND used_bytes <= amount_bytes', name='ck_lte_topup_bytes'
            ),
        )
        op.create_index('ix_lte_topups_subscription_expiry', 'lte_topups', ['subscription_id', 'expires_at'])


def downgrade() -> None:
    op.drop_table('lte_topups')
    op.drop_table('lte_quotas')
    with op.batch_alter_table('subscriptions') as batch:
        batch.drop_column('network_category')
    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('lte_period_days')
        batch.drop_column('lte_traffic_limit_gb')
        batch.drop_column('network_category')
