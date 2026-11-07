"""Автоматизация тикетов: короткие транзакции, проверка актуальности под блокировкой."""

import asyncio
import re
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import or_, select

from app.config import settings
from app.database.crud.ticket import TicketCRUD
from app.database.database import AsyncSessionLocal
from app.database.models import AiSupportRequest, SupportAuditLog, Ticket, TicketMessage
from app.localization.texts import get_texts
from app.services.event_emitter import event_emitter
from app.services.support_settings_service import SupportSettingsService
from app.services.ticket_reply_service import _ai_author_id, _deliver_reply
from app.utils.quiet_hours import is_quiet_now, quiet_end_label


logger = structlog.get_logger(__name__)
NOTICE_ACTIONS = ('ticket_client_reminder', 'ticket_autoclosed', 'ticket_closed_by_ai')


def night_handoff_reply(text: str, language: str) -> str:
    if settings.SUPPORT_NIGHT_AUTOREPLY_ENABLED and is_quiet_now():
        notice = get_texts(language).t('TICKET_NIGHT_AUTOREPLY').format(time=quiet_end_label())
        return f'{text}\n\n{notice}'
    return text


def explicit_close_request(text: str) -> bool:
    """Консервативная дополнительная защита: благодарности сами по себе недостаточно."""
    value = (text or '').lower()
    if re.search(r'\b(?:не|нельзя|not|don[’\']t|как|how|если|if)\b', value):
        return False
    return bool(
        re.search(r'\bзакр(?:ой(?:те)?|ывай(?:те)?)\b', value)
        or re.search(r'\b(?:можно|прошу|можете)\s+закрыть\b', value)
        or re.search(r'\bclose\s+(?:(?:the|my|this)\s+)?(?:ticket|case|request)\b', value)
    )


def asks_client_question(text: str) -> bool:
    # Подпись ИИ после вопроса не должна скрыть вопрос. Длинные цитаты в начале не учитываем.
    tail = re.split(r'[.!\n]+', (text or '').strip())
    return '?' in ' '.join(part for part in tail if part.strip())[-600:]


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _notice_ids(ticket_id: int):
    return select(SupportAuditLog.details['message_id'].as_integer()).where(
        SupportAuditLog.ticket_id == ticket_id,
        SupportAuditLog.action.in_(NOTICE_ACTIONS),
        SupportAuditLog.details['message_id'].as_integer().is_not(None),
    )


async def last_substantive_message(db, ticket_id: int):
    return await db.scalar(
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id, TicketMessage.id.not_in(_notice_ids(ticket_id)))
        .order_by(TicketMessage.id.desc())
        .limit(1)
    )


def _handoff_conditions():
    return or_(
        AiSupportRequest.outcome == 'handoff',
        AiSupportRequest.handoff_reason.is_not(None),
        AiSupportRequest.handoff_done_at.is_not(None),
    )


async def waiting_for_human(db, ticket_id: int) -> bool:
    """Ответ сотрудника снимает ожидание; ответ ИИ и системное напоминание — нет."""
    ai_ids = select(AiSupportRequest.reply_message_id).where(
        AiSupportRequest.ticket_id == ticket_id, AiSupportRequest.reply_message_id.is_not(None)
    )
    sent_drafts = select(SupportAuditLog.details['message_id'].as_integer()).where(
        SupportAuditLog.ticket_id == ticket_id,
        SupportAuditLog.action == 'ai_support_human_reply',
        SupportAuditLog.details['message_id'].as_integer().is_not(None),
    )
    human = await db.scalar(
        select(TicketMessage)
        .where(
            TicketMessage.ticket_id == ticket_id,
            TicketMessage.is_from_admin.is_(True),
            or_(TicketMessage.id.not_in(ai_ids), TicketMessage.id.in_(sent_drafts)),
            TicketMessage.id.not_in(_notice_ids(ticket_id)),
        )
        .order_by(TicketMessage.id.desc())
        .limit(1)
    )
    query = select(AiSupportRequest.id).where(AiSupportRequest.ticket_id == ticket_id, _handoff_conditions())
    if human is not None:
        query = query.where(AiSupportRequest.created_at >= human.created_at)
    return await db.scalar(query.limit(1)) is not None


async def _close_guard(db, ticket, ctx) -> bool:
    from app.services.ai_support import api

    if (
        not settings.AI_SUPPORT_CAN_CLOSE_TICKETS
        or ctx.channel != api.AiChannel.TICKET
        or api.ticket_mode() != 'auto'
        or ticket is None
        or ticket.id != ctx.ticket_id
        or ticket.user_id != ctx.user_id
        or ticket.status not in {'open', 'answered'}
        or ticket.is_user_reply_blocked
        or not SupportSettingsService.is_tickets_enabled()
        or await TicketCRUD.is_user_globally_blocked(db, ticket.user_id)
    ):
        return False
    if (
        await db.scalar(
            select(AiSupportRequest.id).where(AiSupportRequest.ticket_id == ticket.id, _handoff_conditions()).limit(1)
        )
        is not None
    ):
        return False
    last = await last_substantive_message(db, ticket.id)
    return bool(
        last is not None
        and last.id == ctx.trigger_message_id
        and last.user_id == ctx.user_id
        and not last.is_from_admin
        and explicit_close_request(last.message_text)
    )


async def request_ai_close(ctx) -> bool:
    """Инструмент резервирует намерение; только интеграция после финального ответа закрывает тикет."""
    async with AsyncSessionLocal() as db:
        ticket = await db.scalar(select(Ticket).where(Ticket.id == ctx.ticket_id).with_for_update())
        if not await _close_guard(db, ticket, ctx):
            return False
        db.add(
            SupportAuditLog(
                action='ai_ticket_close_requested',
                ticket_id=ticket.id,
                details={'trigger_message_id': ctx.trigger_message_id},
            )
        )
        await db.commit()
    return True


async def _store_notice(db, ticket, text: str, action: str, now: datetime):
    author = await _ai_author_id(db)
    if author is None:
        return None
    message = TicketMessage(ticket_id=ticket.id, user_id=author, message_text=text, is_from_admin=True, has_media=False)
    db.add(message)
    await db.flush()
    db.add(SupportAuditLog(action=action, ticket_id=ticket.id, details={'message_id': message.id}))
    ticket.updated_at = now
    return message


async def _publish_notice(ticket_id: int, message_id: int, author_id: int, text: str, status: str):
    try:
        async with AsyncSessionLocal() as db:
            await event_emitter.emit(
                'ticket.message_added',
                {
                    'ticket_id': ticket_id,
                    'message_id': message_id,
                    'user_id': author_id,
                    'is_from_admin': True,
                    'message_text': text[:200],
                    'has_media': False,
                    'status': status,
                },
                db=db,
            )
    except Exception:
        logger.exception('Не удалось отправить событие автоматизации', ticket_id=ticket_id)
    await _deliver_reply(ticket_id, text, message_id)


async def finish_ai_close(ctx) -> int | None:
    """Вызывать только после ANSWERED: HANDOFF/ошибка/черновик никогда не закрывают тикет."""
    async with AsyncSessionLocal() as db:
        ticket = await db.scalar(select(Ticket).where(Ticket.id == ctx.ticket_id).with_for_update())
        if not await _close_guard(db, ticket, ctx):
            return None
        requested = await db.scalar(
            select(SupportAuditLog.id)
            .where(
                SupportAuditLog.ticket_id == ticket.id,
                SupportAuditLog.action == 'ai_ticket_close_requested',
                SupportAuditLog.details['trigger_message_id'].as_integer() == ctx.trigger_message_id,
            )
            .limit(1)
        )
        if requested is None:
            return None
        text = get_texts(ctx.language).t('TICKET_CLOSED_BY_AI')
        message = await _store_notice(db, ticket, text, 'ticket_closed_by_ai', datetime.now(UTC))
        if message is None:
            return None
        await TicketCRUD.close_ticket(db, ticket.id)
        message_id, author_id = message.id, message.user_id
    await _publish_notice(ctx.ticket_id, message_id, author_id, text, 'closed')
    return message_id


class TicketAutomationService:
    def __init__(self):
        self._task: asyncio.Task | None = None

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='ticket-automation')

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self):
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception('Сбой цикла автоматизации тикетов')
            await asyncio.sleep(60)

    async def run_once(self, now: datetime | None = None):
        fixed_now = now
        now = aware(now or datetime.now(UTC))
        if (
            is_quiet_now(now)
            or not SupportSettingsService.is_tickets_enabled()
            or not (settings.SUPPORT_TICKET_AUTOCLOSE_HOURS or settings.SUPPORT_TICKET_CLIENT_REMINDER_HOURS)
        ):
            return
        # Keyset pagination: не держим транзакцию и весь набор тикетов в памяти между доставками.
        cursor = 0
        while True:
            async with AsyncSessionLocal() as db:
                ids = list(
                    (
                        await db.scalars(
                            select(Ticket.id)
                            .where(Ticket.status == 'answered', Ticket.id > cursor)
                            .order_by(Ticket.id)
                            .limit(100)
                        )
                    ).all()
                )
            if not ids:
                return
            for ticket_id in ids:
                try:
                    await self.process_ticket(ticket_id, aware(fixed_now or datetime.now(UTC)))
                except Exception:
                    logger.exception('Сбой автоматизации тикета', ticket_id=ticket_id)
            cursor = ids[-1]

    async def process_ticket(self, ticket_id: int, now: datetime):
        if is_quiet_now(now):
            return
        async with AsyncSessionLocal() as db:
            ticket = await db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
            if ticket is None or ticket.status != 'answered' or await waiting_for_human(db, ticket_id):
                return
            last = await last_substantive_message(db, ticket_id)
            if last is None or not last.is_from_admin:
                return
            age = aware(now) - aware(last.created_at)
            close_hours = settings.SUPPORT_TICKET_AUTOCLOSE_HOURS
            reminder_hours = settings.SUPPORT_TICKET_CLIENT_REMINDER_HOURS
            close = close_hours > 0 and age >= timedelta(hours=close_hours)
            reminder = (
                reminder_hours > 0
                and age >= timedelta(hours=reminder_hours)
                and ticket.client_reminder_sent_at is None
                and asks_client_question(last.message_text)
            )
            if not close and not reminder:
                return
            # Пользователя читаем явно: relationship нельзя лениво загружать в async-сессии.
            from app.database.models import User

            user = await db.get(User, ticket.user_id)
            if user is None:
                return
            key = 'TICKET_AUTOCLOSED_CLIENT' if close else 'TICKET_CLIENT_REMINDER'
            text = get_texts(user.language).t(key).format(title=ticket.title, hours=close_hours)
            action = 'ticket_autoclosed' if close else 'ticket_client_reminder'
            message = await _store_notice(db, ticket, text, action, now)
            if message is None:
                return
            message_id, author_id = message.id, message.user_id
            if close:
                ticket.autoclosed_at = now
                await TicketCRUD.close_ticket(db, ticket_id)
            else:
                ticket.client_reminder_sent_at = now
                await db.commit()
        await _publish_notice(ticket_id, message_id, author_id, text, 'closed' if close else 'answered')


ticket_automation_service = TicketAutomationService()
