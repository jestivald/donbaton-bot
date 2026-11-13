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


ORIGINAL_HANDLE = chat.handle_ai_message


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
    # Генерация идёт в фоновой задаче; в тестах дожидаемся её, чтобы проверять отправленные сообщения.
    original = chat.handle_ai_message

    async def handle_and_wait(message, db_user=None):
        await original(message, db_user)
        await asyncio.gather(*list(chat._inflight), return_exceptions=True)

    monkeypatch.setattr(chat, 'handle_ai_message', handle_and_wait)


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
    assert transfer.call_args.args[1:4] == (user.id, user.language, 123)
    assert transfer.call_args.kwargs['explanation'] == 'Зову человека'
    assert transfer.call_args.args[4][-2:] == [
        api.ChatTurn('user', message.text),
        api.ChatTurn('assistant', 'Зову человека'),
    ]


class HandoffDb:
    def __init__(self):
        self.ticket = None
        self.active = None
        self.last = None
        self.lock = asyncio.Lock()
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.lock.locked():
            self.lock.release()

    async def execute(self, query, params=None):
        self.statements.append(str(query))
        if 'pg_advisory' in str(query):
            await self.lock.acquire()

    async def commit(self):
        pass

    async def scalar(self, query):
        sql = str(query)
        self.statements.append(sql)
        if sql.startswith('SELECT ticket_messages.ticket_id'):  # поиск по маркеру запроса
            return self.ticket.id if self.ticket is not None else None
        if 'count(' in sql:
            return 0
        if sql.startswith('SELECT ai_support_requests.ticket_id'):
            return None
        if 'tickets.status IN' in sql:  # активный тикет клиента
            return self.active
        if 'ORDER BY ticket_messages.id DESC' in sql:  # последнее сообщение активного тикета
            return self.last
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
    await asyncio.gather(*(chat.handoff_to_operator(message, user.id, user.language, 123, turns) for _ in range(2)))
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


async def test_handler_returns_before_model_finishes(monkeypatch, message, user):
    """Хендлер не ждёт модель: транзакция AuthMiddleware завершается, соединение БД свободно."""
    release = asyncio.Event()

    async def slow_answer(*args):
        await release.wait()
        return api.AiAnswer(api.AiOutcome.ANSWERED, 'Готово', request_id=5)

    monkeypatch.setattr(api, 'answer', slow_answer)
    await asyncio.wait_for(ORIGINAL_HANDLE(message, user), timeout=1)
    assert chat._inflight, 'ответ должен готовиться в фоне'
    message.answer.assert_not_awaited()
    release.set()
    await asyncio.gather(*list(chat._inflight))
    assert 'Готово' in message.answer.call_args.args[0]


async def test_inflight_limit_degrades_to_support(monkeypatch, message, user):
    blockers = [asyncio.get_running_loop().create_future() for _ in range(chat.MAX_INFLIGHT)]
    tasks = {asyncio.ensure_future(blocker) for blocker in blockers}
    monkeypatch.setattr(chat, '_inflight', tasks)
    await asyncio.wait_for(ORIGINAL_HANDLE(message, user), timeout=1)
    api.answer.assert_not_awaited()
    assert 'Сейчас не могу' in message.answer.call_args.args[0]
    for blocker in blockers:
        blocker.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_handoff_appends_to_active_ticket_once(monkeypatch, message, user):
    db = HandoffDb()
    db.active = SimpleNamespace(id=55, user_id=user.id, is_user_reply_blocked=False)
    monkeypatch.setattr(chat, 'AsyncSessionLocal', lambda: db)
    monkeypatch.setattr(chat.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(chat.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=None))
    create_ticket = AsyncMock()
    monkeypatch.setattr(chat.TicketCRUD, 'create_ticket', create_ticket)
    added = []

    async def add_message(db_, ticket_id, user_id, text, is_from_admin=False):
        added.append((ticket_id, text))
        db.last = SimpleNamespace(is_from_admin=False, message_text=text, created_at=datetime.now(UTC))

    monkeypatch.setattr(chat.TicketMessageCRUD, 'add_message', add_message)
    reply_notify = AsyncMock()
    monkeypatch.setattr(chat, 'notify_admins_about_ticket_reply', reply_notify)
    turns = [api.ChatTurn('user', 'оператор')]
    await chat.handoff_to_operator(message, user.id, user.language, 201, turns)
    await chat.handoff_to_operator(message, user.id, user.language, 202, turns)
    create_ticket.assert_not_awaited()
    assert [ticket_id for ticket_id, _ in added] == [55], 'повтор в течение 10 минут не дописывает второй раз'
    assert added[0][1].startswith('Обращение из чата ИИ №201')
    reply_notify.assert_awaited_once()
    assert '№55' in message.answer.call_args.args[0]


@pytest.mark.parametrize(('chat_enabled', 'vision_enabled'), [(False, True), (True, False), (False, False)])
async def test_media_disabled_preserves_original_response(monkeypatch, message, user, chat_enabled, vision_enabled):
    message.text = None
    message.caption = 'Не работает'
    monkeypatch.setattr(api, 'bot_chat_enabled', lambda: chat_enabled)
    monkeypatch.setattr(api, 'vision_enabled', lambda: vision_enabled)
    await chat.handle_ai_media(message, user)
    actual = message.answer.call_args
    message.answer.reset_mock()
    await chat.handle_unknown_message(message, user)
    assert message.answer.call_args == actual
    api.answer.assert_not_awaited()


@pytest.mark.parametrize('caption', ['Ошибка в Happ', None])
@pytest.mark.parametrize('kind', ['photo', 'document'])
async def test_media_answer_buttons_and_text_only_memory(monkeypatch, message, user, caption, kind):
    message.text = None
    message.caption = caption
    message.photo = [SimpleNamespace(file_id='photo', file_size=50, width=10, height=10)] if kind == 'photo' else []
    message.document = (
        SimpleNamespace(file_id='document', file_size=50, mime_type='image/png') if kind == 'document' else None
    )
    monkeypatch.setattr(api, 'vision_enabled', lambda: True)
    image = api.AiImage(b'private screenshot', 'image/png')
    fetch = AsyncMock(return_value=[image])
    monkeypatch.setattr(chat, 'fetch_telegram_images', fetch)
    await chat.handle_ai_media(message, user)
    fetch.assert_awaited_once_with(message.bot, [kind])
    assert api.answer.call_args.args[0] == (caption or '')
    assert api.answer.call_args.kwargs['images'] == [image]
    memory = await chat._request_turns(123, user.id)
    assert memory[0] == api.ChatTurn('user', caption or '')
    assert 'private screenshot' not in str(chat.cache.set.await_args_list)
    assert 'private screenshot' not in str(chat._request_memory)
    buttons = message.answer.call_args.kwargs['reply_markup'].inline_keyboard
    assert buttons[-1][0].callback_data == 'aio:123'


async def test_media_download_error_still_answers_caption(monkeypatch, message, user):
    message.text = None
    message.caption = 'Ошибка в Happ'
    message.photo = [SimpleNamespace(file_id='bad', file_size=50, width=10, height=10)]
    message.bot.get_file = AsyncMock(side_effect=RuntimeError('недоступен'))
    monkeypatch.setattr(api, 'vision_enabled', lambda: True)
    monkeypatch.setattr(chat.settings, 'AI_SUPPORT_VISION_ENABLED', True)
    await chat.handle_ai_media(message, user)
    assert api.answer.call_args.args[0] == 'Ошибка в Happ\n\n(клиент приложил изображение, его не удалось посмотреть)'
    assert 'Настройки' in message.answer.call_args.args[0]
    assert (await chat._request_turns(123, user.id))[0] == api.ChatTurn('user', 'Ошибка в Happ')


@pytest.mark.parametrize(
    ('kind', 'chat_type', 'bot', 'state', 'expected'),
    [
        ('photo', 'private', False, None, True),
        ('image', 'private', False, None, True),
        ('document', 'private', False, None, False),
        ('video', 'private', False, None, False),
        ('photo', 'group', False, None, False),
        ('photo', 'private', True, None, False),
        ('photo', 'private', False, 'TicketState', False),
    ],
)
async def test_media_routing_filters(kind, chat_type, bot, state, expected):
    dp = Dispatcher()
    chat.register_handlers(dp)
    media = {}
    if kind == 'photo':
        media['photo'] = [types.PhotoSize(file_id='photo', file_unique_id='unique', width=100, height=100)]
    elif kind in {'image', 'document'}:
        media['document'] = types.Document(
            file_id='doc', file_unique_id='unique', mime_type='image/png' if kind == 'image' else 'application/pdf'
        )
    event = types.Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=types.Chat(id=77, type=chat_type),
        from_user=types.User(id=77, is_bot=bot, first_name='Клиент'),
        caption='Подпись',
        **media,
    )
    handler = dp.message.handlers[1]
    accepted, _ = await handler.check(event, raw_state=state)
    assert accepted is expected


async def test_media_download_runs_in_bounded_background_task(monkeypatch, message, user):
    message.text = None
    message.caption = ''
    message.photo = [SimpleNamespace(file_id='photo', file_size=50, width=10, height=10)]
    monkeypatch.setattr(api, 'vision_enabled', lambda: True)
    monkeypatch.setattr(chat, 'handle_ai_message', ORIGINAL_HANDLE)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(*args):
        entered.set()
        await release.wait()
        return [api.AiImage(b'png', 'image/png')]

    downloader = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(chat, 'fetch_telegram_images', downloader)
    await asyncio.wait_for(chat.handle_ai_media(message, user), 0.2)
    assert chat._inflight
    await asyncio.wait_for(entered.wait(), 1)
    api.answer.assert_not_awaited()
    # Загрузка удерживает тот же лимит, что и ожидание модели.
    monkeypatch.setattr(chat, 'MAX_INFLIGHT', 1)
    await chat.handle_ai_media(message, user)
    assert 'Ещё отвечаю' in message.answer.call_args.args[0]
    assert downloader.await_count == 1
    release.set()
    await asyncio.gather(*list(chat._inflight))
    api.answer.assert_awaited_once()


async def test_h1_one_pending_chat_task_reserved_before_scheduling(monkeypatch, message, user):
    release = asyncio.Event()

    async def slow(*args):
        await release.wait()

    model = AsyncMock(side_effect=slow)
    monkeypatch.setattr(chat, '_process_chat', model)
    try:
        for _ in range(12):
            await ORIGINAL_HANDLE(message, user)
        assert len(chat._inflight) == 1
        assert message.answer.await_count == 11
        assert 'Ещё отвечаю' in message.answer.call_args.args[0]
        await asyncio.sleep(0)
        model.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(*list(chat._inflight))
    await ORIGINAL_HANDLE(message, user)
    await asyncio.gather(*list(chat._inflight))
    assert model.await_count == 2


async def test_h1_blocked_active_ticket_cannot_be_bypassed(monkeypatch, message, user):
    db = HandoffDb()
    db.active = SimpleNamespace(id=55, user_id=user.id, is_user_reply_blocked=True)
    monkeypatch.setattr(chat, 'AsyncSessionLocal', lambda: db)
    monkeypatch.setattr(chat.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(chat.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=False))
    add, create, notify = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(chat.TicketMessageCRUD, 'add_message', add)
    monkeypatch.setattr(chat.TicketCRUD, 'create_ticket', create)
    monkeypatch.setattr(chat, 'notify_admins_about_ticket_reply', notify)
    await chat.handoff_to_operator(message, user.id, 'ru', 123, [api.ChatTurn('user', 'оператор')])
    add.assert_not_awaited()
    create.assert_not_awaited()
    notify.assert_not_awaited()
    assert 'FOR UPDATE' in next(sql for sql in db.statements if 'tickets.status IN' in sql)
    assert 'Свяжитесь' in message.answer.call_args.args[0]


async def test_h1_chat_handoff_daily_limit(monkeypatch, message, user):
    class QuotaDb(HandoffDb):
        async def scalar(self, query):
            if 'count(' in str(query):
                return 3
            return await super().scalar(query)

    db = QuotaDb()
    monkeypatch.setattr(chat, 'AsyncSessionLocal', lambda: db)
    monkeypatch.setattr(chat.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(chat.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=False))
    create, notify = AsyncMock(), AsyncMock()
    monkeypatch.setattr(chat.TicketCRUD, 'create_ticket', create)
    monkeypatch.setattr(chat, 'notify_admins_about_new_ticket', notify)
    await chat.handoff_to_operator(message, user.id, 'ru', 123, [api.ChatTurn('user', 'оператор')])
    create.assert_not_awaited()
    notify.assert_not_awaited()
    assert message.answer.call_args.args[0] == 'Ваш вопрос уже у оператора, ответ придёт сюда'


@pytest.mark.parametrize(
    'text', ['🏠 Главное меню', '💳 Купить подписку', '👥 Пригласи друга', '❓ Помощь', ' 🏠 Главное меню ']
)
async def test_legacy_reply_buttons_remove_keyboard_without_ai(message, user, text):
    message.text = text
    await chat.handle_ai_message(message, user)
    api.answer.assert_not_awaited()
    assert message.answer.await_count == 1
    sent = message.answer.call_args
    assert isinstance(sent.kwargs['reply_markup'], types.ReplyKeyboardRemove)
    assert '/start' in sent.args[0]


async def test_legacy_reply_buttons_handled_when_ai_disabled(monkeypatch, message, user):
    monkeypatch.setattr(api, 'bot_chat_enabled', lambda: False)
    message.text = '💳 Купить подписку'
    await chat.handle_ai_message(message, user)
    assert isinstance(message.answer.call_args.kwargs['reply_markup'], types.ReplyKeyboardRemove)


async def test_text_mentioning_menu_still_goes_to_ai(message, user):
    message.text = 'Где главное меню? 🏠 Главное меню не открывается'
    await chat.handle_ai_message(message, user)
    api.answer.assert_awaited()


async def test_handoff_confirmation_is_remembered_in_chat_history(monkeypatch, message, user):
    """13.11.2026 #39: кнопка «Оператор» создала тикет, а на «когда придёт?» ИИ ответил «я пока не передавал»."""
    db = HandoffDb()
    monkeypatch.setattr(chat, 'AsyncSessionLocal', lambda: db)
    monkeypatch.setattr(chat.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(chat.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=None))
    monkeypatch.setattr(chat, 'notify_admins_about_new_ticket', AsyncMock())

    async def create(*args):
        db.ticket = SimpleNamespace(id=39)
        return db.ticket

    monkeypatch.setattr(chat.TicketCRUD, 'create_ticket', AsyncMock(side_effect=create))
    history = [{'role': 'user', 'text': 'Ничего не работает'}, {'role': 'assistant', 'text': 'Что именно?'}]
    monkeypatch.setattr(chat.cache, 'get', AsyncMock(return_value=history))
    await chat.handoff_to_operator(message, user.id, user.language, 458, [api.ChatTurn('user', 'Ничего не работает')])
    key, saved, ttl = chat.cache.set.call_args.args
    assert key == 'ai_support:chat:7' and ttl == 300
    assert saved[:2] == history
    assert saved[-1]['role'] == 'assistant' and '№39' in saved[-1]['text']
    # история, которую увидит модель на следующий вопрос, содержит подтверждение передачи
    monkeypatch.setattr(chat.cache, 'get', AsyncMock(return_value=saved))
    turns = await chat._read_history(user.id)
    assert '№39' in turns[-1].text


async def test_handoff_memory_failure_does_not_break_transfer(monkeypatch, message, user):
    monkeypatch.setattr(chat, '_get_or_create_handoff', AsyncMock(return_value=39))
    monkeypatch.setattr(chat.cache, 'get', AsyncMock(side_effect=RuntimeError('offline')))
    await chat.handoff_to_operator(message, user.id, user.language, 458, [])
    api.mark_handoff_done.assert_awaited_once_with(458, 39)
    assert '№39' in message.answer.call_args.args[0]
