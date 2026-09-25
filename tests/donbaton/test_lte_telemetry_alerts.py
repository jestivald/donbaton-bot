"""Owner alerts about the LTE gate: one message per change, never a broken cycle."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import lte_quota_service, lte_telemetry_alerts
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory
from app.services.lte_quota_service import TelemetryEvidence
from app.utils.cache import cache


FI = '85950bae-4671-409c-b090-bbc2adf6e7b3'
DE = '97181e55-43ee-47c6-b45e-6ce188e57309'
PL = '516dd304-9a41-4507-8545-a46f92f6fe69'
T0 = datetime(2026, 9, 25, 12, tzinfo=UTC)
BOT = object()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ROUTE_ALERT_DELAY_SECONDS', 300)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ROUTE_ALERT_REMIND_SECONDS', 21600)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500')
    monkeypatch.setattr(lte_telemetry_alerts, '_memory', {})


@pytest.fixture(autouse=True)
def redis_store(monkeypatch):
    store = {}

    async def read(key):
        return store.get(key)

    async def write(key, value, ex=None, **_options):
        store[key] = value
        store[f'{key}!ttl'] = ex
        return True

    monkeypatch.setattr(cache, '_connected', True)
    monkeypatch.setattr(cache, 'redis_client', AsyncMock())
    cache.redis_client.get.side_effect = read
    cache.redis_client.set.side_effect = write
    return store


@pytest.fixture
def send(monkeypatch):
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(AdminNotificationService, 'send_admin_notification', sender)
    return sender


def evidence(state='live', proven=(FI, DE), failed=None, remaining=None, missing=None):
    configured = {FI, DE}
    missing = tuple(sorted(configured - set(proven))) if missing is None else missing
    return TelemetryEvidence(
        state=state,
        proven=tuple(sorted(proven)),
        missing=missing,
        failed=dict(failed or {}),
        grace_remaining=remaining,
    )


async def cycle(at, current, bot=BOT):
    await lte_telemetry_alerts.report_cycle(current, bot=bot, now=at)


def texts(send):
    return [call.args[0] for call in send.await_args_list]


def restart():
    lte_telemetry_alerts._memory.clear()


@pytest.mark.asyncio
async def test_a_healthy_gate_never_messages(send):
    for minute in range(0, 60, 5):
        await cycle(T0 + timedelta(minutes=minute), evidence())
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_route_is_reported_after_the_delay_exactly_once(send):
    down = evidence(proven=(DE,), failed={FI: 'unreachable'})
    for seconds in (0, 30, 299):
        await cycle(T0 + timedelta(seconds=seconds), down)
    send.assert_not_awaited()
    await cycle(T0 + timedelta(seconds=300), down)
    send.assert_awaited_once()
    text = texts(send)[0]
    assert 'Финляндия (spb-fra)' in text
    assert '<code>unreachable</code>' in text
    assert 'подтверждены Германия (cdn-de1)' in text
    assert 'Учёт под вопросом' not in text
    assert send.await_args.kwargs == {'category': NotificationCategory.INFRASTRUCTURE}
    for seconds in (330, 600, 3600):
        await cycle(T0 + timedelta(seconds=seconds), down)
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_blip_shorter_than_the_delay_stays_silent(send):
    await cycle(T0, evidence(proven=(DE,), failed={FI: 'unreachable'}))
    await cycle(T0 + timedelta(seconds=200), evidence())
    await cycle(T0 + timedelta(seconds=260), evidence(proven=(DE,), failed={FI: 'unreachable'}))
    await cycle(T0 + timedelta(seconds=500), evidence(proven=(DE,), failed={FI: 'unreachable'}))
    send.assert_not_awaited()  # the second outage started at 260 s: 240 s is not yet the delay
    await cycle(T0 + timedelta(seconds=560), evidence(proven=(DE,), failed={FI: 'unreachable'}))
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_is_its_own_single_message(send):
    await cycle(T0, evidence(proven=(DE,), failed={FI: 'unreachable'}))
    await cycle(T0 + timedelta(seconds=300), evidence(proven=(DE,), failed={FI: 'unreachable'}))
    await cycle(T0 + timedelta(minutes=40), evidence())
    await cycle(T0 + timedelta(minutes=41), evidence())
    assert len(texts(send)) == 2
    assert 'Финляндия (spb-fra) — снова подтверждён (не было 40 мин)' in texts(send)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['not_persisted', 'multiplier', 'wrong_node'])
async def test_accounting_codes_ask_to_check_the_node(send, code):
    await cycle(T0, evidence(proven=(FI,), failed={DE: code}))
    await cycle(T0 + timedelta(seconds=300), evidence(proven=(FI,), failed={DE: code}))
    assert 'Учёт под вопросом — проверьте ноду в панели' in texts(send)[0]


@pytest.mark.asyncio
async def test_several_changes_in_one_cycle_make_one_message(send):
    both = evidence(state='grace', proven=(), remaining=600)
    await cycle(T0, both)
    await cycle(T0 + timedelta(seconds=300), both)
    send.assert_awaited_once()
    text = texts(send)[0]
    assert text.startswith('🟠 <b>LTE держится на grace</b>')
    assert 'осталось 10 мин' in text
    assert 'Финляндия (spb-fra)' in text and 'Германия (cdn-de1)' in text
    assert '<code>no_lease</code>' in text


@pytest.mark.asyncio
async def test_gate_goes_grace_then_closed_then_returned(send):
    await cycle(T0, evidence())
    grace = evidence(state='grace', proven=(), remaining=900)
    await cycle(T0 + timedelta(minutes=1), grace)
    await cycle(T0 + timedelta(minutes=3), evidence(state='grace', proven=(), remaining=780))
    send.assert_not_awaited()  # a canary restart must not page anyone
    await cycle(T0 + timedelta(minutes=6), evidence(state='grace', proven=(), remaining=600))
    await cycle(T0 + timedelta(minutes=16), evidence(state='closed', proven=()))
    await cycle(T0 + timedelta(minutes=17), evidence(state='closed', proven=()))
    await cycle(T0 + timedelta(minutes=20), evidence())
    await cycle(T0 + timedelta(minutes=21), evidence())
    titles = [text.splitlines()[0] for text in texts(send)]
    assert titles == [
        '🟠 <b>LTE держится на grace</b>',
        '🔴 <b>LTE снят у всех</b>',
        '✅ <b>LTE возвращён</b>',
    ]
    assert 'бот снимает LTE-сквад со всех подписок' in texts(send)[1]
    assert 'Финляндия (spb-fra), Германия (cdn-de1)' in texts(send)[2]
    assert 'снова подтверждён' in texts(send)[2]  # the routes' recovery rides in the same message


@pytest.mark.asyncio
async def test_grace_that_ends_before_the_delay_is_silent_and_after_it_is_reported(send):
    await cycle(T0, evidence(state='grace', proven=(), remaining=900))
    await cycle(T0 + timedelta(seconds=120), evidence())
    send.assert_not_awaited()
    await cycle(T0 + timedelta(minutes=10), evidence(state='grace', proven=(), remaining=900))
    await cycle(T0 + timedelta(minutes=15), evidence(state='grace', proven=(), remaining=600))
    await cycle(T0 + timedelta(minutes=16), evidence())
    titles = [text.splitlines()[0] for text in texts(send)]
    assert titles == ['🟠 <b>LTE держится на grace</b>', '✅ <b>LTE: подтверждение вернулось</b>']
    assert 'LTE не снимался' in texts(send)[1]


@pytest.mark.asyncio
async def test_first_sight_of_a_closed_gate_is_reported_but_a_healthy_one_is_not(send):
    await cycle(T0, evidence(state='closed', proven=()))
    assert texts(send)[0].startswith('🔴 <b>LTE снят у всех</b>')


@pytest.mark.asyncio
async def test_strict_mode_says_grace_is_off(send, monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 0)
    await cycle(T0, evidence(state='closed', proven=()))
    assert '(grace выключен)' in texts(send)[0]


@pytest.mark.asyncio
async def test_reminder_repeats_while_degraded_and_stops_after_recovery(send):
    down = evidence(proven=(DE,), failed={FI: 'unreachable'})
    await cycle(T0, down)
    await cycle(T0 + timedelta(seconds=300), down)
    await cycle(T0 + timedelta(hours=6), down)
    assert len(texts(send)) == 1  # six hours since the alert, not since the outage
    await cycle(T0 + timedelta(hours=6, seconds=300), down)
    assert len(texts(send)) == 2
    reminder = texts(send)[1]
    assert reminder.startswith('🔁 <b>LTE: напоминание</b>')
    assert 'нет подтверждения 6 ч' in reminder
    await cycle(T0 + timedelta(hours=7), down)
    await cycle(T0 + timedelta(hours=8), evidence())
    await cycle(T0 + timedelta(hours=20), evidence())
    assert [text.splitlines()[0] for text in texts(send)][2:] == ['✅ <b>LTE: обход снова подтверждён</b>']


@pytest.mark.asyncio
async def test_zero_disables_the_reminder(send, monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ROUTE_ALERT_REMIND_SECONDS', 0)
    down = evidence(proven=(DE,), failed={FI: 'unreachable'})
    await cycle(T0, down)
    await cycle(T0 + timedelta(seconds=300), down)
    await cycle(T0 + timedelta(days=3), down)
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_state_survives_a_restart(send, redis_store):
    down = evidence(proven=(DE,), failed={FI: 'unreachable'})
    await cycle(T0, down)
    await cycle(T0 + timedelta(seconds=300), down)
    stored = json.loads(redis_store[lte_telemetry_alerts.state_key(settings.DONBATON_LTE_ACCOUNTING_EPOCH)])
    assert stored['routes'][FI]['announced'] is True
    assert redis_store[f'{lte_telemetry_alerts.state_key(settings.DONBATON_LTE_ACCOUNTING_EPOCH)}!ttl'] > 0
    restart()
    await cycle(T0 + timedelta(minutes=10), down)
    send.assert_awaited_once()  # the new process knows the alert was already sent
    restart()
    await cycle(T0 + timedelta(minutes=20), evidence())
    assert 'не было 20 мин' in texts(send)[1]  # and when the outage started


@pytest.mark.asyncio
async def test_the_last_known_code_outlives_the_lease(send):
    await cycle(T0, evidence(proven=(DE,), failed={FI: 'wrong_node'}))
    await cycle(T0 + timedelta(seconds=300), evidence(state='grace', proven=(), remaining=900))
    text = texts(send)[0]
    assert 'Финляндия (spb-fra) — нет подтверждения 5 мин: трафик записан на другую ноду' in text
    assert 'Германия' not in text  # its outage is only starting


@pytest.mark.asyncio
async def test_telegram_failure_never_breaks_the_cycle_and_is_retried(send):
    send.side_effect = RuntimeError('telegram is down')
    down = evidence(proven=(DE,), failed={FI: 'unreachable'})
    await cycle(T0, down)
    await cycle(T0 + timedelta(seconds=300), down)
    send.reset_mock(side_effect=True)
    send.return_value = False  # delivered nowhere: still pending
    await cycle(T0 + timedelta(seconds=330), down)
    send.return_value = True
    await cycle(T0 + timedelta(seconds=360), down)
    await cycle(T0 + timedelta(seconds=390), down)
    assert len(texts(send)) == 2  # retried until delivered, then silent
    assert 'нет подтверждения 6 мин' in texts(send)[-1]  # the message describes the cycle that sent it


@pytest.mark.asyncio
async def test_a_hung_telegram_call_is_bounded(send, monkeypatch):
    monkeypatch.setattr(lte_telemetry_alerts, 'SEND_TIMEOUT_SECONDS', 0.05)

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    send.side_effect = hang
    await asyncio.wait_for(cycle(T0, evidence(state='closed', proven=())), 2)
    send.side_effect = None
    await cycle(T0 + timedelta(seconds=30), evidence(state='closed', proven=()))
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_a_broken_state_store_never_breaks_the_cycle(send):
    cache.redis_client.get.side_effect = RuntimeError('redis is down')
    cache.redis_client.set.side_effect = RuntimeError('redis is down')
    await cycle(T0, evidence(state='closed', proven=()))
    await cycle(T0 + timedelta(seconds=30), evidence(state='closed', proven=()))
    send.assert_awaited_once()  # the process remembers what it said without Redis


@pytest.mark.asyncio
async def test_no_bot_or_disabled_notifications_only_log(send, monkeypatch):
    await cycle(T0, evidence(state='closed', proven=()), bot=None)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_ENABLED', False)
    await cycle(T0 + timedelta(minutes=5), evidence())
    await cycle(T0 + timedelta(minutes=6), evidence(state='closed', proven=()))
    send.assert_not_awaited()
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_ENABLED', True)
    await cycle(T0 + timedelta(minutes=7), evidence(state='closed', proven=()))
    send.assert_not_awaited()  # nothing new to say, and no backlog is replayed


@pytest.mark.asyncio
async def test_disabled_lte_accounting_is_silent(send, monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', False)
    await cycle(T0, evidence(state='closed', proven=()))
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_nodes_are_named_by_prefix_and_a_removed_route_is_forgotten(send):
    unknown = '00000000-0000-4000-8000-000000000099'
    down = TelemetryEvidence(state='live', proven=(FI,), missing=(unknown,), failed={unknown: 'unmonitored'})
    await cycle(T0, down)
    await cycle(T0 + timedelta(seconds=300), down)
    assert 'нода 00000000 — нет подтверждения 5 мин: канарейка этот маршрут не проверяет' in texts(send)[0]
    await cycle(T0 + timedelta(seconds=330), TelemetryEvidence(state='live', proven=(FI,)))
    await cycle(T0 + timedelta(hours=7), TelemetryEvidence(state='live', proven=(FI,)))
    send.assert_awaited_once()  # taken out of the configuration: no recovery, no reminder


@pytest.mark.asyncio
async def test_monitoring_loop_hands_its_bot_to_the_quota_cycle(monkeypatch):
    from app.services.monitoring_service import MonitoringService

    calls = []

    async def quota_cycle(service, bot=None):
        calls.append((service, bot))
        raise asyncio.CancelledError

    monkeypatch.setattr(lte_quota_service, 'run_quota_cycle', quota_cycle)
    monitoring = MonitoringService(bot=BOT)
    monitoring.is_running = True
    await monitoring._lte_loop()
    assert calls == [(monitoring.subscription_service, BOT)]
