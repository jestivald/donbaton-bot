"""Persist renewal responses atomically with balance charges.

Revision ID: 0146
Revises: 0145

В апстриме v5.2.1 — 0141 (down 0140); у форка ДонБатона 0141–0145 заняты, перенумеровано в 0146.
"""

import sqlalchemy as sa
from alembic import op


revision = '0146'
down_revision = '0145'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'subscription_renewal_operations',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('operation_id', sa.String(36), primary_key=True),
        sa.Column('subscription_id', sa.Integer(), nullable=False),
        sa.Column('period_days', sa.Integer(), nullable=False),
        sa.Column('expected_end_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('response', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('subscription_renewal_operations')
