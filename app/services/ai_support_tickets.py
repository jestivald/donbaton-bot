"""Неблокирующая очередь ответов ИИ на клиентские сообщения тикетов."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

import structlog
from sqlalchemy import func, select

from app.database.crud.ticket import TicketCRUD
from app.database.database import AsyncSessionLocal
from app.database.models import SupportAuditLog, Ticket, TicketMessage
from app.services.ai_support import api
from app.services.ai_support_notify import admin_alert, notify_ticket_answer, send_notice
from app.services.event_emitter import event_emitter
from app.services.support_settings_service import SupportSettingsService
from app.services.ticket_reply_service import publish_support_reply


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TicketJob:
    ticket_id: int
    message_id: int


async def load_conversation(job: TicketJob):
    """Короткий снимок; к моменту обращения к модели сессия уже закрыта."""
    async with AsyncSessionLocal() as db:
        ticket = await TicketCRUD.get_ticket_by_id(db, job.ticket_id, load_messages=True, load_user=True)
        if (
            ticket is None
            or ticket.status == 'closed'
            or ticket.is_user_reply_blocked
            or not SupportSettingsService.is_tickets_enabled()
            or await TicketCRUD.is_user_globally_blocked(db, ticket.user_id)
        ):
            return None
        messages = sorted(ticket.messages, key=lambda message: message.id)
        if not messages or messages[-1].id != job.message_id or messages[-1].is_from_admin:
            return None
        return ticket, messages


def conversation_input(messages: list[TicketMessage]) -> tuple[str, list[api.ChatTurn]]:
    last_support = max((i for i, message in enumerate(messages) if message.is_from_admin), default=-1)
    question = '\n\n'.join(message.message_text or '' for message in messages[last_support + 1 :])
    turns = [
        api.ChatTurn('assistant' if message.is_from_admin else 'user', message.message_text or '')
        for message in messages[: last_support + 1]
    ]
    limit = api.history_turns()
    return question, turns[-limit:] if limit > 0 else []


async def _draft_count(ticket_id: int) -> int:
    async with AsyncSessionLocal() as db:
        return (
            await db.scalar(
                select(func.count(SupportAuditLog.id)).where(
                    SupportAuditLog.ticket_id == ticket_id, SupportAuditLog.action == 'ai_support_draft'
                )
            )
            or 0
        )


async def _human_published_ids(ticket_id: int) -> set[int]:
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(
            select(SupportAuditLog).where(
                SupportAuditLog.ticket_id == ticket_id, SupportAuditLog.action == 'ai_support_human_reply'
            )
        )
        return {row.details['message_id'] for row in rows if row.details and 'message_id' in row.details}


async def _reserve_draft(ticket_id: int, request_id: int) -> bool:
    """Лимит черновиков сохраняется после перезапуска, без зависимости от моделей I1."""
    async with AsyncSessionLocal() as db:
        ticket = await db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
        if ticket is None or ticket.status == 'closed':
            return False
        count = (
            await db.scalar(
                select(func.count(SupportAuditLog.id)).where(
                    SupportAuditLog.ticket_id == ticket_id, SupportAuditLog.action == 'ai_support_draft'
                )
            )
            or 0
        )
        if count >= 10:
            return False
        db.add(SupportAuditLog(action='ai_support_draft', ticket_id=ticket_id, details={'request_id': request_id}))
        await db.commit()
        return True


class AiTicketService:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[TicketJob] = asyncio.Queue(maxsize=1000)
        self._workers: list[asyncio.Task] = []
        self._pending: set[TicketJob] = set()
        self._completed: OrderedDict[TicketJob, None] = OrderedDict()
        # Полосы обеспечивают порядок внутри тикета и ограниченный объём памяти.
        self._locks = [asyncio.Lock() for _ in range(64)]

    async def start(self) -> None:
        if self._workers:
            return
        api.set_admin_alert_handler(admin_alert)
        event_emitter.on('ticket.created', self._on_event)
        event_emitter.on('ticket.message_added', self._on_event)
        self._workers = [asyncio.create_task(self._worker(), name=f'ai-ticket-{i}') for i in range(2)]

    async def stop(self) -> None:
        event_emitter.off('ticket.created', self._on_event)
        event_emitter.off('ticket.message_added', self._on_event)
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._pending.clear()
        self._completed.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        api.set_admin_alert_handler(None)

    def enqueue(self, ticket_id: int, message_id: int) -> bool:
        """Только put_nowait: обработчик события никогда не ждёт модель или БД."""
        job = TicketJob(ticket_id, message_id)
        if not self._workers or job in self._pending or job in self._completed:
            return False
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.warning('Очередь ИИ-поддержки заполнена', ticket_id=ticket_id)
            return False
        self._pending.add(job)
        return True

    def _on_event(self, event: dict) -> None:
        payload = event.get('payload', {})
        if payload.get('is_from_admin', False):
            return
        ticket_id, message_id = payload.get('ticket_id'), payload.get('message_id')
        if isinstance(ticket_id, int) and isinstance(message_id, int):
            self.enqueue(ticket_id, message_id)

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                async with self._locks[job.ticket_id % len(self._locks)]:
                    await self.process(job)
                self._completed[job] = None
                if len(self._completed) > 2000:
                    self._completed.popitem(last=False)
            except Exception:
                logger.exception('Сбой обработки тикета ИИ', ticket_id=job.ticket_id)
            finally:
                self._pending.discard(job)
                self._queue.task_done()

    async def process(self, job: TicketJob) -> None:
        mode = api.ticket_mode()
        if mode == 'off':
            return
        conversation = await load_conversation(job)
        if conversation is None:
            return
        ticket, messages = conversation
        if (messages[0].message_text or '').startswith('Обращение из чата ИИ №'):
            return
        if mode == 'auto':
            ai_ids = await api.ai_reply_message_ids(ticket.id)
            human_ids = await _human_published_ids(ticket.id)
            if (
                any(
                    message.is_from_admin and (message.id not in ai_ids or message.id in human_ids)
                    for message in messages
                )
                or await api.ticket_has_handoff(ticket.id)
                or await api.count_ai_replies_in_ticket(ticket.id) >= api.max_ai_replies_per_ticket()
            ):
                return
        elif await _draft_count(ticket.id) >= 10:
            return
        question, history = conversation_input(messages)
        if not question.strip():
            return
        result = await api.answer(
            question,
            history,
            api.AiRequestContext(
                api.AiChannel.TICKET,
                ticket.user_id,
                language=ticket.user.language if ticket.user else 'ru',
                ticket_id=ticket.id,
                trigger_message_id=job.message_id,
                ticket_title=ticket.title,
            ),
        )
        if result.outcome == api.AiOutcome.DUPLICATE:
            return
        if result.outcome not in {api.AiOutcome.ANSWERED, api.AiOutcome.HANDOFF}:
            logger.info('ИИ оставил тикет оператору', ticket_id=ticket.id, outcome=result.outcome)
            if result.outcome == api.AiOutcome.FILTERED:
                await send_notice('ИИ не смог ответить безопасно', f'Тикет #{ticket.id}', ticket_id=ticket.id)
            return
        if result.request_id is None:
            return
        # Модель могла отвечать долго: черновик и автоответ не должны обгонять клиента.
        if api.ticket_mode() != mode or await load_conversation(job) is None:
            return
        if mode == 'draft':
            if result.reply_text and await _reserve_draft(ticket.id, result.request_id):
                await notify_ticket_answer(ticket.id, result, draft=True)
            return
        handoff = result.outcome == api.AiOutcome.HANDOFF
        reply = result.reply_text or ('Приглашаю оператора.' if handoff else '')
        if not reply:
            return
        published = await publish_support_reply(
            ticket_id=ticket.id,
            author_user_id=None,
            text=f'{api.AI_REPLY_PREFIX}\n\n{reply}\n\n{api.reply_footer_text()}',
            mark_answered=not handoff,
            origin='ai',
            trigger_message_id=job.message_id,
        )
        if published is None:
            return
        await api.attach_reply_message(result.request_id, published)
        if handoff:
            await api.mark_handoff_done(result.request_id, ticket.id)
        await notify_ticket_answer(ticket.id, result)


ai_ticket_service = AiTicketService()


def enqueue_ticket_message(ticket_id: int, message_id: int) -> bool:
    """Безопасная точка входа для мобильного WS и других источников."""
    try:
        return ai_ticket_service.enqueue(ticket_id, message_id)
    except Exception:
        logger.exception('Не удалось поставить тикет в очередь ИИ', ticket_id=ticket_id)
        return False
