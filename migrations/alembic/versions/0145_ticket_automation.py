"""Persistent markers for client ticket automation."""

import sqlalchemy as sa
from alembic import op


revision = '0145'
down_revision = '0144'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tickets', sa.Column('client_reminder_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tickets', sa.Column('autoclosed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column('tickets', 'autoclosed_at')
    op.drop_column('tickets', 'client_reminder_sent_at')
