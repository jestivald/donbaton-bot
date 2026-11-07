"""Автоматизация на реальных SQL-запросах без сети."""

import importlib.util
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.ticket import TicketMessageCRUD
from app.database.models import AiSupportRequest, SupportAuditLog, Ticket, TicketMessage, User
from app.services import ticket_automation_service as automation
from app.services.event_emitter import event_emitter
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
TABLES = [model.__table__ for model in (User, Ticket, TicketMessage, SupportAuditLog, AiSupportRequest)]


@asynccontextmanager
async def automation_db(monkeypatch, *, age=73, text='Удалось подключиться?', status='answered', admin=True):
    async with memory_session(monkeypatch, TABLES) as db:

        @asynccontextmanager
        async def session():
            yield db

        monkeypatch.setattr(automation, 'AsyncSessionLocal', session)
        monkeypatch.setattr(automation, '_ai_author_id', AsyncMock(return_value=2))
        monkeypatch.setattr(automation, '_deliver_reply', AsyncMock())
        monkeypatch.setattr(event_emitter, 'emit', AsyncMock())
        monkeypatch.setattr(automation.SupportSettingsService, 'is_tickets_enabled', lambda: True)
        monkeypatch.setattr(settings, 'SUPPORT_QUIET_HOURS', '23:00-09:00')
        monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow')
        monkeypatch.setattr(settings, 'SUPPORT_TICKET_AUTOCLOSE_HOURS', 72)
        monkeypatch.setattr(settings, 'SUPPORT_TICKET_CLIENT_REMINDER_HOURS', 24)
        ticket = Ticket(id=1, user_id=1, title='<Подключение>', status=status)
        db.add_all(
            [
                User(id=1, language='ru', telegram_id=100),
                User(id=2, language='ru'),
                ticket,
                TicketMessage(
                    id=1,
                    ticket_id=1,
                    user_id=2 if admin else 1,
                    is_from_admin=admin,
                    message_text=text,
                    created_at=NOW - timedelta(hours=age),
                ),
            ]
        )
        await db.commit()
        yield db, ticket


def ai_record(**kwargs):
    return AiSupportRequest(channel='ticket', user_id=1, ticket_id=1, model='test', provider='test', **kwargs)


async def test_autoclose_stores_notice_and_uses_standard_status_event(monkeypatch):
    async with automation_db(monkeypatch) as (db, ticket):
        service = automation.TicketAutomationService()
        await service.run_once(NOW)
        assert ticket.status == 'closed'
        assert ticket.autoclosed_at == NOW
        assert ticket.closed_at is not None
        messages = list((await db.scalars(select(TicketMessage).order_by(TicketMessage.id))).all())
        assert len(messages) == 2
        assert '72' in messages[-1].message_text and '<Подключение>' in messages[-1].message_text
        assert messages[-1].is_from_admin
        status = [call for call in event_emitter.emit.call_args_list if call.args[0] == 'ticket.status_changed']
        assert len(status) == 1 and status[0].args[1]['old_status'] == 'answered'
        assert status[0].args[1]['new_status'] == 'closed'
        automation._deliver_reply.assert_awaited_once()
        await service.run_once(NOW + timedelta(hours=1))
        automation._deliver_reply.assert_awaited_once()


@pytest.mark.parametrize('age', [23, 24, 71, 72])
async def test_thresholds(monkeypatch, age):
    async with automation_db(monkeypatch, age=age) as (_, ticket):
        await automation.TicketAutomationService().run_once(NOW)
        assert (ticket.status == 'closed') == (age >= 72)
        assert (ticket.client_reminder_sent_at is not None) == (24 <= age < 72)
        assert automation._deliver_reply.await_count == int(age >= 24)


async def test_reminder_once_after_restart_does_not_postpone_autoclose(monkeypatch):
    async with automation_db(monkeypatch, age=24) as (db, ticket):
        await automation.TicketAutomationService().run_once(NOW)
        assert ticket.client_reminder_sent_at == NOW
        await automation.TicketAutomationService().run_once(NOW + timedelta(hours=1))
        assert automation._deliver_reply.await_count == 1
        await automation.TicketAutomationService().run_once(NOW + timedelta(hours=48))
        assert ticket.status == 'closed'
        assert automation._deliver_reply.await_count == 2
        assert len(list((await db.scalars(select(TicketMessage))).all())) == 3


@pytest.mark.parametrize('status,admin', [('open', True), ('pending', True), ('closed', True), ('answered', False)])
async def test_ineligible_status_or_last_client_message(monkeypatch, status, admin):
    async with automation_db(monkeypatch, status=status, admin=admin) as (_, ticket):
        await automation.TicketAutomationService().run_once(NOW)
        assert ticket.status == status
        automation._deliver_reply.assert_not_awaited()


@pytest.mark.parametrize('done', [False, True])
async def test_pending_and_completed_handoff_wait_for_human(monkeypatch, done):
    async with automation_db(monkeypatch) as (db, ticket):
        db.add(
            ai_record(
                outcome='handoff',
                handoff_reason='money',
                reply_message_id=1,
                created_at=NOW - timedelta(hours=74),
                handoff_done_at=NOW if done else None,
            )
        )
        await db.commit()
        await automation.TicketAutomationService().run_once(NOW)
        assert ticket.status == 'answered'
        automation._deliver_reply.assert_not_awaited()


async def test_human_answer_after_handoff_allows_autoclose(monkeypatch):
    async with automation_db(monkeypatch) as (db, ticket):
        db.add(ai_record(outcome='handoff', handoff_reason='other', created_at=NOW - timedelta(hours=80)))
        await db.commit()
        await automation.TicketAutomationService().run_once(NOW)
        assert ticket.status == 'closed'


async def test_human_sent_draft_after_handoff_allows_autoclose(monkeypatch):
    async with automation_db(monkeypatch) as (db, ticket):
        db.add(
            ai_record(
                outcome='handoff', handoff_reason='other', created_at=NOW - timedelta(hours=80), reply_message_id=1
            )
        )
        db.add(SupportAuditLog(ticket_id=1, action='ai_support_human_reply', details={'message_id': 1}))
        await db.commit()
        await automation.TicketAutomationService().run_once(NOW)
        assert ticket.status == 'closed'


@pytest.mark.parametrize('age', [24, 73])
async def test_quiet_hours_defer_both_actions_until_morning(monkeypatch, age):
    async with automation_db(monkeypatch, age=age) as (_, ticket):
        night = NOW.replace(hour=21)  # 00:00 МСК
        await automation.TicketAutomationService().run_once(night)
        assert ticket.status == 'answered' and ticket.client_reminder_sent_at is None
        automation._deliver_reply.assert_not_awaited()
        await automation.TicketAutomationService().run_once(night + timedelta(hours=9))
        automation._deliver_reply.assert_awaited_once()


@pytest.mark.parametrize('close,reminder', [(0, 0), (0, 24), (72, 0)])
async def test_independent_off_switches(monkeypatch, close, reminder):
    async with automation_db(monkeypatch) as (_, ticket):
        monkeypatch.setattr(settings, 'SUPPORT_TICKET_AUTOCLOSE_HOURS', close)
        monkeypatch.setattr(settings, 'SUPPORT_TICKET_CLIENT_REMINDER_HOURS', reminder)
        await automation.TicketAutomationService().run_once(NOW)
        assert (ticket.status == 'closed') == bool(close)
        assert (ticket.client_reminder_sent_at is not None) == bool(reminder and not close)


async def test_reminder_requires_question(monkeypatch):
    async with automation_db(monkeypatch, age=25, text='Спасибо, настройки готовы.') as (_, ticket):
        await automation.TicketAutomationService().run_once(NOW)
        assert ticket.client_reminder_sent_at is None
        automation._deliver_reply.assert_not_awaited()


@pytest.mark.parametrize('quiet,enabled', [(True, True), (False, True), (True, False)])
def test_night_handoff_text(monkeypatch, quiet, enabled):
    monkeypatch.setattr(automation, 'is_quiet_now', lambda: quiet)
    monkeypatch.setattr(automation, 'quiet_end_label', lambda: '09:00')
    monkeypatch.setattr(settings, 'SUPPORT_NIGHT_AUTOREPLY_ENABLED', enabled)
    text = automation.night_handoff_reply('Передал оператору.', 'ru')
    assert ('09:00' in text) == (quiet and enabled)


async def test_client_reply_after_reminder_prevents_autoclose(monkeypatch):
    async with automation_db(monkeypatch, age=24) as (db, ticket):
        await automation.TicketAutomationService().run_once(NOW)
        db.add(TicketMessage(ticket_id=1, user_id=1, is_from_admin=False, message_text='Не помогло', created_at=NOW))
        await db.commit()
        await automation.TicketAutomationService().run_once(NOW + timedelta(hours=50))
        assert ticket.status == 'answered'  # Даже при несогласованном статусе последнее слово за клиентом.
        assert automation._deliver_reply.await_count == 1


async def test_current_crud_does_not_reopen_closed_ticket(monkeypatch):
    # Регрессия существующего поведения; исправление CRUD поручается оркестратору вне файлов T2.
    async with automation_db(monkeypatch, status='closed') as (db, ticket):
        await TicketMessageCRUD.add_message(db, 1, 1, 'Вопрос остался', is_from_admin=False)
        assert ticket.status == 'closed'
        assert len(db.new) == 1  # Ранний return не делает даже commit.


async def test_service_lifecycle(monkeypatch):
    service = automation.TicketAutomationService()
    monkeypatch.setattr(service, 'run_once', AsyncMock())
    await service.start()
    task = service._task
    await service.start()
    assert task is service._task
    await service.stop()
    assert service._task is None and task.cancelled()


@pytest.mark.parametrize('quiet', [False, True])
async def test_bot_chat_handoff_confirmation_has_night_notice(monkeypatch, quiet):
    from app.handlers import ai_support as chat
    from app.services.ai_support import api

    monkeypatch.setattr(chat, '_get_or_create_handoff', AsyncMock(return_value=1))
    monkeypatch.setattr(api, 'mark_handoff_done', AsyncMock())
    monkeypatch.setattr(automation, 'is_quiet_now', lambda: quiet)
    monkeypatch.setattr(automation, 'quiet_end_label', lambda: '09:00')
    monkeypatch.setattr(settings, 'SUPPORT_NIGHT_AUTOREPLY_ENABLED', True)
    message = SimpleNamespace(answer=AsyncMock())
    await chat.handoff_to_operator(message, 1, 'ru', 1, [])
    assert ('09:00' in message.answer.call_args.args[0]) == quiet


def test_migration_roundtrip():
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/0145_ticket_automation.py'
    spec = importlib.util.spec_from_file_location('ticket_automation_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == '0145' and migration.down_revision == '0144'
    engine = sa.create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE tickets (id INTEGER PRIMARY KEY)')
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                names = {col['name'] for col in sa.inspect(conn).get_columns('tickets')}
                assert names == {'id', 'client_reminder_sent_at', 'autoclosed_at'}
                migration.downgrade()
                assert {col['name'] for col in sa.inspect(conn).get_columns('tickets')} == {'id'}
    finally:
        engine.dispose()
