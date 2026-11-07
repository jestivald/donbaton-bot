"""Web API использует единственное событие, уже испускаемое CRUD."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database.models import Ticket, TicketMessage, User
from app.handlers.admin import tickets as admin_tickets
from app.services.event_emitter import event_emitter
from app.webapi.routes import tickets
from app.webapi.schemas.tickets import TicketReplyRequest
from tests.fixtures.sqlite_memory import memory_session


async def test_webapi_reply_emits_exactly_one_message_event(monkeypatch):
    """Настоящий add_message сохраняет ответ и отправляет событие один раз."""
    emit = AsyncMock()
    monkeypatch.setattr(event_emitter, 'emit', emit)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(tickets, 'create_bot', lambda: bot)
    notify = AsyncMock()
    monkeypatch.setattr(admin_tickets, 'notify_user_about_ticket_reply', notify)

    async with memory_session(monkeypatch, [User.__table__, Ticket.__table__, TicketMessage.__table__]) as db:
        db.add_all([User(id=1), Ticket(id=1, user_id=1, title='Проверка API', status='open')])
        await db.commit()

        response = await tickets.reply_to_ticket(1, TicketReplyRequest(message_text='Ответ поддержки'), db=db)

        assert response.ticket.status == 'answered'
        assert response.message.message_text == 'Ответ поддержки'
        emit.assert_awaited_once()
        name, payload = emit.await_args.args
        assert name == 'ticket.message_added'
        assert payload['message_id'] == response.message.id
        assert payload['ticket_id'] == 1
        assert payload['is_from_admin'] is True
        assert payload['status'] == 'answered'
        notify.assert_awaited_once()
        bot.session.close.assert_awaited_once()
