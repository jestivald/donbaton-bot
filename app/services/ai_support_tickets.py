"""Неблокирующая очередь ответов ИИ на клиентские сообщения тикетов."""

import asyncio
from collections import Counter, OrderedDict
from dataclasses import dataclass

import structlog
from sqlalchemy import func, or_, select

from app.database.crud.ticket import TicketCRUD
from app.database.database import AsyncSessionLocal
from app.database.models import AiSupportRequest, SupportAuditLog, Ticket, TicketMessage
from app.services.ai_support import api
from app.services.ai_support.images import fetch_telegram_images, ticket_image_refs, unavailable_question
from app.services.ai_support.redaction import bounded_text
from app.services.ai_support_notify import admin_alert, notify_ticket_answer, send_notice
from app.services.event_emitter import event_emitter
from app.services.maintenance_service import maintenance_service
from app.services.support_settings_service import SupportSettingsService
from app.services.ticket_automation_service import explicit_close_request, finish_ai_close, night_handoff_reply
from app.services.ticket_reply_service import publish_support_reply


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TicketJob:
    ticket_id: int
    message_id: int
    user_id: int | None = None


MAX_HISTORY_MESSAGES = 32
MAX_PENDING_PER_USER = 3


async def load_conversation(job: TicketJob):
    """Актуальность — одним ID; истории загружается только ограниченный хвост."""
    async with AsyncSessionLocal() as db:
        latest = await db.scalar(
            select(TicketMessage.id)
            .where(TicketMessage.ticket_id == job.ticket_id)
            .order_by(TicketMessage.id.desc())
            .limit(1)
        )
        if latest != job.message_id:
            return None
        ticket = await TicketCRUD.get_ticket_by_id(db, job.ticket_id, load_messages=False, load_user=True)
        if (
            ticket is None
            or ticket.status == 'closed'
            or ticket.is_user_reply_blocked
            or not SupportSettingsService.is_tickets_enabled()
            or await TicketCRUD.is_user_globally_blocked(db, ticket.user_id)
        ):
            return None
        rows = await db.scalars(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == job.ticket_id)
            .order_by(TicketMessage.id.desc())
            .limit(MAX_HISTORY_MESSAGES)
        )
        messages = list(reversed(rows.all()))
        if not messages or messages[-1].id != job.message_id or messages[-1].is_from_admin:
            return None
        return ticket, messages


async def _has_human_reply(ticket_id: int) -> bool:
    """Ответ человека в старой части истории тоже навсегда выключает автоответ."""
    async with AsyncSessionLocal() as db:
        ai_ids = select(AiSupportRequest.reply_message_id).where(
            AiSupportRequest.ticket_id == ticket_id, AiSupportRequest.reply_message_id.is_not(None)
        )
        sent_drafts = select(SupportAuditLog.details['message_id'].as_integer()).where(
            SupportAuditLog.ticket_id == ticket_id, SupportAuditLog.action == 'ai_support_human_reply'
        )
        return (
            await db.scalar(
                select(TicketMessage.id)
                .where(
                    TicketMessage.ticket_id == ticket_id,
                    TicketMessage.is_from_admin.is_(True),
                    or_(TicketMessage.id.not_in(ai_ids), TicketMessage.id.in_(sent_drafts)),
                )
                .limit(1)
            )
            is not None
        )


def conversation_input(messages: list[TicketMessage]) -> tuple[str, list[api.ChatTurn]]:
    last_support = max((i for i, message in enumerate(messages) if message.is_from_admin), default=-1)
    question = '\n\n'.join((message.message_text or '')[:4000] for message in messages[last_support + 1 :])
    question = bounded_text(question)
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


async def _ticket_open(ticket_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        status = await db.scalar(select(Ticket.status).where(Ticket.id == ticket_id))
    return status is not None and status != 'closed'


class AiTicketService:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1000)
        self._workers: list[asyncio.Task] = []
        self._pending: dict[int, TicketJob] = {}
        self._user_pending: Counter[int] = Counter()
        self._completed: OrderedDict[TicketJob, None] = OrderedDict()

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
        self._user_pending.clear()
        self._completed.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        api.set_admin_alert_handler(None)

    def enqueue(self, ticket_id: int, message_id: int, user_id: int) -> bool:
        """Один слот на тикет, до трёх на клиента; источники передают серверный ID владельца."""
        job = TicketJob(ticket_id, message_id, user_id)
        if not self._workers or job in self._completed:
            return False
        pending = self._pending.get(ticket_id)
        if pending is not None:
            if pending.user_id != user_id or message_id <= pending.message_id:
                return False
            self._pending[ticket_id] = job
            return True
        if self._user_pending[user_id] >= MAX_PENDING_PER_USER or len(self._pending) >= self._queue.maxsize:
            return False
        try:
            self._queue.put_nowait(ticket_id)
        except asyncio.QueueFull:
            logger.warning('Очередь ИИ-поддержки заполнена', ticket_id=ticket_id)
            return False
        self._pending[ticket_id] = job
        self._user_pending[user_id] += 1
        return True

    def _on_event(self, event: dict) -> None:
        payload = event.get('payload', {})
        if payload.get('is_from_admin', False):
            return
        ticket_id, message_id, user_id = (payload.get(key) for key in ('ticket_id', 'message_id', 'user_id'))
        if all(type(value) is int for value in (ticket_id, message_id, user_id)):
            self.enqueue(ticket_id, message_id, user_id)

    async def _worker(self) -> None:
        while True:
            ticket_id = await self._queue.get()
            job = self._pending[ticket_id]
            try:
                await self.process(job)
                self._completed[job] = None
                if len(self._completed) > 2000:
                    self._completed.popitem(last=False)
            except Exception:
                logger.exception('Сбой обработки тикета ИИ', ticket_id=job.ticket_id)
            finally:
                if self._pending[ticket_id] != job:
                    # Пока модель отвечала, клиент уточнил вопрос. Слот остаётся за тем же тикетом.
                    self._queue.put_nowait(ticket_id)
                else:
                    del self._pending[ticket_id]
                    self._user_pending[job.user_id] -= 1
                    if not self._user_pending[job.user_id]:
                        del self._user_pending[job.user_id]
                self._queue.task_done()

    async def process(self, job: TicketJob) -> None:
        mode = api.ticket_mode()
        if mode == 'off':
            return
        conversation = await load_conversation(job)
        if conversation is None:
            return
        ticket, messages = conversation
        if any((m.message_text or '').startswith('Обращение из чата ИИ №') for m in (messages[0], messages[-1])):
            return
        if mode == 'auto':
            if (
                await _has_human_reply(ticket.id)
                or await api.ticket_has_handoff(ticket.id)
                or await api.count_ai_replies_in_ticket(ticket.id) >= api.max_ai_replies_per_ticket()
            ):
                return
        elif await _draft_count(ticket.id) >= 10:
            return
        question, history = conversation_input(messages)
        images = []
        refs = ticket_image_refs(messages) if api.vision_enabled() else []
        if refs:
            images = await fetch_telegram_images(maintenance_service._bot, refs)
            if not images:
                question = unavailable_question(question)
        if not question.strip() and not images:
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
            **({'images': images} if images else {}),
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
            # Решение «передать человеку» верно и после нового сообщения клиента: админов уведомляем, ИИ в тикете
            # дальше молчит. Клиенту не пишем — его новое сообщение и так ждёт оператора.
            if mode == 'auto' and result.outcome == api.AiOutcome.HANDOFF and await _ticket_open(ticket.id):
                await api.mark_handoff_done(result.request_id, ticket.id)
                await notify_ticket_answer(ticket.id, result)
            return
        if mode == 'draft':
            if result.reply_text and await _reserve_draft(ticket.id, result.request_id):
                await notify_ticket_answer(ticket.id, result, draft=True)
            return
        if result.outcome == api.AiOutcome.ANSWERED and explicit_close_request(question):
            closed_message = await finish_ai_close(
                api.AiRequestContext(
                    api.AiChannel.TICKET,
                    ticket.user_id,
                    language=ticket.user.language if ticket.user else 'ru',
                    ticket_id=ticket.id,
                    trigger_message_id=job.message_id,
                )
            )
            if closed_message is not None:
                await api.attach_reply_message(result.request_id, closed_message)
                await notify_ticket_answer(ticket.id, result)
                return
        handoff = result.outcome == api.AiOutcome.HANDOFF
        reply = result.reply_text or ('Приглашаю оператора.' if handoff else '')
        if not reply:
            return
        if handoff:
            reply = night_handoff_reply(reply, ticket.user.language if ticket.user else 'ru')
        published = await publish_support_reply(
            ticket_id=ticket.id,
            author_user_id=None,
            # После передачи человеку подпись «напишите «оператор»» уже не нужна.
            # Без префикса «🤖 ИИ-помощник» (решение владельца 07.11): клиент видит просто ответ. Ответы ИИ
            # отличаются по журналу (reply_message_id), админам копия приходит с пометкой «🤖 Ответ ИИ».
            text=reply + ('' if handoff else f'\n\n{api.reply_footer_text()}'),
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


def enqueue_ticket_message(ticket_id: int, message_id: int, user_id: int) -> bool:
    """Безопасная точка входа для мобильного WS и других источников."""
    try:
        return ai_ticket_service.enqueue(ticket_id, message_id, user_id)
    except Exception:
        logger.exception('Не удалось поставить тикет в очередь ИИ', ticket_id=ticket_id)
        return False
