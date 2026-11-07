"""Политика сводок без сети, живой БД и зависимости от часов машины."""

import asyncio
import html
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import monitoring_service as monitoring, ticket_digest_service as digest
from app.services.ai_support import api
from app.services.monitoring_service import MonitoringService
from app.services.support_settings_service import SupportSettingsService


DAY = datetime(2026, 11, 8, 9, tzinfo=UTC)  # 12:00 Moscow
MORNING = DAY.replace(hour=6)
NIGHT = DAY.replace(hour=21)
LOAD_TICKETS = digest._load_tickets


class RedisMemory:
    """Shared Redis surrogate: state outlives individual monitoring instances."""

    def __init__(self):
        self.now = DAY
        self.values = {}

    async def get(self, key):
        item = self.values.get(key)
        if item and item[1] > self.now:
            return item[0]
        self.values.pop(key, None)
        return None

    async def set(self, key, value, *, expire):
        self.values[key] = (value, self.now + timedelta(seconds=expire))
        return True

    async def setnx(self, key, value, *, expire):
        if await self.get(key) is not None:
            return False
        return await self.set(key, value, expire=expire)

    async def delete(self, key):
        self.values.pop(key, None)
        return True


def ticket(ticket_id=1, **kwargs):
    values = {'id': ticket_id, 'title': 'Не работает VPN', 'updated_at': DAY - timedelta(hours=10)}
    values.update(kwargs)
    return digest.DigestTicket(**values)


@pytest.fixture
def env(monkeypatch):
    for name, value in {
        'TIMEZONE': 'Europe/Moscow',
        'DEFAULT_LANGUAGE': 'ru',
        'SUPPORT_QUIET_HOURS': '23:00-09:00',
        'SUPPORT_TICKET_SLA_DIGEST_ENABLED': True,
        'SUPPORT_MORNING_DIGEST_ENABLED': False,
        'SUPPORT_TICKET_SLA_DIGEST_INTERVAL_MINUTES': 180,
        'SUPPORT_TICKET_SLA_ENABLED': True,
    }.items():
        monkeypatch.setattr(digest.settings, name, value)
    redis = RedisMemory()
    load = AsyncMock(return_value=([ticket(i) for i in range(1, 8)], 7))
    send = AsyncMock(return_value=True)
    answer = AsyncMock(return_value=api.AiAnswer(api.AiOutcome.ERROR))
    monkeypatch.setattr(digest, 'cache', redis)
    monkeypatch.setattr(digest, '_load_tickets', load)
    monkeypatch.setattr(digest.AdminNotificationService, 'send_ticket_event_notification', send)
    monkeypatch.setattr(api, 'is_enabled', lambda: True)
    monkeypatch.setattr(api, 'answer', answer)
    return SimpleNamespace(redis=redis, load=load, send=send, answer=answer, bot=SimpleNamespace(id=123))


async def cycle(env, now=DAY):
    env.redis.now = now
    return await digest.send_ticket_digests(SimpleNamespace(), env.bot, now, 60, 30)


async def test_one_message_for_seven_tickets(env):
    assert await cycle(env)
    env.send.assert_awaited_once()
    text = env.send.call_args.args[0]
    assert 'Тикеты без ответа: 7' in text
    assert text.count('• #') == 7
    env.load.assert_awaited_once_with(SimpleNamespace(), DAY, 60, 30, morning=False)


async def test_interval_survives_new_monitoring_instance(env, monkeypatch):
    monkeypatch.setattr(type(digest.settings), 'is_admin_notifications_enabled', lambda self: True)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.redis.now

    monkeypatch.setattr(monitoring, 'datetime', Clock)
    await cycle(env)
    for minute in (1, 60, 179):
        env.redis.now = DAY + timedelta(minutes=minute)
        # Fresh instance, no instance/local state from the previous process.
        service = object.__new__(MonitoringService)
        service.bot = env.bot
        await service._check_ticket_sla(SimpleNamespace())
    assert env.send.await_count == 1
    await cycle(env, DAY + timedelta(minutes=180))
    assert env.send.await_count == 2


@pytest.mark.parametrize('now', [NIGHT, DAY.replace(hour=20), DAY.replace(hour=5, minute=59)])
async def test_quiet_hours_skip_all_digests(env, monkeypatch, now):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    assert await cycle(env, now)
    env.load.assert_not_awaited()
    env.send.assert_not_awaited()
    assert not env.redis.values


async def test_morning_once_and_replaces_regular_digest(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    await cycle(env, MORNING)
    assert 'Утренняя сводка' in env.send.call_args.args[0]
    assert env.load.call_args.kwargs['morning'] is True
    await cycle(env, MORNING + timedelta(minutes=1))
    assert env.send.await_count == 1
    await cycle(env, MORNING + timedelta(hours=3))
    assert 'Тикеты без ответа' in env.send.call_args.args[0]
    await cycle(env, MORNING + timedelta(days=1))
    assert env.send.await_count == 3
    assert 'Утренняя сводка' in env.send.call_args.args[0]


async def test_interval_counts_from_delivery_after_slow_telegram(env):
    async def send(text):
        env.redis.now += timedelta(seconds=30)
        return True

    env.send.side_effect = send
    await cycle(env)
    await cycle(env, DAY + timedelta(minutes=180))
    assert env.send.await_count == 1
    await cycle(env, DAY + timedelta(minutes=180, seconds=30))
    assert env.send.await_count == 2


async def test_morning_overrides_unexpired_sla_interval(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    await env.redis.set('support:ticket_digest:123:interval', 1, expire=86400)
    await cycle(env, MORNING)
    env.send.assert_awaited_once()
    assert 'Утренняя сводка' in env.send.call_args.args[0]


async def test_morning_catches_up_after_restart_later_in_day(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    await cycle(env, DAY)
    await cycle(env, DAY + timedelta(minutes=1))
    env.send.assert_awaited_once()
    assert 'Утренняя сводка' in env.send.call_args.args[0]


async def test_both_switches_off_leave_legacy_policy(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_TICKET_SLA_DIGEST_ENABLED', False)
    assert not await cycle(env)
    env.load.assert_not_awaited()
    env.send.assert_not_awaited()


async def test_morning_only_then_legacy_policy(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_TICKET_SLA_DIGEST_ENABLED', False)
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    assert await cycle(env, MORNING)
    assert not await cycle(env, MORNING + timedelta(minutes=1))
    env.send.assert_awaited_once()


async def test_no_quiet_window_means_no_morning_digest(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    monkeypatch.setattr(digest.settings, 'SUPPORT_QUIET_HOURS', '')
    await cycle(env)
    assert 'Тикеты без ответа' in env.send.call_args.args[0]


async def test_timezone_is_respected(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'TIMEZONE', 'UTC')
    assert await cycle(env, MORNING)
    env.send.assert_not_awaited()
    await cycle(env, DAY)
    env.send.assert_awaited_once()


async def test_empty_morning_processed_once(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    monkeypatch.setattr(digest.settings, 'SUPPORT_TICKET_SLA_DIGEST_ENABLED', False)
    env.load.return_value = ([], 0)
    await cycle(env, MORNING)
    assert not await cycle(env, MORNING + timedelta(minutes=1))
    env.load.assert_awaited_once()
    env.send.assert_not_awaited()


@pytest.mark.parametrize('morning', [False, True])
async def test_delivery_failure_retries_next_cycle(env, monkeypatch, morning):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', morning)
    env.send.return_value = False
    await cycle(env)
    env.send.return_value = True
    await cycle(env)
    assert env.send.await_count == 2
    assert not any(key.endswith(':lock') for key in env.redis.values)


async def test_concurrent_cycles_do_not_duplicate(env):
    assert all(await asyncio.gather(cycle(env), cycle(env)))
    env.send.assert_awaited_once()


async def test_redis_unavailable_fails_closed(env, monkeypatch):
    monkeypatch.setattr(env.redis, 'setnx', AsyncMock(return_value=False))
    assert await cycle(env)
    env.load.assert_not_awaited()
    env.send.assert_not_awaited()


async def test_morning_send_exception_releases_reservations(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    env.send.side_effect = RuntimeError('telegram unavailable')
    with pytest.raises(RuntimeError, match='telegram unavailable'):
        await cycle(env)
    assert not env.redis.values
    env.send.side_effect = None
    await cycle(env)
    assert env.send.await_count == 2


async def test_quiet_hours_rechecked_after_slow_ai(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    monkeypatch.setattr(digest, '_summaries', AsyncMock(return_value={}))
    ticks = iter([0, 2])
    monkeypatch.setattr(digest, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    await cycle(env, DAY.replace(hour=19, minute=59, second=59))
    env.send.assert_not_awaited()
    assert not env.redis.values


async def test_ai_failure_falls_back_to_redacted_client_text(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    env.load.return_value = ([ticket(last_client='Оплатил, связь с user@example.org +7 999 123 45 67')], 1)
    env.answer.side_effect = RuntimeError('provider unavailable')
    await cycle(env)
    text = env.send.call_args.args[0]
    assert 'Оплатил' in text and '[почта]' in text and '[телефон]' in text
    assert 'user@example.org' not in text and '999' not in text


async def test_journal_summary_has_priority_and_is_redacted(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    env.load.return_value = ([ticket(summary='Не подключается. Перезапуск проверен. user@example.org')], 1)
    await cycle(env)
    assert 'Перезапуск проверен' in env.send.call_args.args[0]
    assert 'user@example.org' not in env.send.call_args.args[0]
    env.answer.assert_not_awaited()


async def test_ai_request_is_bounded_and_does_not_bind_ticket_or_user(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    env.load.return_value = ([ticket(i, conversation='abc ' * 5000) for i in range(30)], 30)
    env.answer.return_value = api.AiAnswer(
        api.AiOutcome.ANSWERED, reply_text='Клиент хочет подключиться. Шаги проверены.'
    )
    await cycle(env)
    assert env.answer.await_count == 3
    for call in env.answer.call_args_list:
        question, history, ctx = call.args
        assert len(question) < 2000 and not history
        assert ctx.channel == api.AiChannel.ADMIN_TEST
        assert ctx.user_id is None and ctx.ticket_id is None and not ctx.allow_account_tools
    assert 'Шаги проверены' in env.send.call_args.args[0]


async def test_ai_timeout_falls_back(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    monkeypatch.setattr(digest, 'AI_TIMEOUT_SECONDS', 0.01)
    env.load.return_value = ([ticket(last_client='Последние слова клиента')], 1)

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    env.answer.side_effect = hang
    await cycle(env)
    assert 'Последние слова клиента' in env.send.call_args.args[0]


async def test_disabled_ai_uses_client_text_without_call(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_MORNING_DIGEST_ENABLED', True)
    monkeypatch.setattr(api, 'is_enabled', lambda: False)
    env.load.return_value = ([ticket(last_client='Соединение не установлено')], 1)
    await cycle(env)
    assert 'Соединение не установлено' in env.send.call_args.args[0]
    env.answer.assert_not_awaited()


def test_render_caps_30_rows_and_adds_remaining_count(env):
    text = digest._render([ticket(i) for i in range(40)], 40, DAY, None)
    assert text.count('• #') == 30
    assert 'и ещё 10' in text


def test_render_stays_one_telegram_message_with_whole_escaped_rows(env):
    tickets = [ticket(i, title='<script> & ' * 20) for i in range(30)]
    text = digest._render(tickets, 100, DAY, dict.fromkeys(range(30), '<&' * 110))
    assert len(text) < 4096
    assert '<script>' not in text
    assert 'и ещё ' in text and '&lt;' in text
    assert len(html.unescape(text)) < 4096


@pytest.mark.parametrize('morning', [False, True])
async def test_selection_keeps_legacy_sla_filters_but_morning_lists_all_open(env, monkeypatch, morning):
    queries = []

    async def execute(query):
        queries.append(str(query))
        return SimpleNamespace(all=list)

    db = SimpleNamespace(execute=execute, commit=AsyncMock())
    assert await LOAD_TICKETS(db, DAY, 60, 30, morning=morning) == ([], 0)
    sql = queries[0]
    assert 'tickets.status =' in sql
    assert 'LIMIT' in sql and 'count(*) OVER ()' in sql
    assert ('tickets.updated_at <=' in sql) is (not morning)
    assert ('tickets.last_sla_reminder_at IS NULL' in sql) is (not morning)
    assert 'ai_support_requests.handoff_done_at IS NOT NULL' in sql
    assert 'ticket_messages.is_from_admin IS false' in sql
    db.commit.assert_awaited_once()


async def test_loading_snapshots_bounded_history_and_releases_transaction(env):
    rows = [(i, f'VPN {i}', DAY, None if i < 5 else 'Уже проверено', 'Текст клиента', 100) for i in range(1, 8)]
    queries = []

    async def execute(query):
        queries.append(str(query))
        if len(queries) == 1:
            return SimpleNamespace(all=lambda: rows)
        return SimpleNamespace(all=lambda: [(False, 'Клиент user@example.org'), (True, 'Перезапуск проверен')])

    db = SimpleNamespace(execute=execute, commit=AsyncMock())
    tickets, total = await LOAD_TICKETS(db, DAY, 60, 30, morning=True)
    assert total == 100 and len(tickets) == 7
    assert len(queries) == 4  # main snapshot + only three candidates for AI
    assert all('LIMIT' in query for query in queries)
    assert 'Перезапуск проверен' in tickets[0].conversation
    assert '[почта]' in tickets[0].conversation and 'user@example.org' not in tickets[0].conversation
    assert tickets[3].conversation == ''
    db.commit.assert_awaited_once()


@pytest.mark.parametrize('guard', ['sla', 'admin', 'bot'])
async def test_monitoring_guards_prevent_digests(env, monkeypatch, guard):
    monkeypatch.setattr(SupportSettingsService, 'get_sla_enabled', lambda: guard != 'sla')
    monkeypatch.setattr(type(digest.settings), 'is_admin_notifications_enabled', lambda self: guard != 'admin')
    service = object.__new__(MonitoringService)
    service.bot = None if guard == 'bot' else env.bot
    await service._check_ticket_sla(SimpleNamespace())
    env.load.assert_not_awaited()
    env.send.assert_not_awaited()


async def test_monitoring_legacy_path_remains_per_ticket(env, monkeypatch):
    monkeypatch.setattr(digest.settings, 'SUPPORT_TICKET_SLA_DIGEST_ENABLED', False)
    monkeypatch.setattr(type(digest.settings), 'is_admin_notifications_enabled', lambda self: True)
    tickets = [
        SimpleNamespace(id=i, title='VPN', user=None, updated_at=DAY, last_sla_reminder_at=None) for i in range(7)
    ]
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: tickets))),
        commit=AsyncMock(),
    )
    service = object.__new__(MonitoringService)
    service.bot = env.bot
    service._log_monitoring_event = AsyncMock()
    await service._check_ticket_sla(db)
    assert env.send.await_count == 7
    assert all(t.last_sla_reminder_at for t in tickets)
    env.load.assert_not_awaited()
