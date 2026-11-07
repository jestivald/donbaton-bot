"""Контракт личного чата: маршрутизация, деградация, память и передача оператору."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher, types
from aiogram.filters import StateFilter

from app.handlers import ai_support as chat
from app.handlers.admin import ai_support as admin
from app.services.ai_support import api


@pytest.fixture
def user():
    return SimpleNamespace(id=7, language='ru')


@pytest.fixture
def message():
    return SimpleNamespace(
        text='Как подключиться?',
        chat=SimpleNamespace(id=77),
        answer=AsyncMock(),
        bot=SimpleNamespace(send_chat_action=AsyncMock()),
    )


@pytest.fixture(autouse=True)
def contract(monkeypatch):
    monkeypatch.setattr(api, 'bot_chat_enabled', lambda: True)
    monkeypatch.setattr(api, 'history_turns', lambda: 6)
    monkeypatch.setattr(api, 'chat_memory_ttl_seconds', lambda: 300)
    monkeypatch.setattr(
        api, 'answer', AsyncMock(return_value=api.AiAnswer(api.AiOutcome.ANSWERED, '<Настройки>', request_id=123))
    )
    monkeypatch.setattr(chat.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(chat.cache, 'set', AsyncMock(return_value=True))
    monkeypatch.setattr(api, 'mark_handoff_done', AsyncMock())
    chat._request_memory.clear()


async def test_disabled_preserves_original_unknown_response(monkeypatch, message, user):
    monkeypatch.setattr(api, 'bot_chat_enabled', lambda: False)
    await chat.handle_ai_message(message, user)
    first = message.answer.call_args
    message.answer.reset_mock()
    await chat.handle_unknown_message(message, user)
    assert first == message.answer.call_args
    assert first.args[0] == '❓ Не понимаю эту команду. Используйте кнопки меню.'
    api.answer.assert_not_awaited()


async def test_unimplemented_settings_do_not_break_fallback(monkeypatch, message, user):
    def unavailable():
        raise NotImplementedError

    monkeypatch.setattr(api, 'bot_chat_enabled', unavailable)
    await chat.handle_ai_message(message, user)
    assert 'Не понимаю' in message.answer.call_args.args[0]


@pytest.mark.parametrize(
    ('chat_type', 'text', 'bot', 'state', 'expected'),
    [
        ('private', 'вопрос', False, None, True),
        ('group', 'вопрос', False, None, False),
        ('private', '/start', False, None, False),
        ('private', None, False, None, False),
        ('private', 'вопрос', True, None, False),
        ('private', 'вопрос', False, 'SomeState', False),
    ],
)
async def test_private_text_and_state_filters(chat_type, text, bot, state, expected):
    dp = Dispatcher()
    chat.register_handlers(dp)
    handler = dp.message.handlers[0]
    assert any(isinstance(item.callback, StateFilter) for item in handler.filters)
    event = types.Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=types.Chat(id=77, type=chat_type),
        from_user=types.User(id=77, is_bot=bot, first_name='Клиент'),
        text=text,
    )
    accepted, _ = await handler.check(event, raw_state=state)
    assert accepted is expected


async def test_answer_escaped_disclosed_and_remembered(message, user):
    await chat.handle_ai_message(message, user)
    sent = message.answer.call_args
    assert '&lt;Настройки&gt;' in sent.args[0]
    assert 'Я ИИ-помощник' in sent.args[0]
    assert sent.kwargs['parse_mode'] == 'HTML'
    buttons = [button for row in sent.kwargs['reply_markup'].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == ['aif:123:1', 'aif:123:0', 'aio:123']
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    api.answer.assert_awaited_once()
    assert api.answer.call_args.args[2].user_id == user.id
    assert chat.cache.set.call_args.args[0] == 'ai_support:chat:7'
    assert chat.cache.set.call_args.args[2] == 300


async def test_history_excludes_duplicate_disclosure(monkeypatch, message, user):
    monkeypatch.setattr(chat.cache, 'get', AsyncMock(return_value=[{'role': 'user', 'text': 'Привет'}]))
    await chat.handle_ai_message(message, user)
    assert 'Я ИИ-помощник' not in message.answer.call_args.args[0]
    assert api.answer.call_args.args[1] == [api.ChatTurn('user', 'Привет')]


async def test_redis_failure_uses_empty_history(monkeypatch, message, user):
    monkeypatch.setattr(chat.cache, 'get', AsyncMock(side_effect=RuntimeError('offline')))
    monkeypatch.setattr(chat.cache, 'set', AsyncMock(side_effect=RuntimeError('offline')))
    await chat.handle_ai_message(message, user)
    assert api.answer.call_args.args[1] == []
    assert 'Настройки' in message.answer.call_args.args[0]
    assert await chat._request_turns(123, user.id)


@pytest.mark.parametrize(
    'outcome',
    [
        api.AiOutcome.RATE_LIMITED,
        api.AiOutcome.BUDGET_EXCEEDED,
        api.AiOutcome.ERROR,
        api.AiOutcome.FILTERED,
        api.AiOutcome.DISABLED,
    ],
)
async def test_unavailable_outcomes_offer_support(monkeypatch, message, user, outcome):
    monkeypatch.setattr(api, 'answer', AsyncMock(return_value=api.AiAnswer(outcome)))
    await chat.handle_ai_message(message, user)
    assert 'Сейчас не могу' in message.answer.call_args.args[0]
    assert message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data == 'menu_support'


async def test_model_exception_degrades_safely(monkeypatch, message, user):
    monkeypatch.setattr(api, 'answer', AsyncMock(side_effect=RuntimeError('offline')))
    await chat.handle_ai_message(message, user)
    assert 'Сейчас не могу' in message.answer.call_args.args[0]


def test_cabinet_mode_links_to_existing_support(monkeypatch):
    monkeypatch.setattr(chat.settings, 'MAIN_MENU_MODE', 'cabinet')
    monkeypatch.setattr(chat.settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example')
    assert chat.support_keyboard('ru').inline_keyboard[0][0].web_app.url == 'https://cabinet.example/support'


async def test_handoff_outcome_calls_ticket_transfer(monkeypatch, message, user):
    monkeypatch.setattr(
        api, 'answer', AsyncMock(return_value=api.AiAnswer(api.AiOutcome.HANDOFF, 'Зову человека', request_id=123))
    )
    transfer = AsyncMock()
    monkeypatch.setattr(chat, 'handoff_to_operator', transfer)
    await chat.handle_ai_message(message, user)
    assert transfer.call_args.args[2] == 123
    assert transfer.call_args.args[3][-2:] == [
        api.ChatTurn('user', message.text),
        api.ChatTurn('assistant', 'Зову человека'),
    ]


class HandoffDb:
    def __init__(self):
        self.ticket = None
        self.lock = asyncio.Lock()
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.lock.locked():
            self.lock.release()

    async def execute(self, query, params):
        self.statements.append(str(query))
        await self.lock.acquire()

    async def scalar(self, query):
        self.statements.append(str(query))
        return self.ticket


@pytest.mark.parametrize('blocked,enabled', [(False, True), (True, True), (False, False)])
async def test_handoff_one_ticket_or_contact(monkeypatch, message, user, blocked, enabled):
    db = HandoffDb()
    monkeypatch.setattr(chat, 'AsyncSessionLocal', lambda: db)
    monkeypatch.setattr(chat.SupportSettingsService, 'is_tickets_enabled', lambda: enabled)
    monkeypatch.setattr(chat.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=blocked))
    monkeypatch.setattr(chat.settings, 'SUPPORT_USERNAME', '@helpdesk')
    notifications = AsyncMock()
    monkeypatch.setattr(chat, 'notify_admins_about_new_ticket', notifications)

    async def create(*args):
        db.ticket = SimpleNamespace(id=88)
        return db.ticket

    create_ticket = AsyncMock(side_effect=create)
    monkeypatch.setattr(chat.TicketCRUD, 'create_ticket', create_ticket)
    turns = [api.ChatTurn('user', 'Не помогло'), api.ChatTurn('assistant', 'Приглашаю оператора')]
    await asyncio.gather(*(chat.handoff_to_operator(message, user, 123, turns) for _ in range(2)))
    if blocked or not enabled:
        create_ticket.assert_not_awaited()
        assert message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url == 'https://t.me/helpdesk'
    else:
        create_ticket.assert_awaited_once()
        notifications.assert_awaited_once()
        assert 'Клиент: Не помогло' in create_ticket.call_args.args[3]
        assert 'ИИ: Приглашаю оператора' in create_ticket.call_args.args[3]
        assert 'Обращение из чата ИИ №123' in create_ticket.call_args.args[3]
        assert '№88' in message.answer.call_args.args[0]
        assert 'pg_advisory_xact_lock' in db.statements[0]


async def test_operator_callback_checks_owner(monkeypatch, message, user):
    monkeypatch.setattr(
        api, 'get_request', AsyncMock(return_value=SimpleNamespace(user_id=99, channel=api.AiChannel.BOT_CHAT))
    )
    callback = SimpleNamespace(data='aio:123', message=message, answer=AsyncMock())
    transfer = AsyncMock()
    monkeypatch.setattr(chat, 'handoff_to_operator', transfer)
    await chat.handle_operator(callback, user)
    transfer.assert_not_awaited()
    assert callback.answer.call_args.kwargs['show_alert'] is True


@pytest.mark.parametrize('owner', [True, False])
async def test_feedback_ownership_and_operator_offer(monkeypatch, message, user, owner):
    monkeypatch.setattr(api, 'record_feedback', AsyncMock(return_value=owner))
    message.edit_reply_markup = AsyncMock()
    callback = SimpleNamespace(data='aif:123:0', message=message, answer=AsyncMock())
    await chat.handle_feedback(callback, user)
    api.record_feedback.assert_awaited_once_with(123, user.id, False)
    if owner:
        keyboard = message.edit_reply_markup.call_args.kwargs['reply_markup']
        assert len(keyboard.inline_keyboard) == 1
        assert keyboard.inline_keyboard[0][0].callback_data == 'aio:123'
    else:
        message.edit_reply_markup.assert_not_awaited()


async def test_admin_status_masks_secret_and_shows_stats(monkeypatch, user):
    monkeypatch.setattr(type(admin.settings), 'is_admin', lambda self, telegram_id: True)
    monkeypatch.setattr(api, 'describe_settings', lambda: {'api_key': 'never-display-this-value', 'enabled': True})
    monkeypatch.setattr(api, 'get_stats', AsyncMock(side_effect=lambda days: api.AiStats(days=days, requests=5)))
    monkeypatch.setattr(api, 'kb_status', AsyncMock(return_value=api.KbStatus(2, 3, 400, 100, 'abcdef', None)))
    event = types.CallbackQuery(
        id='1',
        from_user=types.User(id=777, first_name='Админ', is_bot=False),
        chat_instance='1',
        data='admin_ai_support',
    )
    # Декораторы проверяют настоящий тип события; тело экрана работает с доступным сообщением.
    message = SimpleNamespace(edit_text=AsyncMock())
    object.__setattr__(event, 'message', message)
    state = SimpleNamespace(clear=AsyncMock())
    await admin.show_ai_support(event, user, state)
    body = message.edit_text.call_args.args[0]
    assert 'never-display-this-value' not in body
    assert 'api_key: задан' in body
    assert api.get_stats.call_count == 2
    assert 'страниц 2' in body
