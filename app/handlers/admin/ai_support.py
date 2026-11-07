"""Экран ИИ-поддержки и подтверждение оператором черновиков."""

import asyncio
import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.ai_support import api
from app.services.support_settings_service import SupportSettingsService
from app.services.ticket_reply_service import publish_support_reply
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)
_draft_locks = [asyncio.Lock() for _ in range(64)]


class AiSupportStates(StatesGroup):
    question = State()


def _keyboard(language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('AI_SUPPORT_REFRESH', '🔄 Обновить базу'), callback_data='ais:refresh'
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('AI_SUPPORT_TEST', '🧪 Проверить ответ'), callback_data='ais:test'
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('AI_SUPPORT_SETTINGS', '⚙️ Настройки'), callback_data='botcfg_cat:other:AI_SUPPORT:1:1'
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')],
        ]
    )


@admin_required
@error_handler
async def show_ai_support(callback: types.CallbackQuery, db_user: User, state: FSMContext) -> None:
    await state.clear()
    texts = get_texts(db_user.language)
    try:
        config = api.describe_settings()
        # Ключ не показываем даже при ошибке реализации describe_settings.
        lines = []
        for key, value in config.items():
            if any(part in key.lower() for part in ('key', 'token', 'secret', 'password')):
                value = texts.t('AI_SUPPORT_KEY_SET', 'задан') if value else texts.t('AI_SUPPORT_KEY_MISSING', 'нет')
            lines.append(f'{key}: {value}')
        for days in (1, 7):
            stats = await api.get_stats(days)
            lines.append(
                texts.t(
                    'AI_SUPPORT_STATS',
                    '{days} дн.: запросов {requests}, ответов {answered}, передач {handoffs}, ошибок {errors}, '
                    'блокировок {blocked}, стоимость ${cost}',
                ).format(
                    days=days,
                    requests=stats.requests,
                    answered=stats.answered,
                    handoffs=stats.handoffs,
                    errors=stats.errors,
                    blocked=stats.blocked,
                    cost=f'{stats.cost_usd:.4f}',
                )
            )
        kb = await api.kb_status()
        lines.append(
            texts.t(
                'AI_SUPPORT_KB', 'База: страниц {pages}, статей {items}, символов {chars}, версия {version}'
            ).format(pages=kb.pages, items=kb.items, chars=kb.chars, version=kb.version)
        )
        body = '\n'.join(lines)
    except Exception:
        logger.exception('Не удалось прочитать статус ИИ')
        body = texts.t('AI_SUPPORT_ADMIN_UNAVAILABLE', 'Статус ИИ временно недоступен.')
    await callback.message.edit_text(
        f'<b>{html.escape(texts.t("AI_SUPPORT_TITLE", "🤖 ИИ-помощник"))}</b>\n\n{html.escape(body[:3500])}',
        parse_mode='HTML',
        reply_markup=_keyboard(db_user.language),
    )
    await callback.answer()


@admin_required
@error_handler
async def refresh_kb(callback: types.CallbackQuery, db_user: User, state: FSMContext) -> None:
    api.invalidate_kb_cache()
    await show_ai_support(callback, db_user, state)


@admin_required
@error_handler
async def start_test(callback: types.CallbackQuery, db_user: User, state: FSMContext) -> None:
    texts = get_texts(db_user.language)
    await state.set_state(AiSupportStates.question)
    await callback.message.answer(
        texts.t('AI_SUPPORT_TEST_PROMPT', 'Пришлите вопрос для проверки ИИ.'), reply_markup=_keyboard(db_user.language)
    )
    await callback.answer()


@admin_required
@error_handler
async def test_question(message: types.Message, db_user: User, state: FSMContext) -> None:
    await state.clear()
    result = await api.answer(
        message.text,
        [],
        api.AiRequestContext(api.AiChannel.ADMIN_TEST, None, language=db_user.language, allow_account_tools=False),
    )
    body = '\n\n'.join(
        str(part)
        for part in (
            result.outcome,
            result.reply_text,
            result.handoff_reason,
            result.operator_summary,
            result.error_code,
        )
        if part
    )
    await message.answer(html.escape(body[:3500]), parse_mode='HTML', reply_markup=_keyboard(db_user.language))


async def send_draft(callback: types.CallbackQuery, db_user: User) -> None:
    """Права совпадают с ручным ответом: администратор или активный модератор."""
    texts = get_texts(db_user.language)
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    try:
        request_id = int(callback.data.split(':')[1])
        async with _draft_locks[request_id % len(_draft_locks)]:
            record = await api.get_request(request_id)
            if (
                record is None
                or record.channel != api.AiChannel.TICKET
                or record.ticket_id is None
                or record.trigger_message_id is None
                or record.outcome not in {api.AiOutcome.ANSWERED, api.AiOutcome.HANDOFF}
                or not record.answer_text
                or record.reply_message_id is not None
            ):
                await callback.answer(
                    texts.t('AI_SUPPORT_DRAFT_EXPIRED', 'Черновик уже отправлен или недоступен.'), show_alert=True
                )
                return
            published = await publish_support_reply(
                ticket_id=record.ticket_id,
                author_user_id=db_user.id,
                text=record.answer_text,
                mark_answered=True,
                origin='human',
                trigger_message_id=record.trigger_message_id,
            )
            if published is None:
                await callback.answer(
                    texts.t('AI_SUPPORT_DRAFT_STALE', 'Тикет закрыт или появились новые сообщения.'), show_alert=True
                )
                return
            await api.attach_reply_message(request_id, published)
        await callback.answer(texts.t('AI_SUPPORT_DRAFT_SENT', 'Ответ отправлен клиенту.'))
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception('Не удалось отправить черновик ИИ')
        await callback.answer(
            texts.t('AI_SUPPORT_ADMIN_UNAVAILABLE', 'Статус ИИ временно недоступен.'), show_alert=True
        )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_ai_support, F.data == 'admin_ai_support')
    dp.callback_query.register(refresh_kb, F.data == 'ais:refresh')
    dp.callback_query.register(start_test, F.data == 'ais:test')
    dp.callback_query.register(send_draft, F.data.startswith('aisd:'))
    dp.message.register(test_question, AiSupportStates.question, F.text)
