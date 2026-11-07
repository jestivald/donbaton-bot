"""Ответ клиента сохраняет открытую очередь, SLA и блокировку тикета."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes import tickets
from app.cabinet.schemas.tickets import TicketMessageCreateRequest
from app.database.models import Ticket, TicketMessage, User
from app.services.event_emitter import event_emitter
from tests.fixtures.sqlite_memory import memory_session


@pytest.fixture
def isolated_notifications(monkeypatch):
    """Оставить реальные запросы и модель, подменив только внешние эффекты."""
    monkeypatch.setattr(tickets.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(tickets, 'notify_admins_about_ticket_reply', AsyncMock())
    monkeypatch.setattr(
        tickets.TicketNotificationCRUD, 'create_admin_notification_for_user_reply', AsyncMock(return_value=None)
    )
    emit = AsyncMock()
    monkeypatch.setattr(event_emitter, 'emit', emit)
    return emit


@pytest.mark.parametrize('initial_status', ['answered', 'pending', 'open', 'closed'])
async def test_client_reply_reopens_ticket_and_resets_sla(monkeypatch, isolated_notifications, initial_status):
    """Закрытый тикет по-прежнему не принимает ответ; остальные ждут поддержки."""
    async with memory_session(monkeypatch, [User.__table__, Ticket.__table__, TicketMessage.__table__]) as db:
        user = User(id=1)
        earlier = datetime.now(UTC) - timedelta(hours=2)
        ticket = Ticket(
            id=1,
            user_id=1,
            title='Проверка очереди',
            status=initial_status,
            updated_at=earlier,
            last_sla_reminder_at=earlier,
            closed_at=earlier if initial_status == 'closed' else None,
        )
        db.add_all([user, ticket])
        await db.commit()
        request = TicketMessageCreateRequest(message='Проблема всё ещё есть')

        if initial_status == 'closed':
            with pytest.raises(HTTPException) as error:
                await tickets.add_ticket_message(1, request, user, db)
            assert error.value.status_code == 400
            assert error.value.detail == 'Cannot add message to closed ticket'
            assert (await db.execute(select(TicketMessage))).scalars().all() == []
            assert ticket.status == 'closed'
            assert ticket.closed_at == earlier
            assert ticket.updated_at == earlier
            assert ticket.last_sla_reminder_at == earlier
            isolated_notifications.assert_not_awaited()
            return

        reply = await tickets.add_ticket_message(1, request, user, db)
        await db.refresh(ticket)
        assert ticket.status == 'open'
        assert ticket.last_sla_reminder_at is None
        assert ticket.updated_at > earlier
        assert reply.message_text == request.message
        isolated_notifications.assert_awaited_once()
        assert isolated_notifications.await_args.args[1]['status'] == 'open'


@pytest.mark.parametrize('block_kind', ['permanent', 'temporary', 'expired', 'none'])
async def test_reply_block_is_exposed_and_enforced(monkeypatch, isolated_notifications, block_kind):
    """Поле API и guard используют вычисляемое свойство настоящей модели."""
    # Изолируем per-ticket guard: глобальный guard тестируется отдельно.
    monkeypatch.setattr(tickets.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=None))
    blocked = block_kind in {'permanent', 'temporary'}
    until = {
        'temporary': datetime.now(UTC) + timedelta(days=1),
        'expired': datetime.now(UTC) - timedelta(days=1),
    }.get(block_kind)
    async with memory_session(monkeypatch, [User.__table__, Ticket.__table__, TicketMessage.__table__]) as db:
        user = User(id=1)
        ticket = Ticket(
            id=1,
            user_id=1,
            title='Проверка блокировки',
            status='open',
            messages=[],
            user_reply_block_permanent=block_kind == 'permanent',
            user_reply_block_until=until,
        )
        db.add_all([user, ticket])
        await db.commit()

        detail = await tickets.get_ticket(1, user, db)
        request = TicketMessageCreateRequest(message='Проверка ответа')
        if blocked:
            with pytest.raises(HTTPException) as error:
                await tickets.add_ticket_message(1, request, user, db)
            assert error.value.status_code == 403
            assert error.value.detail == 'Replies to this ticket are blocked'
            assert (await db.execute(select(TicketMessage))).scalars().all() == []
            isolated_notifications.assert_not_awaited()
        else:
            await tickets.add_ticket_message(1, request, user, db)
        assert detail.is_reply_blocked is blocked
