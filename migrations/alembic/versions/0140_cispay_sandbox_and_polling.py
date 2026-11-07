# ДонБатон: апстрим v5.2.0 0136_cispay_sandbox_and_polling.py перенумерован в 0140 (у форка 0129–0135 заняты своими миграциями;
# прод на '0135' = 0135_ai_support). Цепочка: 0139 -> 0140.
"""Persist CisPay sandbox isolation and shared polling reservation.

Revision ID: 0140
Revises: 0139
"""

import sqlalchemy as sa
from alembic import op

revision = '0140'
down_revision = '0139'
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('cispay_payments')}
    if 'is_sandbox' not in columns:
        op.add_column(
            'cispay_payments', sa.Column('is_sandbox', sa.Boolean(), nullable=False, server_default=sa.false())
        )
    if 'next_check_at' not in columns:
        op.add_column('cispay_payments', sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if op.get_bind().execute(sa.text('SELECT 1 FROM cispay_payments WHERE is_sandbox LIMIT 1')).first():
        raise RuntimeError('Cannot discard sandbox provenance from existing payments')
    op.drop_column('cispay_payments', 'next_check_at')
    op.drop_column('cispay_payments', 'is_sandbox')
