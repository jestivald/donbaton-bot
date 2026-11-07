# ДонБатон: апстрим v5.2.0 0138_daily_selected_devices_billing.py перенумерован в 0142 (у форка 0129–0135 заняты своими миграциями;
# прод на '0135' = 0135_ai_support). Цепочка: 0141 -> 0142.
"""Version daily device billing without repricing existing subscriptions.

Revision ID: 0142
Revises: 0141
"""

import sqlalchemy as sa
from alembic import op


revision = '0142'
down_revision = '0141'
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('subscriptions')}
    if 'daily_devices_billing_version' not in columns:
        op.add_column(
            'subscriptions',
            sa.Column('daily_devices_billing_version', sa.Integer(), nullable=False, server_default='0'),
        )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text('SELECT 1 FROM subscriptions WHERE daily_devices_billing_version <> 0 LIMIT 1'))
        .first()
    ):
        raise RuntimeError('Cannot discard active daily device billing policy')
    op.drop_column('subscriptions', 'daily_devices_billing_version')
