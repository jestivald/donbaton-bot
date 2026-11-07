"""ИИ для свободного текста в личке; остальные обработчики имеют приоритет."""

import asyncio
import contextlib
import html
from collections import OrderedDict
from time import monotonic

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from sqlalchemy import select, text as sql_text

from app.config import settings
from app.database.crud.ticket import TicketCRUD
from app.database.database import AsyncSessionLocal
from app.database.models import Ticket, TicketMessage, User
from app.handlers.common import handle_unknown_message
from app.handlers.tickets import notify_admins_about_new_ticket
from app.localization.texts import get_texts
from app.services.ai_support import api
from app.services.support_settings_service import SupportSettingsService
from app.utils.cache import cache


logger = structlog.get_logger(__name__)
_request_memory: OrderedDict[int, tuple[float, dict]] = OrderedDict()
_chat_locks = [asyncio.Lock() for _ in range(64)]


def support_keyboard(language: str, *, contact_only: bool = False) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    label = texts.t('AI_SUPPORT_CONTACT', '👤 Поддержка')
    contact = settings.get_support_contact_url()
    if contact_only and contact:
        button = types.InlineKeyboardButton(text=label, url=contact)
    else:
        from app.utils.miniapp_buttons import build_cabinet_url

        url = build_cabinet_url('/support') if settings.MAIN_MENU_MODE == 'cabinet' else None
        button = (
            types.InlineKeyboardButton(text=label, web_app=types.WebAppInfo(url=url))
            if url
            else types.InlineKeyboardButton(text=label, callback_data='menu_support')
        )
    return types.InlineKeyboardMarkup(inline_keyboard=[[button]])


def answer_keyboard(request_id: int, language: str, *, feedback: bool = True) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = []
    if feedback:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('AI_SUPPORT_HELPFUL', '👍 Помогло'), callback_data=f'aif:{request_id}:1'
                ),
                types.InlineKeyboardButton(
                    text=texts.t('AI_SUPPORT_NOT_HELPFUL', '👎 Не помогло'), callback_data=f'aif:{request_id}:0'
                ),
            ]
        )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('AI_SUPPORT_OPERATOR', '👤 Оператор'), callback_data=f'aio:{request_id}'
            )
        ]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _typing(message: types.Message) -> None:
    while True:
        with contextlib.suppress(Exception):
            await message.bot.send_chat_action(chat_id=message.chat.id, action='typing')
        await asyncio.sleep(4)


async def _read_history(user_id: int) -> list[api.ChatTurn]:
    try:
        raw = await cache.get(f'ai_support:chat:{user_id}')
        turns = [
            api.ChatTurn(item['role'], item['text']) for item in raw or [] if item['role'] in {'user', 'assistant'}
        ]
        limit = api.history_turns()
        return turns[-limit:] if limit > 0 else []
    except Exception:
        return []


async def _remember(request_id: int, user_id: int, turns: list[api.ChatTurn]) -> None:
    ttl = api.chat_memory_ttl_seconds()
    data = {'user_id': user_id, 'turns': [{'role': turn.role, 'text': turn.text} for turn in turns]}
    now = monotonic()
    while _request_memory and (len(_request_memory) >= 1000 or next(iter(_request_memory.values()))[0] <= now):
        _request_memory.popitem(last=False)
    _request_memory[request_id] = (now + ttl, data)
    try:
        await cache.set(f'ai_support:request:{request_id}', data, ttl)
        limit = api.history_turns()
        await cache.set(f'ai_support:chat:{user_id}', data['turns'][-limit:] if limit > 0 else [], ttl)
    except Exception:
        logger.debug('Память чата ИИ недоступна')


async def _request_turns(request_id: int, user_id: int) -> list[api.ChatTurn]:
    try:
        data = await cache.get(f'ai_support:request:{request_id}')
    except Exception:
        data = None
    if not data:
        expires, data = _request_memory.get(request_id, (0, None))
        if expires <= monotonic():
            data = None
    if not data or data.get('user_id') != user_id:
        return []
    return [api.ChatTurn(item['role'], item['text']) for item in data['turns']]


async def _unavailable(message: types.Message, language: str) -> None:
    texts = get_texts(language)
    await message.answer(
        texts.t('AI_SUPPORT_UNAVAILABLE', 'Сейчас не могу ответить автоматически. Напишите в поддержку.'),
        reply_markup=support_keyboard(language),
    )


async def handle_ai_message(message: types.Message, db_user: User | None = None) -> None:
    language = db_user.language if db_user else 'ru'
    try:
        enabled = api.bot_chat_enabled()
    except Exception:
        enabled = False
    if not enabled or db_user is None:
        await handle_unknown_message(message, db_user)
        return
    async with _chat_locks[db_user.id % len(_chat_locks)]:
        typing = asyncio.create_task(_typing(message))
        try:
            history = await _read_history(db_user.id)
            result = await api.answer(
                message.text, history, api.AiRequestContext(api.AiChannel.BOT_CHAT, db_user.id, language=language)
            )
        except Exception:
            logger.exception('Сбой ИИ в личном чате', user_id=db_user.id)
            await _unavailable(message, language)
            return
        finally:
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)
        turns = [*history, api.ChatTurn('user', message.text)]
        if result.reply_text and result.outcome in {api.AiOutcome.ANSWERED, api.AiOutcome.HANDOFF}:
            turns.append(api.ChatTurn('assistant', result.reply_text))
        if result.request_id is not None:
            await _remember(result.request_id, db_user.id, turns)
        if result.outcome == api.AiOutcome.HANDOFF and result.request_id is not None:
            await handoff_to_operator(message, db_user, result.request_id, turns, result.handoff_reason)
        elif result.outcome == api.AiOutcome.ANSWERED and result.reply_text:
            texts = get_texts(language)
            disclosure = texts.t(
                'AI_SUPPORT_DISCLOSURE',
                'Я ИИ-помощник ДонБатона. Оператора можно позвать в любой момент.',
            )
            text = (f'{disclosure}\n\n' if not history else '') + result.reply_text
            keyboard = answer_keyboard(result.request_id, language) if result.request_id is not None else None
            # Режем исходный текст, затем экранируем: сущности HTML не разрываются.
            for offset in range(0, len(text), 3500):
                await message.answer(
                    html.escape(text[offset : offset + 3500]),
                    parse_mode='HTML',
                    reply_markup=keyboard if offset + 3500 >= len(text) else None,
                )
        elif result.outcome != api.AiOutcome.DUPLICATE:
            await _unavailable(message, language)


async def _get_or_create_handoff(user_id: int, request_id: int, turns: list[api.ChatTurn], reason: str | None):
    """Маркер в первом сообщении делает создание устойчивым к сбою до mark_handoff_done."""
    marker = f'Обращение из чата ИИ №{request_id}\n'
    async with AsyncSessionLocal() as db:
        # CRUD делает commit; к этому моменту маркер уже сохранён и виден следующему вызову.
        await db.execute(sql_text('SELECT pg_advisory_xact_lock(714202, :request_id)'), {'request_id': request_id})
        existing = await db.scalar(
            select(Ticket)
            .join(TicketMessage, TicketMessage.ticket_id == Ticket.id)
            .where(Ticket.user_id == user_id, TicketMessage.message_text.startswith(marker, autoescape=True))
            .limit(1)
        )
        if existing is not None:
            return existing.id
        if not SupportSettingsService.is_tickets_enabled() or await TicketCRUD.is_user_globally_blocked(db, user_id):
            return None
        questions = [turn.text for turn in turns if turn.role == 'user']
        title = f'Вопрос из чата с ИИ: {questions[-1] if questions else "Нужен оператор"}'[:255]
        transcript = '\n\n'.join(f'{"Клиент" if turn.role == "user" else "ИИ"}: {turn.text}' for turn in turns)
        body = f'{marker}\n{transcript}\n\nПричина передачи: {reason or api.HandoffReason.CLIENT_REQUEST}'
        ticket = await TicketCRUD.create_ticket(db, user_id, title, body)
        await notify_admins_about_new_ticket(ticket, db)
        return ticket.id


async def handoff_to_operator(
    message: types.Message,
    db_user: User,
    request_id: int,
    turns: list[api.ChatTurn],
    reason: str | None = None,
) -> None:
    texts = get_texts(db_user.language)
    try:
        ticket_id = await _get_or_create_handoff(db_user.id, request_id, turns, reason)
        if ticket_id is None:
            await message.answer(
                texts.t('AI_SUPPORT_CONTACT_ONLY', 'Свяжитесь с оператором по кнопке поддержки.'),
                reply_markup=support_keyboard(db_user.language, contact_only=True),
            )
            return
        await api.mark_handoff_done(request_id, ticket_id)
        await message.answer(
            texts.t(
                'AI_SUPPORT_HANDOFF_DONE',
                'Передал оператору, обращение №{ticket_id}; ответ придёт сюда и в разделе «Поддержка».',
            ).format(ticket_id=ticket_id)
        )
    except Exception:
        logger.exception('Не удалось передать чат ИИ оператору', request_id=request_id)
        await _unavailable(message, db_user.language)


async def handle_feedback(callback: types.CallbackQuery, db_user: User) -> None:
    texts = get_texts(db_user.language)
    try:
        _, raw_id, value = callback.data.split(':')
        if value not in {'0', '1'} or not await api.record_feedback(int(raw_id), db_user.id, value == '1'):
            await callback.answer(texts.ACCESS_DENIED, show_alert=True)
            return
        await callback.answer(texts.t('AI_SUPPORT_FEEDBACK_SAVED', 'Спасибо за отзыв'))
        await callback.message.edit_reply_markup(
            reply_markup=answer_keyboard(int(raw_id), db_user.language, feedback=False)
        )
    except Exception:
        logger.exception('Не удалось записать отзыв ИИ')
        with contextlib.suppress(Exception):
            await callback.answer(texts.t('AI_SUPPORT_CALLBACK_EXPIRED', 'Откройте поддержку и повторите вопрос.'))


async def handle_operator(callback: types.CallbackQuery, db_user: User) -> None:
    texts = get_texts(db_user.language)
    try:
        request_id = int(callback.data.split(':')[1])
        record = await api.get_request(request_id)
        if record is None or record.user_id != db_user.id or record.channel != api.AiChannel.BOT_CHAT:
            await callback.answer(texts.ACCESS_DENIED, show_alert=True)
            return
        await callback.answer()
        turns = await _request_turns(request_id, db_user.id)
        if not turns:
            turns = [api.ChatTurn('assistant', record.answer_text or record.operator_summary or 'Нужен оператор')]
        await handoff_to_operator(callback.message, db_user, request_id, turns)
    except Exception:
        logger.exception('Сбой кнопки оператора ИИ')
        await _unavailable(callback.message, db_user.language)


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_feedback, F.data.startswith('aif:'))
    dp.callback_query.register(handle_operator, F.data.startswith('aio:'))
    dp.message.register(
        handle_ai_message,
        StateFilter(None),
        F.from_user.is_bot.is_(False),
        F.successful_payment.is_(None),
        F.text.is_not(None),
        ~F.text.startswith('/'),
        F.chat.type == 'private',
    )
