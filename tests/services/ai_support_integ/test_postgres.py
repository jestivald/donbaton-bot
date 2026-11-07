"""Гарантии конкурентной публикации проверяет оркестратор на PostgreSQL."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import SupportAuditLog, Ticket, TicketMessage, User
from app.handlers import ai_support as chat
from app.services import ticket_reply_service as service
from app.services.ai_support import api
from tests.fixtures.postgres_db import postgres_engine, wait_for_lock_waiter


pytestmark = pytest.mark.postgres
TABLES = [User.__table__, Ticket.__table__, TicketMessage.__table__, SupportAuditLog.__table__]


async def test_concurrent_publish_rechecks_trigger_under_row_lock(postgres_database, monkeypatch):
    async with postgres_engine(postgres_database, TABLES) as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            user = User(first_name='Клиент', language='ru')
            db.add(user)
            await db.flush()
            ticket = Ticket(user_id=user.id, title='Вопрос', status='open')
            db.add(ticket)
            await db.flush()
            question = TicketMessage(ticket_id=ticket.id, user_id=user.id, message_text='Помогите', is_from_admin=False)
            db.add(question)
            admin = User(first_name='Админ', language='ru', telegram_id=900001)
            db.add(admin)
            await db.commit()
            ticket_id, trigger, admin_id = ticket.id, question.id, admin.id

        held = asyncio.Event()
        waiter_seen = asyncio.Event()
        first_read = True
        async with factory() as watcher:

            class GatedSession(AsyncSession):
                async def scalar(self, statement, *args, **kwargs):
                    nonlocal first_read
                    result = await super().scalar(statement, *args, **kwargs)
                    if 'FOR UPDATE' in str(statement) and first_read:
                        first_read = False
                        held.set()
                        await wait_for_lock_waiter(watcher)
                        waiter_seen.set()
                    return result

            monkeypatch.setattr(
                service, 'AsyncSessionLocal', async_sessionmaker(engine, class_=GatedSession, expire_on_commit=False)
            )
            monkeypatch.setattr(service.SupportSettingsService, 'is_tickets_enabled', lambda: True)
            monkeypatch.setattr(service.event_emitter, 'emit', AsyncMock())
            monkeypatch.setattr(service, '_deliver_reply', AsyncMock())
            monkeypatch.setattr(service.settings, 'AI_SUPPORT_AUTHOR_TELEGRAM_ID', 900001)

            async def publish():
                return await service.publish_support_reply(
                    ticket_id=ticket_id,
                    author_user_id=None,
                    text='Ответ ИИ',
                    mark_answered=False,
                    origin='ai',
                    trigger_message_id=trigger,
                )

            first = asyncio.create_task(publish())
            await asyncio.wait_for(held.wait(), timeout=5)
            second = asyncio.create_task(publish())
            results = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
            assert waiter_seen.is_set(), 'Второй издатель обязан ждать блокировку первого'
            assert sum(result is not None for result in results) == 1
        async with factory() as db:
            assert (
                await db.scalar(
                    select(func.count()).select_from(TicketMessage).where(TicketMessage.is_from_admin.is_(True))
                )
                == 1
            )
            assert await db.scalar(select(func.count()).select_from(User)) == 2  # клиент и админ, без служебного автора
            reply = await db.scalar(select(TicketMessage).where(TicketMessage.is_from_admin.is_(True)))
            assert reply.user_id == admin_id
            assert (await db.get(Ticket, ticket_id)).status == 'open'


async def test_chat_handoff_survives_restart_and_creates_one_ticket(postgres_database, monkeypatch):
    async with postgres_engine(postgres_database, TABLES) as engine:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            user = User(first_name='Клиент', language='ru')
            db.add(user)
            await db.commit()
            user_id = user.id
        monkeypatch.setattr(chat, 'AsyncSessionLocal', factory)
        monkeypatch.setattr(chat.SupportSettingsService, 'is_tickets_enabled', lambda: True)
        monkeypatch.setattr(chat, 'notify_admins_about_new_ticket', AsyncMock())
        monkeypatch.setattr(service.event_emitter, 'emit', AsyncMock())
        turns = [api.ChatTurn('user', 'Нужен человек')]
        results = await asyncio.gather(
            *(chat._get_or_create_handoff(user_id, 123, turns, 'client_request') for _ in range(2))
        )
        chat._request_memory.clear()
        replay = await chat._get_or_create_handoff(user_id, 123, [], 'client_request')
        assert results[0] == results[1] == replay
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(Ticket)) == 1
            assert await db.scalar(select(func.count()).select_from(TicketMessage)) == 1
        chat.notify_admins_about_new_ticket.assert_awaited_once()
