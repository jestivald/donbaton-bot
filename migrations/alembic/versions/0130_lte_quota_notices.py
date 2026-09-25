"""отметки уведомлений об остатке LTE

Revision ID: 0130
Revises: 0129
Create Date: 2026-09-25

Переномерована 0127 → 0130 при переносе на ветку апстрима 4.15.0 (там 0127–0129 заняты
апстримовскими миграциями; правило §4а handoff 4.13.0: наша ещё не выкачена, двигается она).

Клиент получает «LTE почти закончился» и «LTE закончился» не больше одного раза
за период. Отметка ставится в цикле учёта до отправки, в той же транзакции, что
и списание, и снимается, когда остаток снова вырос (докупка, новый период,
ручная добавка). Колонки есть и у прав только в панели (``lte_orphan_quotas``):
о них сообщают в админ-чат.

Существующие строки не меняются: обе колонки появляются пустыми. Свежая установка
получает их из модели (``0001`` вызывает ``create_all``), поэтому каждая колонка
проверяется перед добавлением.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0130'
down_revision: Union[str, None] = '0129'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('lte_quotas', 'lte_orphan_quotas')
COLUMNS = ('low_notice_sent_at', 'exhausted_notice_sent_at')


def _existing_columns(table: str) -> set[str] | None:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return None
    return {column['name'] for column in inspector.get_columns(table)}


def upgrade() -> None:
    for table in TABLES:
        existing = _existing_columns(table)
        if existing is None:
            continue  # таблицы нет — учёт LTE здесь не развёрнут
        missing = [name for name in COLUMNS if name not in existing]
        if not missing:
            continue
        with op.batch_alter_table(table) as batch:
            for name in missing:
                batch.add_column(sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Отметки — служебное состояние рассылки, не оплаченные права: удаление
    # колонок теряет только память о том, кому уже написали в этом периоде.
    for table in TABLES:
        existing = _existing_columns(table)
        if not existing:
            continue
        present = [name for name in COLUMNS if name in existing]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for name in present:
                batch.drop_column(name)
