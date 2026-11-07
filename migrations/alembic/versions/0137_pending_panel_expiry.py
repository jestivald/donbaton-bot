# ДонБатон: апстрим v5.2.0 0133_pending_panel_expiry.py перенумерован в 0137 (у форка 0129–0135 заняты своими миграциями;
# прод на '0135' = 0135_ai_support). Цепочка: 0136 -> 0137.
"""Keep unconfirmed local expiry across restarts.

Revision ID: 0137
Revises: 0136
"""

import sqlalchemy as sa
from alembic import op


revision = '0137'
down_revision = '0136'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'pending_panel_expire_at' not in {column['name'] for column in inspector.get_columns('subscriptions')}:
        op.add_column('subscriptions', sa.Column('pending_panel_expire_at', sa.DateTime(timezone=True), nullable=True))
    if 'ix_subscriptions_pending_expiry' not in {index['name'] for index in inspector.get_indexes('subscriptions')}:
        op.create_index(
            'ix_subscriptions_pending_expiry',
            'subscriptions',
            ['id'],
            postgresql_where=sa.text('pending_panel_expire_at IS NOT NULL'),
            sqlite_where=sa.text('pending_panel_expire_at IS NOT NULL'),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'ix_subscriptions_pending_expiry' in {index['name'] for index in inspector.get_indexes('subscriptions')}:
        op.drop_index('ix_subscriptions_pending_expiry', table_name='subscriptions')
    if 'pending_panel_expire_at' in {column['name'] for column in inspector.get_columns('subscriptions')}:
        op.drop_column('subscriptions', 'pending_panel_expire_at')
