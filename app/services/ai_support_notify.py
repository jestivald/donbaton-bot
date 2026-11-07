"""Уведомления ИИ-поддержки в существующий чат/топик тикетов."""

import html

import structlog
from aiogram import types

from app.services.admin_notification_service import AdminNotificationService
from app.services.ai_support import api
from app.services.maintenance_service import maintenance_service


logger = structlog.get_logger(__name__)


def ticket_keyboard(ticket_id: int, request_id: int | None = None) -> types.InlineKeyboardMarkup:
    rows = []
    if request_id is not None:
        rows.append([types.InlineKeyboardButton(text='✅ Отправить клиенту', callback_data=f'aisd:{request_id}')])
    rows.append([types.InlineKeyboardButton(text='🎫 Открыть тикет', callback_data=f'admin_view_ticket_{ticket_id}')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def send_notice(
    title: str,
    body: str,
    *,
    ticket_id: int | None = None,
    request_id: int | None = None,
    respect_ai_toggle: bool = True,
) -> bool:
    """Все внешние вставки экранируются после обрезки, включая тексты тревог."""
    try:
        if maintenance_service._bot is None or (respect_ai_toggle and not api.notify_admins_enabled()):
            return False
        text = f'<b>{html.escape(title[:180])}</b>\n\n{html.escape(body[:2900])}'
        keyboard = ticket_keyboard(ticket_id, request_id) if ticket_id is not None else None
        return await AdminNotificationService(maintenance_service._bot).send_ticket_event_notification(text, keyboard)
    except Exception:
        logger.exception('Не удалось отправить уведомление ИИ-поддержки')
        return False


async def admin_alert(kind: str, text: str) -> None:
    # Выключатель копий автоответов не скрывает служебные тревоги ядра.
    await send_notice(f'⚠️ ИИ-помощник: {kind}', text, respect_ai_toggle=False)


async def notify_ticket_answer(ticket_id: int, result: api.AiAnswer, *, draft: bool = False) -> bool:
    if draft:
        title = f'📝 Черновик ИИ для тикета #{ticket_id}'
    elif result.outcome == api.AiOutcome.HANDOFF:
        title = f'🙋 Нужен оператор — тикет #{ticket_id}'
    else:
        title = f'🤖 Ответ ИИ в тикете #{ticket_id}'
    parts = [result.reply_text or '']
    if result.handoff_reason:
        parts.append(f'ИИ советует ответить человеку: {result.handoff_reason}')
    if result.operator_summary:
        parts.append(f'Резюме: {result.operator_summary}')
    return await send_notice(
        title,
        '\n\n'.join(parts),
        ticket_id=ticket_id,
        request_id=result.request_id if draft else None,
        respect_ai_toggle=not draft,
    )
