"""Гейт канарейки выключен (решение владельца 28.09.2026): LTE не ждёт lease.

Доступ к LTE решает только леджер: подписка действует, учёт ``ready``, пакет не
исчерпан — как при живой канарейке. Lease, якорь grace и состояние оповещений
в Redis не читаются и не пишутся, владельцу ничего не приходит, уведомления
клиентам об остатке уходят. Включённый флаг возвращает прежний гейт: его
по-прежнему проверяют остальные тесты (``conftest.py`` включает флаг явно).
"""

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from structlog.testing import capture_logs

from app.config import Settings, settings
from app.database.models import User
from app.handlers.subscription.lte import format_lte_status
from app.services import lte_quota_service, lte_telemetry_alerts
from app.services.admin_notification_service import AdminNotificationService
from app.services.lte_quota_service import (
    GIB,
    LtePolicyError,
    TelemetryEvidence,
    _read_telemetry_heartbeat as production_reader,
    _resolve_telemetry_evidence,
    apply_panel_policy,
    get_quota_status,
    reconcile_subscription,
    run_quota_cycle,
    validate_tariff_for_sale,
)
from app.utils.cache import cache
from tests.donbaton.conftest import TEST_LTE_NODES
from tests.donbaton.test_lte_legacy import TABLES as CYCLE_TABLES
from tests.donbaton.test_lte_quota import LTE, NOW, TABLES, WIFI, sample, seed, tariff
from tests.donbaton.test_lte_quota_notices import (
    TABLES as NOTICE_TABLES,
    api_for,
    customer,
    events,
    panel,
)
from tests.fixtures.sqlite_memory import memory_session


ENV_EXAMPLE = Path(__file__).resolve().parents[2] / '.env.example'
DISABLED = 'LTE telemetry gate disabled, canary lease is not consulted'
GATE_LOGS = ('LTE telemetry missing, within grace', 'LTE telemetry missing beyond grace, LTE closed')


@pytest.fixture(autouse=True)
def gate_off(monkeypatch, synthetic_telemetry):
    """Как в бою с 28.09: флаг выключен, канарейки нет, в Redis ни lease, ни якоря."""
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GATE_ENABLED', False)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GRACE_SECONDS', 900)
    # The real reader: were the lease consulted, it would reach the stub below.
    monkeypatch.setattr(lte_quota_service, '_read_telemetry_heartbeat', production_reader)
    monkeypatch.setattr(lte_quota_service, '_REPORTED_TELEMETRY_CONFIG', set())
    monkeypatch.setattr(lte_telemetry_alerts, '_memory', {})
    monkeypatch.setattr(cache, '_connected', True)
    monkeypatch.setattr(cache, 'redis_client', AsyncMock())
    cache.redis_client.get.return_value = None


def telemetry_keys_touched():
    """Every Redis key of the canary contract the code read or wrote."""
    calls = [*cache.redis_client.get.await_args_list, *cache.redis_client.set.await_args_list]
    return [call.args[0] for call in calls if call.args and str(call.args[0]).startswith('donbaton:lte:telemetry')]


def gate_logs(logs):
    return [log for log in logs if log['event'] in GATE_LOGS]


@pytest.mark.asyncio
async def test_active_customer_gets_lte_without_any_lease(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        with capture_logs() as logs:
            payload = await apply_panel_policy(db, sub, {}, panel_user=sample(1000), now=NOW)
            status = await get_quota_status(db, sub, now=NOW)
    assert payload['active_internal_squads'] == [WIFI, LTE]
    assert status['accounting_state'] == 'ready'
    assert 'после проверки счётчика' not in format_lte_status('wifi_lte', status)
    assert telemetry_keys_touched() == []
    assert gate_logs(logs) == []


@pytest.mark.asyncio
async def test_the_ledger_still_decides_unready_and_exhausted_stay_wifi_only(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        # No sample yet: accounting is `pending`, so no LTE, with or without the gate.
        pending = await apply_panel_policy(db, sub, {}, now=NOW)
        assert pending['active_internal_squads'] == [WIFI]
        opened = await apply_panel_policy(db, sub, {}, panel_user=sample(1000), now=NOW)
        assert opened['active_internal_squads'] == [WIFI, LTE]
        exhausted = await apply_panel_policy(db, sub, {}, panel_user=sample(1100), now=NOW + timedelta(seconds=30))
        assert exhausted['active_internal_squads'] == [WIFI]
        assert sub._lte_quota_status['exhausted']
    assert telemetry_keys_touched() == []


@pytest.mark.asyncio
async def test_the_same_ledger_with_the_gate_on_and_no_lease_is_wifi_only(monkeypatch):
    """The flag is the only difference: on, the old fail-closed gate is back."""
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GATE_ENABLED', True)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(1000), now=NOW)
        assert payload['active_internal_squads'] == [WIFI]
        assert sub._lte_quota_status['accounting_state'] == 'telemetry_unavailable'
    assert telemetry_keys_touched()  # the gate did look for a lease


@pytest.mark.asyncio
async def test_cycle_evidence_is_disabled_nothing_is_read_and_it_is_said_once():
    with capture_logs() as logs:
        first = await _resolve_telemetry_evidence(now=NOW)
        second = await _resolve_telemetry_evidence(now=NOW + timedelta(seconds=30))
    assert first.state == second.state == 'disabled'
    assert (first.payload, first.last_valid) == (None, None)
    assert telemetry_keys_touched() == []
    assert [log['event'] for log in logs] == [DISABLED]


@pytest.mark.asyncio
async def test_owner_is_never_paged_and_alert_state_is_left_alone(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500')
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(AdminNotificationService, 'send_admin_notification', send)
    # Evidence that pages the owner at once with the gate on stays silent with it off.
    closed = TelemetryEvidence(
        state='closed',
        missing=tuple(TEST_LTE_NODES),
        failed=dict.fromkeys(TEST_LTE_NODES, 'unreachable'),
    )
    for minutes in (0, 10, 400):
        at = NOW + timedelta(minutes=minutes)
        await lte_telemetry_alerts.report_cycle(closed, bot=object(), now=at)
        await lte_telemetry_alerts.report_cycle(await _resolve_telemetry_evidence(now=at), bot=object(), now=at)
    send.assert_not_awaited()
    assert telemetry_keys_touched() == []
    assert lte_telemetry_alerts._memory == {}
    # Not a vacuous check: the very same evidence does page with the gate on.
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GATE_ENABLED', True)
    await lte_telemetry_alerts.report_cycle(closed, bot=object(), now=NOW)
    send.assert_awaited_once()
    assert 'LTE снят у всех' in send.await_args.args[0]


@pytest.mark.asyncio
async def test_quota_cycle_opens_lte_without_a_canary_and_pages_nobody(monkeypatch):
    from app.database import database

    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500')
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(AdminNotificationService, 'send_admin_notification', send)
    async with memory_session(monkeypatch, CYCLE_TABLES) as db:
        await seed(db)
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(database, 'AsyncSessionLocal', maker)
        api = SimpleNamespace(get_user_by_id=AsyncMock(return_value=sample(0)), update_user=AsyncMock())

        @asynccontextmanager
        async def client():
            yield api

        service = SimpleNamespace(is_configured=True, get_api_client=client)
        with capture_logs() as logs:
            await run_quota_cycle(service, bot=object())
            await run_quota_cycle(service, bot=object())
    assert api.update_user.await_args_list[0].kwargs['active_internal_squads'] == [WIFI, LTE]
    assert gate_logs(logs) == []
    send.assert_not_awaited()
    assert telemetry_keys_touched() == []


@pytest.mark.asyncio
async def test_customer_balance_notices_still_go_out(monkeypatch):
    for key in ('DONBATON_LTE_NOTICE_ENABLED', 'DONBATON_LTE_LOW_NOTICE_GB', 'DONBATON_LTE_EXHAUSTED_NOTICE_ENABLED'):
        monkeypatch.setattr(settings, key, Settings.model_fields[key].default)
    async with memory_session(monkeypatch, NOTICE_TABLES) as db:
        sub, quota, _ = await customer(db, limit_bytes=2 * GIB)
        user = await db.get(User, sub.user_id)
        evidence = await _resolve_telemetry_evidence()  # what the cycle hands over now
        notices = []
        for lifetime in (0, 2 * GIB - GIB // 2, 2 * GIB):
            await reconcile_subscription(
                db, sub, api_for(panel(lifetime)), user=user, heartbeat=evidence, notices=notices
            )
            await db.refresh(quota)
        assert events(notices) == ['low', 'exhausted']
        assert sub._lte_quota_status['accounting_state'] == 'ready'
        assert quota.exhausted_notice_sent_at is not None
    assert lte_quota_service._lease_is_live(lte_quota_service._UNSET_HEARTBEAT, datetime.now(UTC))
    assert telemetry_keys_touched() == []


def test_selling_lte_does_not_need_canary_nodes(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_NODE_UUIDS', '')
    validate_tariff_for_sale(tariff())
    monkeypatch.setattr(settings, 'DONBATON_LTE_TELEMETRY_GATE_ENABLED', True)
    with pytest.raises(LtePolicyError, match='хотя бы одной'):
        validate_tariff_for_sale(tariff())


def test_gate_is_off_by_default_and_env_example_does_not_pin_it():
    """Ключ в .env перекрывает админку молча, поэтому в примере он закомментирован."""
    assert Settings.model_fields['DONBATON_LTE_TELEMETRY_GATE_ENABLED'].default is False
    lines = [line.strip() for line in ENV_EXAMPLE.read_text(encoding='utf-8').splitlines()]
    assert '# DONBATON_LTE_TELEMETRY_GATE_ENABLED=false' in lines
    assert [line for line in lines if re.match(r'^DONBATON_LTE_TELEMETRY_GATE_ENABLED\s*=', line)] == []
