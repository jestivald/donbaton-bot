"""Серверные границы закрытия ИИ, включая отложенное решение после генерации."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import TicketMessage
from app.services import ai_support_tickets as worker, ticket_automation_service as automation
from app.services.ai_support import api, prompts, tools
from tests.services.test_ticket_automation import NOW, ai_record, automation_db


def context(**kwargs):
    return api.AiRequestContext(api.AiChannel.TICKET, 1, ticket_id=1, trigger_message_id=1, **kwargs)


async def call_close(ctx=None, state=None, args=None):
    return await tools.execute(
        {'function': {'name': 'close_ticket', 'arguments': json.dumps(args or {})}},
        ctx or context(),
        SimpleNamespace(share_account=False),
        state or tools.ToolState(),
    )


async def test_close_reserves_then_closes_with_notice_and_is_idempotent(monkeypatch):
    async with automation_db(monkeypatch, text='Вопрос решён, закрывайте', admin=False, status='open') as (db, ticket):
        monkeypatch.setattr(api, 'ticket_mode', lambda: 'auto')
        assert (await call_close())['accepted']
        assert ticket.status == 'open'  # До финального исхода модели закрытия нет.
        message_id = await automation.finish_ai_close(context())
        assert message_id is not None and ticket.status == 'closed'
        message = await db.get(TicketMessage, message_id)
        assert message.is_from_admin and 'просьбе' in message.message_text
        automation._deliver_reply.assert_awaited_once()
        assert await automation.finish_ai_close(context()) is None


@pytest.mark.parametrize(
    'guard',
    [
        'owner',
        'other_ticket',
        'closed',
        'newer',
        'thanks',
        'negation',
        'blocked',
        'off',
        'draft',
        'bot',
        'test',
        'switch',
        'arguments',
    ],
)
async def test_server_guards(monkeypatch, guard):
    async with automation_db(monkeypatch, text='Закройте обращение', admin=False, status='open') as (db, ticket):
        monkeypatch.setattr(api, 'ticket_mode', lambda: 'auto')
        ctx = context()
        args = None
        if guard == 'owner':
            ctx = replace(ctx, user_id=2)
        elif guard == 'other_ticket':
            ctx = replace(ctx, ticket_id=2)
        elif guard == 'closed':
            ticket.status = 'closed'
        elif guard == 'newer':
            db.add(TicketMessage(ticket_id=1, user_id=1, message_text='Подождите', is_from_admin=False))
        elif guard in {'thanks', 'negation'}:
            last = await db.get(TicketMessage, 1)
            last.message_text = 'Спасибо, всё работает' if guard == 'thanks' else 'Не закрывайте обращение'
        elif guard == 'blocked':
            ticket.user_reply_block_permanent = True
        elif guard in {'off', 'draft'}:
            monkeypatch.setattr(api, 'ticket_mode', lambda: guard)
        elif guard in {'bot', 'test'}:
            ctx = replace(ctx, channel=api.AiChannel.BOT_CHAT if guard == 'bot' else api.AiChannel.ADMIN_TEST)
        elif guard == 'switch':
            monkeypatch.setattr(settings, 'AI_SUPPORT_CAN_CLOSE_TICKETS', False)
        elif guard == 'arguments':
            args = {'ticket_id': 2}
        await db.commit()
        assert await call_close(ctx, args=args) == tools.UNAVAILABLE
        assert ticket.status != 'closed' or guard == 'closed'
        automation._deliver_reply.assert_not_awaited()


@pytest.mark.parametrize(
    'reason,done', [('money', False), ('refund', True), ('client_request', True), ('other', False)]
)
async def test_handoff_and_money_never_close(monkeypatch, reason, done):
    async with automation_db(monkeypatch, text='Закрывайте', admin=False, status='open') as (db, ticket):
        monkeypatch.setattr(api, 'ticket_mode', lambda: 'auto')
        db.add(ai_record(outcome='handoff', handoff_reason=reason, handoff_done_at=NOW if done else None))
        await db.commit()
        assert await call_close() == tools.UNAVAILABLE
        assert ticket.status == 'open'


async def test_handoff_in_same_tool_round_blocks_close(monkeypatch):
    async with automation_db(monkeypatch, text='Закрывайте', admin=False, status='open') as (_, ticket):
        monkeypatch.setattr(api, 'ticket_mode', lambda: 'auto')
        assert await call_close(state=tools.ToolState(reason=api.HandoffReason.MONEY)) == tools.UNAVAILABLE
        assert ticket.status == 'open'


async def test_new_handoff_or_client_reply_after_reservation_blocks_final_close(monkeypatch):
    async with automation_db(monkeypatch, text='Закрывайте', admin=False, status='open') as (db, ticket):
        monkeypatch.setattr(api, 'ticket_mode', lambda: 'auto')
        assert (await call_close())['accepted']
        db.add(ai_record(outcome='handoff', handoff_reason='money'))
        await db.commit()
        assert await automation.finish_ai_close(context()) is None
        assert ticket.status == 'open'


@pytest.mark.parametrize(
    'channel,mode,enabled,expected',
    [
        (api.AiChannel.TICKET, 'auto', True, True),
        (api.AiChannel.TICKET, 'draft', True, False),
        (api.AiChannel.BOT_CHAT, 'auto', True, False),
        (api.AiChannel.ADMIN_TEST, 'auto', True, False),
        (api.AiChannel.TICKET, 'auto', False, False),
    ],
)
def test_tool_exposed_only_for_automatic_ticket_channel(monkeypatch, channel, mode, enabled, expected):
    monkeypatch.setattr(api, 'ticket_mode', lambda: mode)
    monkeypatch.setattr(settings, 'AI_SUPPORT_CAN_CLOSE_TICKETS', enabled)
    names = {
        item['function']['name']
        for item in tools.definitions(replace(context(), channel=channel), SimpleNamespace(share_account=False))
    }
    assert ('close_ticket' in names) == expected


def test_prompt_requires_explicit_request():
    assert 'только по явной просьбе' in prompts.SYSTEM_RULES
    assert 'Не закрывай по своей инициативе' in prompts.SYSTEM_RULES


async def test_finish_without_model_tool_request_does_nothing(monkeypatch):
    async with automation_db(monkeypatch, text='Закрывайте', admin=False, status='open') as (db, ticket):
        monkeypatch.setattr(api, 'ticket_mode', lambda: 'auto')
        assert await automation.finish_ai_close(context()) is None
        assert ticket.status == 'open'
        assert len(list((await db.scalars(select(TicketMessage))).all())) == 1


@pytest.mark.parametrize(
    'text,expected',
    [
        ('Вопрос решён, закрывайте', True),
        ('Можно закрыть обращение', True),
        ('Close my ticket please', True),
        ('Спасибо, всё работает', False),
        ('Как закрыть обращение?', False),
        ('Если поможет, закрывайте', False),
        ('На экране кнопка закрыть обращение', False),
        ('Не закрывайте', False),
    ],
)
def test_explicit_close_request(text, expected):
    assert automation.explicit_close_request(text) == expected


@pytest.mark.parametrize(
    'outcome,mode,closed',
    [
        (api.AiOutcome.ANSWERED, 'auto', True),
        (api.AiOutcome.HANDOFF, 'auto', False),
        (api.AiOutcome.ERROR, 'auto', False),
        (api.AiOutcome.ANSWERED, 'draft', False),
    ],
)
async def test_worker_finalizes_only_successful_automatic_reply(monkeypatch, outcome, mode, closed):
    prepare_worker(monkeypatch, outcome, mode)
    await worker.AiTicketService().process(worker.TicketJob(1, 1, 1))
    assert worker.finish_ai_close.await_count == int(closed)
    if closed:
        worker.publish_support_reply.assert_not_awaited()
        api.attach_reply_message.assert_awaited_once_with(10, 2)


def prepare_worker(monkeypatch, outcome, mode='auto'):
    ticket = SimpleNamespace(id=1, user_id=1, title='VPN', user=SimpleNamespace(language='ru'))
    messages = [SimpleNamespace(id=1, user_id=1, message_text='Вопрос решён, закрывайте', is_from_admin=False)]
    monkeypatch.setattr(worker, 'load_conversation', AsyncMock(return_value=(ticket, messages)))
    monkeypatch.setattr(worker, '_has_human_reply', AsyncMock(return_value=False))
    monkeypatch.setattr(worker, '_draft_count', AsyncMock(return_value=0))
    monkeypatch.setattr(worker, '_reserve_draft', AsyncMock(return_value=True))
    monkeypatch.setattr(api, 'ticket_mode', lambda: mode)
    monkeypatch.setattr(api, 'ticket_has_handoff', AsyncMock(return_value=False))
    monkeypatch.setattr(api, 'count_ai_replies_in_ticket', AsyncMock(return_value=0))
    monkeypatch.setattr(api, 'answer', AsyncMock(return_value=api.AiAnswer(outcome, 'Готово', request_id=10)))
    monkeypatch.setattr(api, 'attach_reply_message', AsyncMock())
    monkeypatch.setattr(api, 'mark_handoff_done', AsyncMock())
    monkeypatch.setattr(worker, 'finish_ai_close', AsyncMock(return_value=2))
    monkeypatch.setattr(worker, 'publish_support_reply', AsyncMock(return_value=2))
    monkeypatch.setattr(worker, 'notify_ticket_answer', AsyncMock())


@pytest.mark.parametrize('outcome', [api.AiOutcome.HANDOFF, api.AiOutcome.ANSWERED])
async def test_worker_appends_night_text_only_on_handoff(monkeypatch, outcome):
    prepare_worker(monkeypatch, outcome)
    monkeypatch.setattr(worker, 'finish_ai_close', AsyncMock(return_value=None))
    monkeypatch.setattr(automation, 'is_quiet_now', lambda: True)
    monkeypatch.setattr(automation, 'quiet_end_label', lambda: '09:00')
    monkeypatch.setattr(settings, 'SUPPORT_NIGHT_AUTOREPLY_ENABLED', True)
    await worker.AiTicketService().process(worker.TicketJob(1, 1, 1))
    assert ('09:00' in worker.publish_support_reply.call_args.kwargs['text']) == (outcome == api.AiOutcome.HANDOFF)
