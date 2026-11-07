"""Журнал ИИ-помощника и дедупликация сообщений тикетов.

Revision ID: 0135
Revises: 0134
"""

import sqlalchemy as sa
from alembic import op


revision = '0135'
down_revision = '0134'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_support_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('channel', sa.String(20), nullable=False),
        sa.Column('outcome', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('ticket_id', sa.Integer(), sa.ForeignKey('tickets.id', ondelete='SET NULL'), nullable=True),
        sa.Column('trigger_message_id', sa.Integer(), nullable=True, unique=True),
        sa.Column('reply_message_id', sa.Integer(), nullable=True),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('provider', sa.String(40), nullable=False),
        sa.Column('thinking', sa.Boolean(), nullable=False, server_default='false'),
        *[
            sa.Column(name, sa.Integer(), nullable=False, server_default='0')
            for name in (
                'prompt_tokens',
                'cache_hit_tokens',
                'cache_miss_tokens',
                'completion_tokens',
                'reasoning_tokens',
                'latency_ms',
                'tool_calls',
            )
        ],
        sa.Column('cost_usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('handoff_reason', sa.String(30), nullable=True),
        sa.Column('operator_summary', sa.Text(), nullable=True),
        sa.Column('question_text', sa.Text(), nullable=True),
        sa.Column('answer_text', sa.Text(), nullable=True),
        sa.Column('error_code', sa.String(40), nullable=True),
        sa.Column('kb_version', sa.String(16), nullable=False, server_default=''),
        sa.Column('prompt_version', sa.String(16), nullable=False, server_default=''),
        sa.Column('feedback', sa.String(16), nullable=True),
        sa.Column('feedback_at', sa.DateTime(timezone=True), nullable=True),
    )
    for name in ('created_at', 'user_id', 'ticket_id'):
        op.create_index(f'ix_ai_support_requests_{name}', 'ai_support_requests', [name])


def downgrade() -> None:
    op.drop_table('ai_support_requests')
