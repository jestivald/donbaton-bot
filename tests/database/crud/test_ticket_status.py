"""Событие смены статуса содержит прежнее значение до записи нового."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.database.crud.ticket import TicketCRUD
from app.database.models import Ticket, User
from app.services.event_emitter import event_emitter
from tests.fixtures.sqlite_memory import memory_session


@pytest.mark.parametrize(
    ('old_status', 'new_status'), [('open', 'answered'), ('pending', 'closed'), ('answered', 'open')]
)
async def test_status_event_preserves_previous_status(monkeypatch, old_status, new_status):
    emit = AsyncMock()
    monkeypatch.setattr(event_emitter, 'emit', emit)
    closed_at = datetime.now(UTC) if new_status == 'closed' else None
    async with memory_session(monkeypatch, [User.__table__, Ticket.__table__]) as db:
        ticket = Ticket(id=1, user_id=1, title='Проверка статуса', status=old_status)
        db.add_all([User(id=1), ticket])
        await db.commit()

        assert await TicketCRUD.update_ticket_status(db, 1, new_status, closed_at)
        await db.refresh(ticket)
        assert ticket.status == new_status
        assert ticket.closed_at == closed_at
        emit.assert_awaited_once_with(
            'ticket.status_changed',
            {
                'ticket_id': 1,
                'user_id': 1,
                'old_status': old_status,
                'new_status': new_status,
                'closed_at': closed_at.isoformat() if closed_at else None,
            },
            db=db,
        )
