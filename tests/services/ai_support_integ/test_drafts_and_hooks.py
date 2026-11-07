"""Отправка черновика и стыки источников сообщений с очередью."""

import ast
import asyncio
import html
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import support_ws
from app.database.crud.ticket import TicketCRUD
from app.handlers.admin import ai_support as admin
from app.services import ai_support_notify as notice, ai_support_tickets as worker
from app.services.ai_support import api


def request_record(**overrides):
    values = {
        'id': 123,
        'channel': api.AiChannel.TICKET,
        'outcome': api.AiOutcome.ANSWERED,
        'user_id': 7,
        'ticket_id': 3,
        'trigger_message_id': 10,
        'reply_message_id': None,
        'answer_text': 'Попробуйте подключиться заново',
        'handoff_reason': None,
        'operator_summary': None,
        'feedback': None,
        'created_at': datetime.now(UTC),
    }
    values.update(overrides)
    return api.AiRequestRecord(**values)


@pytest.fixture
def draft(monkeypatch):
    record = request_record()
    monkeypatch.setattr(type(admin.settings), 'is_admin', lambda self, user_id: user_id == 77)
    monkeypatch.setattr(admin.SupportSettingsService, 'is_moderator', lambda user_id: user_id == 88)
    monkeypatch.setattr(api, 'get_request', AsyncMock(return_value=record))
    monkeypatch.setattr(admin, 'publish_support_reply', AsyncMock(return_value=11))

    async def attach(request_id, message_id):
        record.reply_message_id = message_id

    monkeypatch.setattr(api, 'attach_reply_message', AsyncMock(side_effect=attach))
    callback = SimpleNamespace(
        data='aisd:123',
        from_user=SimpleNamespace(id=77),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_reply_markup=AsyncMock()),
    )
    return record, callback, SimpleNamespace(id=5, language='ru')


@pytest.mark.parametrize('telegram_id', [77, 88, 99])
async def test_draft_rights_and_idempotency(draft, telegram_id):
    record, callback, user = draft
    callback.from_user.id = telegram_id
    await asyncio.gather(admin.send_draft(callback, user), admin.send_draft(callback, user))
    if telegram_id == 99:
        admin.publish_support_reply.assert_not_awaited()
        api.get_request.assert_not_awaited()
    else:
        admin.publish_support_reply.assert_awaited_once_with(
            ticket_id=3,
            author_user_id=user.id,
            text=record.answer_text,
            mark_answered=True,
            origin='human',
            trigger_message_id=10,
        )
        assert not admin.publish_support_reply.call_args.kwargs['text'].startswith(api.AI_REPLY_PREFIX)
        api.attach_reply_message.assert_awaited_once_with(123, 11)


@pytest.mark.parametrize(
    'change',
    [
        {'channel': api.AiChannel.BOT_CHAT},
        {'reply_message_id': 11},
        {'trigger_message_id': None},
        {'answer_text': None},
        {'outcome': api.AiOutcome.FILTERED},
    ],
)
async def test_invalid_draft_cannot_be_published(draft, change):
    record, callback, user = draft
    for key, value in change.items():
        setattr(record, key, value)
    await admin.send_draft(callback, user)
    admin.publish_support_reply.assert_not_awaited()


async def test_stale_draft_not_attached(draft, monkeypatch):
    _, callback, user = draft
    monkeypatch.setattr(admin, 'publish_support_reply', AsyncMock(return_value=None))
    await admin.send_draft(callback, user)
    api.attach_reply_message.assert_not_awaited()
    assert 'новые сообщения' in callback.answer.call_args.args[0]


class CreateDb:
    async def commit(self):
        pass

    async def flush(self):
        pass

    async def refresh(self, obj):
        pass

    def add(self, obj):
        obj.id = 15 if isinstance(obj, support_ws.TicketMessage) else 3


async def test_crud_created_event_contains_first_message_id(monkeypatch):
    from app.services.event_emitter import event_emitter

    emit = AsyncMock()
    monkeypatch.setattr(event_emitter, 'emit', emit)
    await TicketCRUD.create_ticket(CreateDb(), 7, 'Вопрос', 'Текст вопроса')
    assert emit.call_args.args[0] == 'ticket.created'
    assert emit.call_args.args[1]['ticket_id'] == 3
    assert emit.call_args.args[1]['message_id'] == 15


def test_rest_created_payload_and_registration_order():
    root = Path(__file__).resolve().parents[3]
    tree = ast.parse((root / 'app/cabinet/routes/tickets.py').read_text())
    create = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'create_ticket')
    emit = next(
        node
        for node in ast.walk(create)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'emit'
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == 'ticket.created'
    )
    payload = dict(zip([key.value for key in emit.args[1].keys], emit.args[1].values, strict=True))
    assert ast.unparse(payload['message_id']) == 'message.id'
    bot_source = (root / 'app/bot.py').read_text()
    assert bot_source.index('ai_support.register_handlers(dp)\n    common.register_handlers(dp)') > 0


async def test_mobile_ws_enqueues_after_commit_only_for_customer(monkeypatch):
    calls = []
    ticket = SimpleNamespace(
        id=3,
        user_id=7,
        status='open',
        is_user_reply_blocked=False,
        messages=[],
        user=None,
        title='Вопрос',
        priority='normal',
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        closed_at=None,
    )

    class ReplyDb(CreateDb):
        async def commit(self):
            calls.append('commit')

        async def refresh(self, obj, attrs=None):
            pass

    def enqueue(ticket_id, message_id):
        assert 'commit' in calls
        calls.append(('enqueue', ticket_id, message_id))

    monkeypatch.setattr(worker, 'enqueue_ticket_message', enqueue)
    monkeypatch.setattr(support_ws, '_get_visible_ticket', AsyncMock(return_value=ticket))
    monkeypatch.setattr(support_ws, '_can_reply_ticket', AsyncMock(return_value=True))
    monkeypatch.setattr(support_ws, '_require_ws_permission', AsyncMock())
    monkeypatch.setattr(support_ws.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(support_ws.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=None))
    monkeypatch.setattr(
        support_ws.TicketNotificationCRUD, 'create_admin_notification_for_user_reply', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        support_ws.TicketNotificationCRUD, 'create_user_notification_for_admin_reply', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(support_ws, '_notify_ticket_reply_via_telegram', AsyncMock())
    monkeypatch.setattr(support_ws.support_ws_manager, 'broadcast_ticket_event', AsyncMock())
    for role in ('owner', 'admin'):
        context = SimpleNamespace(role=role, user_id=7)
        session = support_ws.SupportWsSession(websocket=SimpleNamespace(), context=context)
        await support_ws._handle_ticket_reply(ReplyDb(), session, {'ticketId': '3', 'body': 'Мой вопрос'})
    assert calls == ['commit', ('enqueue', 3, 15), 'commit']


async def test_notice_escapes_inserts_and_uses_existing_ticket_delivery(monkeypatch):
    monkeypatch.setattr(api, 'notify_admins_enabled', lambda: True)
    monkeypatch.setattr(notice.maintenance_service, '_bot', object())
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notice.AdminNotificationService, 'send_ticket_event_notification', send)
    assert await notice.send_notice('<Заголовок>', '<текст>' * 1000, ticket_id=3, request_id=123)
    text, keyboard = send.call_args.args
    assert '&lt;Заголовок&gt;' in text
    assert '<текст>' not in text
    assert len(html.unescape(text)) < 3500
    assert keyboard.inline_keyboard[0][0].callback_data == 'aisd:123'
    assert keyboard.inline_keyboard[1][0].callback_data == 'admin_view_ticket_3'


async def test_notice_respects_ai_toggle(monkeypatch):
    monkeypatch.setattr(api, 'notify_admins_enabled', lambda: False)
    send = AsyncMock()
    monkeypatch.setattr(notice.AdminNotificationService, 'send_ticket_event_notification', send)
    assert not await notice.send_notice('Тема', 'Текст')
    send.assert_not_awaited()


async def test_drafts_and_core_alerts_use_support_toggles_even_if_copies_disabled(monkeypatch):
    monkeypatch.setattr(api, 'notify_admins_enabled', lambda: False)
    monkeypatch.setattr(notice.maintenance_service, '_bot', object())
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notice.AdminNotificationService, 'send_ticket_event_notification', send)
    await notice.notify_ticket_answer(3, api.AiAnswer(api.AiOutcome.ANSWERED, 'Черновик', request_id=123), draft=True)
    await notice.admin_alert('balance', 'Пополните баланс провайдера')
    assert send.await_count == 2


async def test_handoff_draft_has_no_send_button(monkeypatch):
    from app.services import ai_support_notify as notify

    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notify, 'send_notice', sent)
    result = api.AiAnswer(api.AiOutcome.HANDOFF, 'Передано', handoff_reason=api.HandoffReason.MONEY, request_id=31)
    await notify.notify_ticket_answer(5, result, draft=True)
    assert sent.call_args.kwargs['request_id'] is None
