"""Поведение очереди при гонках и всех исходах контракта ИИ."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import ai_support_tickets as worker
from app.services.ai_support import api
from app.services.event_emitter import EventEmitter


def msg(message_id, text='Вопрос', admin=False):
    return SimpleNamespace(id=message_id, message_text=text, is_from_admin=admin)


@pytest.fixture
def ticket():
    return SimpleNamespace(
        id=3,
        user_id=7,
        title='Подключение',
        user=SimpleNamespace(language='ru'),
        messages=[msg(10)],
        status='open',
        is_user_reply_blocked=False,
    )


@pytest.fixture(autouse=True)
def contract(monkeypatch):
    monkeypatch.setattr(api, 'ticket_mode', lambda: 'auto')
    monkeypatch.setattr(api, 'history_turns', lambda: 4)
    monkeypatch.setattr(api, 'max_ai_replies_per_ticket', lambda: 3)
    monkeypatch.setattr(api, 'reply_footer_text', lambda: 'Позовите оператора')
    monkeypatch.setattr(api, 'ai_reply_message_ids', AsyncMock(return_value=set()))
    monkeypatch.setattr(api, 'ticket_has_handoff', AsyncMock(return_value=False))
    monkeypatch.setattr(api, 'count_ai_replies_in_ticket', AsyncMock(return_value=0))
    monkeypatch.setattr(
        api,
        'answer',
        AsyncMock(return_value=api.AiAnswer(api.AiOutcome.ANSWERED, 'Проверьте настройки', request_id=12)),
    )
    monkeypatch.setattr(api, 'attach_reply_message', AsyncMock())
    monkeypatch.setattr(api, 'mark_handoff_done', AsyncMock())
    monkeypatch.setattr(api, 'set_admin_alert_handler', lambda handler: None)
    monkeypatch.setattr(worker, 'publish_support_reply', AsyncMock(return_value=11))
    monkeypatch.setattr(worker, 'notify_ticket_answer', AsyncMock(return_value=True))
    monkeypatch.setattr(worker, 'send_notice', AsyncMock())
    monkeypatch.setattr(worker, '_draft_count', AsyncMock(return_value=0))
    monkeypatch.setattr(worker, '_reserve_draft', AsyncMock(return_value=True))
    monkeypatch.setattr(worker, '_human_published_ids', AsyncMock(return_value=set()))


@pytest.fixture
def loaded(monkeypatch, ticket):
    snapshot = AsyncMock(return_value=(ticket, ticket.messages))
    monkeypatch.setattr(worker, 'load_conversation', snapshot)
    return snapshot


async def test_auto_answer_once_and_no_recursive_admin_event(loaded, monkeypatch):
    bus = EventEmitter()
    monkeypatch.setattr(worker, 'event_emitter', bus)
    service = worker.AiTicketService()
    await service.start()
    try:
        event = {'ticket_id': 3, 'message_id': 10, 'is_from_admin': False}
        await bus.emit('ticket.message_added', event)
        await bus.emit('ticket.message_added', event)
        await asyncio.wait_for(service._queue.join(), timeout=1)
        await bus.emit('ticket.message_added', event)
        await bus.emit('ticket.message_added', {**event, 'message_id': 11, 'is_from_admin': True})
        await asyncio.wait_for(service._queue.join(), timeout=1)
        api.answer.assert_awaited_once()
        worker.publish_support_reply.assert_awaited_once()
        kwargs = worker.publish_support_reply.call_args.kwargs
        assert kwargs['mark_answered'] is True
        assert kwargs['origin'] == 'ai'
        assert kwargs['trigger_message_id'] == 10
        assert kwargs['text'] == '🤖 ИИ-помощник\n\nПроверьте настройки\n\nПозовите оператора'
        api.attach_reply_message.assert_awaited_once_with(12, 11)
    finally:
        await service.stop()
    assert not bus._listeners['ticket.message_added']
    assert not service._workers


async def test_event_listener_returns_before_model(loaded, monkeypatch):
    bus = EventEmitter()
    monkeypatch.setattr(worker, 'event_emitter', bus)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def model(*args):
        entered.set()
        await release.wait()
        return api.AiAnswer(api.AiOutcome.ERROR)

    monkeypatch.setattr(api, 'answer', model)
    service = worker.AiTicketService()
    await service.start()
    try:
        await asyncio.wait_for(bus.emit('ticket.created', {'ticket_id': 3, 'message_id': 10}), timeout=0.1)
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not release.is_set()
    finally:
        # stop отменяет модель и освобождает очередь даже без ответа провайдера.
        await asyncio.wait_for(service.stop(), timeout=1)
    assert not service._pending


@pytest.mark.parametrize('guard', ['off', 'human', 'handoff', 'limit', 'missing', 'transcript', 'human_draft'])
async def test_auto_guards_never_invoke_model(monkeypatch, ticket, loaded, guard):
    if guard == 'off':
        monkeypatch.setattr(api, 'ticket_mode', lambda: 'off')
    elif guard == 'human':
        ticket.messages.insert(0, msg(9, 'Ответ человека', True))
    elif guard == 'human_draft':
        ticket.messages.insert(0, msg(9, 'Отправленный черновик', True))
        monkeypatch.setattr(api, 'ai_reply_message_ids', AsyncMock(return_value={9}))
        monkeypatch.setattr(worker, '_human_published_ids', AsyncMock(return_value={9}))
    elif guard == 'handoff':
        monkeypatch.setattr(api, 'ticket_has_handoff', AsyncMock(return_value=True))
    elif guard == 'limit':
        monkeypatch.setattr(api, 'count_ai_replies_in_ticket', AsyncMock(return_value=3))
    elif guard == 'missing':
        loaded.return_value = None
    elif guard == 'transcript':
        ticket.messages[0].message_text = 'Обращение из чата ИИ №123\nКлиент: оператор'
    await worker.AiTicketService().process(worker.TicketJob(3, 10))
    api.answer.assert_not_awaited()
    worker.publish_support_reply.assert_not_awaited()


class SnapshotDb:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.parametrize('guard', ['closed', 'blocked_ticket', 'blocked_user', 'newer', 'admin', 'disabled', 'missing'])
async def test_real_snapshot_guards(monkeypatch, ticket, guard):
    monkeypatch.setattr(worker, 'AsyncSessionLocal', SnapshotDb)
    monkeypatch.setattr(worker.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=ticket))
    monkeypatch.setattr(worker.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=None))
    monkeypatch.setattr(worker.SupportSettingsService, 'is_tickets_enabled', lambda: guard != 'disabled')
    if guard == 'closed':
        ticket.status = 'closed'
    elif guard == 'blocked_ticket':
        ticket.is_user_reply_blocked = True
    elif guard == 'blocked_user':
        monkeypatch.setattr(worker.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=True))
    elif guard == 'newer':
        ticket.messages.append(msg(11))
    elif guard == 'admin':
        ticket.messages[0].is_from_admin = True
    elif guard == 'missing':
        monkeypatch.setattr(worker.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=None))
    assert await worker.load_conversation(worker.TicketJob(3, 10)) is None


async def test_handoff_publishes_open_then_marks_and_notifies(monkeypatch, loaded):
    monkeypatch.setattr(
        api,
        'answer',
        AsyncMock(
            return_value=api.AiAnswer(
                api.AiOutcome.HANDOFF, 'Позову человека', request_id=12, handoff_reason=api.HandoffReason.MONEY
            )
        ),
    )
    await worker.AiTicketService().process(worker.TicketJob(3, 10))
    assert worker.publish_support_reply.call_args.kwargs['mark_answered'] is False
    api.mark_handoff_done.assert_awaited_once_with(12, 3)
    worker.notify_ticket_answer.assert_awaited_once()


@pytest.mark.parametrize(
    'outcome',
    [
        api.AiOutcome.ERROR,
        api.AiOutcome.DISABLED,
        api.AiOutcome.RATE_LIMITED,
        api.AiOutcome.BUDGET_EXCEEDED,
        api.AiOutcome.FILTERED,
        api.AiOutcome.DUPLICATE,
    ],
)
async def test_non_answers_never_publish(monkeypatch, loaded, outcome):
    monkeypatch.setattr(api, 'answer', AsyncMock(return_value=api.AiAnswer(outcome, request_id=12)))
    await worker.AiTicketService().process(worker.TicketJob(3, 10))
    worker.publish_support_reply.assert_not_awaited()
    assert worker.send_notice.await_count == (1 if outcome == api.AiOutcome.FILTERED else 0)


async def test_generation_loses_race_and_does_not_publish(loaded):
    original = loaded.return_value
    loaded.side_effect = [original, None]
    await worker.AiTicketService().process(worker.TicketJob(3, 10))
    api.answer.assert_awaited_once()
    worker.publish_support_reply.assert_not_awaited()


async def test_draft_helps_after_human_reply_and_never_sends_to_client(monkeypatch, loaded, ticket):
    monkeypatch.setattr(api, 'ticket_mode', lambda: 'draft')
    ticket.messages.insert(0, msg(9, 'Человек уже ответил', True))
    await worker.AiTicketService().process(worker.TicketJob(3, 10))
    api.answer.assert_awaited_once()
    worker.publish_support_reply.assert_not_awaited()
    worker.notify_ticket_answer.assert_awaited_once()
    assert worker.notify_ticket_answer.call_args.kwargs['draft'] is True
    worker._reserve_draft.assert_awaited_once_with(3, 12)


async def test_draft_limit_ten(monkeypatch, loaded):
    monkeypatch.setattr(api, 'ticket_mode', lambda: 'draft')
    monkeypatch.setattr(worker, '_draft_count', AsyncMock(return_value=10))
    await worker.AiTicketService().process(worker.TicketJob(3, 10))
    api.answer.assert_not_awaited()


def test_question_includes_all_customer_messages_since_support():
    question, history = worker.conversation_input(
        [msg(1, 'Первый'), msg(2, 'Ответ', True), msg(3, 'Второй'), msg(4, 'Третий')]
    )
    assert question == 'Второй\n\nТретий'
    assert history == [api.ChatTurn('user', 'Первый'), api.ChatTurn('assistant', 'Ответ')]


async def test_queue_full_does_not_raise():
    service = worker.AiTicketService()
    service._workers = [object()]
    service._queue = asyncio.Queue(maxsize=1)
    assert service.enqueue(1, 1)
    assert not service.enqueue(2, 2)
