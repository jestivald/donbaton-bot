"""Публикация: один commit/событие, повторная валидация и независимые каналы."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import SupportAuditLog, TicketMessage, User
from app.services import ticket_reply_service as service


class Db:
    def __init__(self, values):
        self.values = iter(values)
        self.added = []
        self.commits = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def scalar(self, query):
        return next(self.values)

    async def execute(self, *args):
        return None

    async def flush(self):
        pass

    def add(self, obj):
        if isinstance(obj, TicketMessage):
            obj.id = 91
        elif isinstance(obj, User):
            obj.id = 92
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def ticket():
    return SimpleNamespace(id=3, user_id=7, status='open', is_user_reply_blocked=False, last_sla_reminder_at='old')


@pytest.fixture(autouse=True)
def delivery(monkeypatch):
    monkeypatch.setattr(service.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(service.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=None))
    monkeypatch.setattr(service.event_emitter, 'emit', AsyncMock())
    monkeypatch.setattr(service, '_deliver_reply', AsyncMock())


@pytest.mark.parametrize('answered,status', [(True, 'answered'), (False, 'open')])
async def test_publish_status_and_exactly_one_event(monkeypatch, ticket, answered, status):
    db = Db([ticket, SimpleNamespace(id=10, is_from_admin=False)])
    monkeypatch.setattr(service, 'AsyncSessionLocal', lambda: db)
    result = await service.publish_support_reply(
        ticket_id=3, author_user_id=5, text='Ответ', mark_answered=answered, origin='ai', trigger_message_id=10
    )
    assert result == 91
    assert ticket.status == status
    assert ticket.last_sla_reminder_at is None
    assert db.commits == 1
    assert db.closed
    assert db.added[0].is_from_admin is True
    service.event_emitter.emit.assert_awaited_once()
    assert service.event_emitter.emit.call_args.args == (
        'ticket.message_added',
        {
            'ticket_id': 3,
            'message_id': 91,
            'user_id': 5,
            'is_from_admin': True,
            'message_text': 'Ответ',
            'has_media': False,
            'status': status,
        },
    )
    service._deliver_reply.assert_awaited_once_with(3, 'Ответ', 91)


@pytest.mark.parametrize('guard', ['closed', 'missing', 'blocked', 'newer', 'admin', 'no_trigger', 'disabled'])
async def test_publish_ai_rechecks_before_commit(monkeypatch, ticket, guard):
    trigger = 10
    last = SimpleNamespace(id=10, is_from_admin=False)
    if guard == 'closed':
        ticket.status = 'closed'
    elif guard == 'missing':
        ticket = None
    elif guard == 'blocked':
        ticket.is_user_reply_blocked = True
    elif guard == 'newer':
        last.id = 11
    elif guard == 'admin':
        last.is_from_admin = True
    elif guard == 'no_trigger':
        trigger = None
    elif guard == 'disabled':
        monkeypatch.setattr(service.SupportSettingsService, 'is_tickets_enabled', lambda: False)
    db = Db([ticket, last])
    monkeypatch.setattr(service, 'AsyncSessionLocal', lambda: db)
    assert (
        await service.publish_support_reply(
            ticket_id=3, author_user_id=5, text='Ответ', mark_answered=True, origin='ai', trigger_message_id=trigger
        )
        is None
    )
    assert not db.added
    assert not db.commits
    service.event_emitter.emit.assert_not_awaited()


async def test_system_author_has_no_login_or_admin_rights(monkeypatch, ticket):
    db = Db([ticket, SimpleNamespace(id=10, is_from_admin=False), None])
    monkeypatch.setattr(service, 'AsyncSessionLocal', lambda: db)
    await service.publish_support_reply(
        ticket_id=3, author_user_id=None, text='ИИ', mark_answered=True, origin='ai', trigger_message_id=10
    )
    author = next(obj for obj in db.added if isinstance(obj, User))
    assert author.auth_type == 'ai_support'
    assert author.telegram_id is None
    assert author.email is None
    assert author.password_hash is None
    reply = next(obj for obj in db.added if isinstance(obj, TicketMessage))
    assert reply.user_id == author.id


async def test_human_draft_is_audited_as_human(monkeypatch, ticket):
    db = Db([ticket, SimpleNamespace(id=10, is_from_admin=False)])
    monkeypatch.setattr(service, 'AsyncSessionLocal', lambda: db)
    await service.publish_support_reply(
        ticket_id=3, author_user_id=5, text='От человека', mark_answered=True, origin='human', trigger_message_id=10
    )
    audit = next(obj for obj in db.added if isinstance(obj, SupportAuditLog))
    assert audit.action == 'ai_support_human_reply'
    assert audit.details == {'message_id': 91}


@pytest.mark.parametrize('toggle', [True, False])
async def test_delivery_all_channels_with_independent_toggles(monkeypatch, ticket, toggle, delivery):
    from app.cabinet.routes import websocket
    from app.handlers.admin import tickets
    from app.services.maintenance_service import maintenance_service

    monkeypatch.undo()
    db = Db([])
    monkeypatch.setattr(service, 'AsyncSessionLocal', lambda: db)
    monkeypatch.setattr(service.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=ticket))
    monkeypatch.setattr(service.SupportSettingsService, 'get_user_ticket_notifications_enabled', lambda: toggle)
    exact_reply = SimpleNamespace(id=91)
    db.get = AsyncMock(return_value=exact_reply)
    calls = []

    async def exact_notify(bot, target, text, db, *, reply_message=None):
        calls.append(reply_message)

    monkeypatch.setattr(tickets, 'notify_user_about_ticket_reply', exact_notify)
    monkeypatch.setattr(maintenance_service, '_bot', object())
    cabinet_notice = AsyncMock(return_value=object())
    ws = AsyncMock()
    monkeypatch.setattr(service.TicketNotificationCRUD, 'create_user_notification_for_admin_reply', cabinet_notice)
    monkeypatch.setattr(websocket, 'notify_user_ticket_reply', ws)
    await service._deliver_reply(3, 'Ответ', 91)
    assert calls == ([exact_reply] if toggle else [])
    cabinet_notice.assert_awaited_once_with(db, ticket, 'Ответ')
    ws.assert_awaited_once_with(7, 3, 'Ответ')


async def test_delivery_failure_does_not_skip_cabinet(monkeypatch, ticket):
    from app.cabinet.routes import websocket
    from app.handlers.admin import tickets

    original_deliver = service._deliver_reply
    # Фикстура подменила только точку вызова; проверяем реальную реализацию ниже.
    monkeypatch.undo()
    assert original_deliver is not service._deliver_reply
    monkeypatch.setattr(service, 'AsyncSessionLocal', lambda: Db([]))
    monkeypatch.setattr(service.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=ticket))
    monkeypatch.setattr(service.SupportSettingsService, 'get_user_ticket_notifications_enabled', lambda: True)
    monkeypatch.setattr(tickets, 'notify_user_about_ticket_reply', AsyncMock(side_effect=RuntimeError('offline')))
    cabinet_notice = AsyncMock(return_value=object())
    ws = AsyncMock()
    monkeypatch.setattr(service.TicketNotificationCRUD, 'create_user_notification_for_admin_reply', cabinet_notice)
    monkeypatch.setattr(websocket, 'notify_user_ticket_reply', ws)
    await service._deliver_reply(3, 'Ответ', 91)
    cabinet_notice.assert_awaited_once()
    ws.assert_awaited_once()
