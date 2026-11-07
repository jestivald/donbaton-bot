"""Клиент закрывает только свой незакрытый тикет через общий CRUD/event путь."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.cabinet.routes import tickets
from app.config import settings
from app.database.models import Ticket, TicketMessage, User
from app.services.event_emitter import event_emitter
from tests.fixtures.sqlite_memory import memory_session


@pytest.fixture
def emitted(monkeypatch):
    monkeypatch.setattr(tickets.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(settings, 'SUPPORT_CLIENT_CAN_CLOSE_TICKETS', True)
    emit = AsyncMock()
    monkeypatch.setattr(event_emitter, 'emit', emit)
    return emit


@asynccontextmanager
async def ticket_client(monkeypatch, initial_status='open', *, reply_blocked=False):
    async with memory_session(monkeypatch, [User.__table__, Ticket.__table__, TicketMessage.__table__]) as db:
        user = User(id=1)
        earlier = datetime.now(UTC) - timedelta(hours=1)
        ticket = Ticket(
            id=10,
            user_id=1,
            title='Проверка закрытия',
            status=initial_status,
            created_at=earlier,
            updated_at=earlier,
            closed_at=earlier if initial_status == 'closed' else None,
            user_reply_block_permanent=reply_blocked,
        )
        db.add_all(
            [
                user,
                User(id=2),
                ticket,
                Ticket(id=20, user_id=2, title='Чужой тикет', status='open'),
                TicketMessage(
                    id=1, ticket_id=10, user_id=1, message_text='Вопрос', created_at=earlier, is_from_admin=False
                ),
                TicketMessage(
                    id=2,
                    ticket_id=10,
                    user_id=2,
                    message_text='Ответ',
                    created_at=earlier + timedelta(minutes=1),
                    is_from_admin=True,
                ),
            ]
        )
        await db.commit()
        app = FastAPI()
        app.include_router(tickets.router, prefix='/cabinet')
        app.dependency_overrides[tickets.get_current_cabinet_user] = lambda: user
        app.dependency_overrides[tickets.get_cabinet_db] = lambda: db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            yield client, db, ticket


@pytest.mark.parametrize('initial_status', ['open', 'answered', 'pending'])
async def test_close_persists_and_emits_once(monkeypatch, emitted, initial_status):
    async with ticket_client(monkeypatch, initial_status) as (client, db, ticket):
        response = await client.post('/cabinet/tickets/10/close')
        assert response.status_code == 200
        detail = response.json()
        assert detail['status'] == 'closed'
        assert detail['can_close'] is False
        assert detail['closed_at'] is not None
        assert [message['message_text'] for message in detail['messages']] == ['Вопрос', 'Ответ']
        assert detail == (await client.get('/cabinet/tickets/10')).json()
        await db.refresh(ticket)
        assert ticket.status == 'closed'
        assert ticket.closed_at is not None
        assert ticket.updated_at > ticket.created_at
        emitted.assert_awaited_once()
        event, payload = emitted.await_args.args
        assert event == 'ticket.status_changed'
        assert payload == {
            'ticket_id': 10,
            'user_id': 1,
            'old_status': initial_status,
            'new_status': 'closed',
            'closed_at': ticket.closed_at.isoformat(),
        }

        # Повтор не меняет время закрытия и не создаёт новое событие.
        again = await client.post('/cabinet/tickets/10/close')
        assert again.status_code == 400
        assert (await client.get('/cabinet/tickets/10')).json() == detail
        emitted.assert_awaited_once()
        reply = await client.post('/cabinet/tickets/10/messages', json={'message': 'Поздний ответ'})
        assert reply.status_code == 400
        assert (await client.get('/cabinet/tickets/10')).json() == detail


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('initial_status', ['open', 'answered', 'pending', 'closed'])
async def test_get_exposes_close_capability(monkeypatch, emitted, enabled, initial_status):
    monkeypatch.setattr(settings, 'SUPPORT_CLIENT_CAN_CLOSE_TICKETS', enabled)
    async with ticket_client(monkeypatch, initial_status) as (client, _, _):
        response = await client.get('/cabinet/tickets/10')
        assert response.status_code == 200
        assert response.json()['can_close'] is (enabled and initial_status != 'closed')
        emitted.assert_not_awaited()


@pytest.mark.parametrize('ticket_id', [20, 999])
async def test_foreign_and_missing_tickets_are_not_found(monkeypatch, emitted, ticket_id):
    async with ticket_client(monkeypatch) as (client, db, _):
        for method, path in [('GET', f'/cabinet/tickets/{ticket_id}'), ('POST', f'/cabinet/tickets/{ticket_id}/close')]:
            response = await client.request(method, path)
            assert response.status_code == 404
            assert response.json()['detail'] == 'Ticket not found'
        assert (await db.get(Ticket, 20)).status == 'open'
        emitted.assert_not_awaited()


@pytest.mark.parametrize('disabled', ['client_close', 'tickets'])
async def test_disabled_feature_rejects_close(monkeypatch, emitted, disabled):
    if disabled == 'client_close':
        monkeypatch.setattr(settings, 'SUPPORT_CLIENT_CAN_CLOSE_TICKETS', False)
    else:
        monkeypatch.setattr(tickets.SupportSettingsService, 'is_tickets_enabled', lambda: False)
    async with ticket_client(monkeypatch) as (client, db, ticket):
        response = await client.post('/cabinet/tickets/10/close')
        assert response.status_code == 403
        await db.refresh(ticket)
        assert ticket.status == 'open'
        assert ticket.closed_at is None
        emitted.assert_not_awaited()


async def test_already_closed_rejects_without_event(monkeypatch, emitted):
    async with ticket_client(monkeypatch, 'closed') as (client, db, ticket):
        closed_at = ticket.closed_at
        response = await client.post('/cabinet/tickets/10/close')
        assert response.status_code == 400
        await db.refresh(ticket)
        assert ticket.closed_at == closed_at
        emitted.assert_not_awaited()


async def test_reply_block_does_not_prevent_own_close(monkeypatch, emitted):
    async with ticket_client(monkeypatch, reply_blocked=True) as (client, _, _):
        response = await client.post('/cabinet/tickets/10/close')
        assert response.status_code == 200
        assert response.json()['is_reply_blocked'] is True
        assert response.json()['can_close'] is False
        emitted.assert_awaited_once()
