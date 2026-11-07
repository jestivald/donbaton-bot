"""Публикация фоновых ответов поддержки с повторной проверкой тикета."""

from datetime import UTC, datetime
from typing import Literal

import structlog
from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import selectinload

from app.database.crud.ticket import TicketCRUD
from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.database.database import AsyncSessionLocal
from app.database.models import SupportAuditLog, Ticket, TicketMessage, User
from app.services.event_emitter import event_emitter
from app.services.support_settings_service import SupportSettingsService


logger = structlog.get_logger(__name__)


async def _ai_author_id(db) -> int:
    """Служебный автор без способов входа и административных прав."""
    await db.execute(sql_text('SELECT pg_advisory_xact_lock(714201, 1)'))
    author = await db.scalar(select(User).where(User.auth_type == 'ai_support').limit(1))
    if author is None:
        author = User(auth_type='ai_support', first_name='ИИ-помощник', language='ru')
        db.add(author)
        await db.flush()
    return author.id


async def publish_support_reply(
    *,
    ticket_id: int,
    author_user_id: int | None,
    text: str,
    mark_answered: bool,
    origin: Literal['human', 'ai', 'system'],
    trigger_message_id: int | None = None,
) -> int | None:
    """Сохранить один ответ, затем независимо доставить его во все каналы.

    ``trigger_message_id`` проверяется также при отправке черновика человеком.
    Сессия генерации здесь не используется; сетевой доставки под блокировкой нет.
    """
    try:
        async with AsyncSessionLocal() as db:
            ticket = await db.scalar(
                select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.user)).with_for_update()
            )
            if ticket is None or ticket.status == 'closed':
                return None
            if origin == 'ai':
                if (
                    trigger_message_id is None
                    or ticket.is_user_reply_blocked
                    or not SupportSettingsService.is_tickets_enabled()
                    or await TicketCRUD.is_user_globally_blocked(db, ticket.user_id)
                ):
                    return None
            if trigger_message_id is not None:
                last = await db.scalar(
                    select(TicketMessage)
                    .where(TicketMessage.ticket_id == ticket_id)
                    .order_by(TicketMessage.id.desc())
                    .limit(1)
                )
                if last is None or last.id != trigger_message_id or last.is_from_admin:
                    return None
            if author_user_id is None:
                if origin == 'human':
                    return None
                author_user_id = await _ai_author_id(db)
            message = TicketMessage(
                ticket_id=ticket_id, user_id=author_user_id, message_text=text, is_from_admin=True, has_media=False
            )
            db.add(message)
            ticket.status = 'answered' if mark_answered else 'open'
            ticket.last_sla_reminder_at = None
            ticket.updated_at = datetime.now(UTC)
            await db.flush()
            message_id = message.id
            if origin == 'human' and trigger_message_id is not None:
                db.add(
                    SupportAuditLog(
                        action='ai_support_human_reply',
                        actor_user_id=author_user_id,
                        ticket_id=ticket_id,
                        details={'message_id': message_id},
                    )
                )
            status = ticket.status
            await db.commit()
    except Exception:
        logger.exception('Не удалось опубликовать ответ поддержки', ticket_id=ticket_id, origin=origin)
        return None

    # После commit каждая доставка независима: ошибка одного канала не теряет остальные.
    try:
        async with AsyncSessionLocal() as event_db:
            await event_emitter.emit(
                'ticket.message_added',
                {
                    'ticket_id': ticket_id,
                    'message_id': message_id,
                    'user_id': author_user_id,
                    'is_from_admin': True,
                    'message_text': text[:200],
                    'has_media': False,
                    'status': status,
                },
                db=event_db,
            )
    except Exception:
        logger.exception('Не удалось отправить событие ответа', ticket_id=ticket_id)
    await _deliver_reply(ticket_id, text, message_id)
    return message_id


async def _deliver_reply(ticket_id: int, text: str, message_id: int) -> None:
    from app.cabinet.routes.websocket import notify_user_ticket_reply
    from app.handlers.admin.tickets import notify_user_about_ticket_reply
    from app.services.maintenance_service import maintenance_service

    try:
        async with AsyncSessionLocal() as db:
            ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False, load_user=True)
            if ticket is None:
                return
            # Если доставка поддерживает точное сообщение, исключаем гонку вложений.
            # Со старой сигнатурой остаётся выбор по последнему сообщению тикета.
            import inspect

            kwargs = {}
            if 'reply_message' in inspect.signature(notify_user_about_ticket_reply).parameters:
                kwargs['reply_message'] = await db.get(TicketMessage, message_id)
            if SupportSettingsService.get_user_ticket_notifications_enabled():
                try:
                    await notify_user_about_ticket_reply(maintenance_service._bot, ticket, text, db, **kwargs)
                except Exception:
                    logger.exception('Не удалось доставить ответ в Telegram/email', ticket_id=ticket_id)
            try:
                notification = await TicketNotificationCRUD.create_user_notification_for_admin_reply(db, ticket, text)
            except Exception:
                notification = None
                logger.exception('Не удалось сохранить уведомление кабинета', ticket_id=ticket_id)
            if notification:
                try:
                    await notify_user_ticket_reply(ticket.user_id, ticket_id, text[:100])
                except Exception:
                    logger.exception('Не удалось отправить WS-уведомление кабинета', ticket_id=ticket_id)
    except Exception:
        logger.exception('Не удалось подготовить доставку ответа', ticket_id=ticket_id)
