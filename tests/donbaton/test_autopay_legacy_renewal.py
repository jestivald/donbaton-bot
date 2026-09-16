"""Автопродление не оплачивает перенесённые индивидуальные условия.

Инцидент на проде: фоновый цикл `_process_autopayments` каждый час звал
`calculate_renewal_price` по подписке с перенесёнными индивидуальными
условиями, ловил `LtePolicyError` общим `except Exception`, писал ERROR и рос
`failed_count` — примерно два десятка ложных ошибок в сутки на подписку.
Хуже того, в двухчасовом окне «недавно истёкших» guard замолкал (строгое `<`),
и autopay мог молча списать деньги за технический тариф импорта, который
клиент не выбирал.

Политика владельца: клиент дорабатывает оплаченный срок на прежних условиях,
потом САМ выбирает тариф. Докупка LTE при этом остаётся доступна.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Base, LteQuota, Subscription, SubscriptionStatus, Tariff, User
from app.services.lte_quota_service import (
    credit_topup,
    legacy_autopay_blocked,
    legacy_renewal_requires_expiry,
)
from app.services.pricing_engine import PricingEngine
from tests.donbaton.test_lte_legacy import EPOCH, activate, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI
from tests.fixtures.sqlite_memory import memory_session


# Цикл автоплатежей пишет MonitoringLog и Transaction, поэтому берём всю схему,
# а не точечный список таблиц LTE-тестов.
TABLES = list(Base.metadata.sorted_tables)
BALANCE = 500_000


@pytest.fixture(autouse=True)
def accounting_config(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_PERIOD_DAYS', 0)
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    # Промо-связи к этим синтетическим пользователям отношения не имеют.
    monkeypatch.setattr(PricingEngine, 'resolve_promo_group', staticmethod(lambda user: None))


class _Recorder:
    """Пишущий логгер: тест смотрит, каким уровнем цикл отчитался о подписке."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __getattr__(self, level):
        def log(event='', *args, **kwargs):
            self.calls.append((level, str(event)))

        return log

    def events(self, level: str) -> list[str]:
        return [event for name, event in self.calls if name == level]


class _MemoryCache:
    """Дедуп уведомлений в проде живёт в Redis; в тесте достаточно словаря."""

    def __init__(self):
        self.store: dict[str, object] = {}

    async def exists(self, key):
        return key in self.store

    async def set(self, key, value, expire=None):
        self.store[key] = value
        return True


class _FakePanelSync:
    async def update_remnawave_user(self, db, subscription, **kwargs):
        return SimpleNamespace(id=11, used_traffic_bytes=0)

    async def create_remnawave_user(self, db, subscription, **kwargs):
        return SimpleNamespace(id=11, used_traffic_bytes=0)


async def _seed_legacy(db, *, custom, paid_until, status='active', autopay=True):
    sub, target, snapshot, _ = await legacy_seed(db, custom=custom, paid_until=paid_until, status=status, devices=15)
    quota = await activate(db, sub, target, snapshot)
    user = await db.get(User, sub.user_id)
    user.balance_kopeks = BALANCE
    sub.autopay_enabled = autopay
    sub.autopay_days_before = 3
    await db.commit()
    return sub, user, quota


def _service(monkeypatch, bot=None, cache=None):
    import app.services.monitoring_service as monitoring_module
    import app.services.subscription_renewal_service as renewal_module

    monkeypatch.setattr(renewal_module, 'with_admin_notification_service', AsyncMock(return_value=None))
    recorder = _Recorder()
    monkeypatch.setattr(monitoring_module, 'logger', recorder)
    monkeypatch.setattr(monitoring_module, 'cache', cache or _MemoryCache())
    # Событие мониторинга пишется, только когда processed+failed > 0, — это и есть
    # проверка «failed_count не растёт».
    audit = AsyncMock()
    monkeypatch.setattr(monitoring_module.MonitoringService, '_log_monitoring_event', audit)

    service = monitoring_module.MonitoringService.__new__(monitoring_module.MonitoringService)
    service.bot = bot
    service._notified_users = set()
    service._autopay_fail_state = {}
    service.subscription_service = _FakePanelSync()
    return service, recorder, audit


@pytest.mark.asyncio
async def test_custom_terms_are_skipped_without_error_or_debit(monkeypatch):
    """Живой оплаченный срок: ни ERROR, ни failed_count, ни списания."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, user, _ = await _seed_legacy(db, custom=True, paid_until=NOW + timedelta(days=1))
        end_before = sub.end_date
        service, recorder, audit = _service(monkeypatch)

        await service._process_autopayments(db)

        subscription = await db.get(Subscription, sub.id)
        await db.refresh(subscription)
        assert subscription.end_date == end_before, 'подписка продлилась вопреки индивидуальным условиям'
        assert (await db.get(User, user.id)).balance_kopeks == BALANCE
        assert recorder.events('error') == []
        audit.assert_not_awaited()
        assert any('индивидуальными условиями' in event for event in recorder.events('info'))


@pytest.mark.asyncio
async def test_user_is_told_once_not_every_cycle(monkeypatch):
    """Цикл идёт раз в час — объяснение уходит один раз, а не двадцать раз в сутки."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_legacy(db, custom=True, paid_until=NOW + timedelta(days=1))
        bot = SimpleNamespace(send_message=AsyncMock())
        cache = _MemoryCache()
        service, _, _ = _service(monkeypatch, bot=bot, cache=cache)

        await service._process_autopayments(db)
        assert bot.send_message.await_count == 1
        text = bot.send_message.await_args.kwargs['text']
        assert 'индивидуальные условия' in text
        assert 'докупить LTE' in text

        service._notified_users = set()
        await service._process_autopayments(db)
        assert bot.send_message.await_count == 1, 'повторное уведомление в следующем цикле'


@pytest.mark.asyncio
async def test_standard_migrated_subscription_still_renews(monkeypatch):
    """Перенос без индивидуальных условий продлевается как раньше."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, user, _ = await _seed_legacy(db, custom=False, paid_until=NOW + timedelta(days=1))
        end_before = sub.end_date
        service, recorder, audit = _service(monkeypatch)

        await service._process_autopayments(db)

        subscription = await db.get(Subscription, sub.id)
        await db.refresh(subscription)
        assert subscription.end_date > end_before, 'обычная подписка перестала продлеваться'
        assert (await db.get(User, user.id)).balance_kopeks == BALANCE - 29900
        assert recorder.events('error') == []
        audit.assert_awaited()


@pytest.mark.asyncio
async def test_two_hour_expired_window_cannot_buy_the_technical_tariff(monkeypatch):
    """Строгое `<` в старом guard'е открывало окно на 2 часа после окончания срока."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, user, _ = await _seed_legacy(db, custom=True, paid_until=NOW - timedelta(minutes=30))
        sub.status = SubscriptionStatus.EXPIRED.value
        await db.commit()
        end_before = sub.end_date
        service, recorder, audit = _service(monkeypatch)

        await service._process_autopayments(db)

        subscription = await db.get(Subscription, sub.id)
        await db.refresh(subscription)
        assert subscription.end_date == end_before, 'куплен технический тариф импорта'
        assert subscription.status == SubscriptionStatus.EXPIRED.value
        assert (await db.get(User, user.id)).balance_kopeks == BALANCE
        assert recorder.events('error') == []
        audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_lte_topup_stays_available_while_autopay_is_blocked(monkeypatch):
    """Запрет автопродления не закрывает докупку LTE — это разные решения клиента."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, quota = await _seed_legacy(db, custom=True, paid_until=NOW + timedelta(days=1))
        service, _, _ = _service(monkeypatch)
        await service._process_autopayments(db)

        await credit_topup(db, sub, bytes_amount=20 * 1024**3, operation_id='legacy-topup', now=NOW)
        await db.commit()

        assert legacy_autopay_blocked(await db.get(LteQuota, quota.subscription_id)) is True


@pytest.mark.asyncio
async def test_expiring_notice_does_not_promise_automatic_renewal(monkeypatch):
    """Обещать «продлится автоматически» тому, у кого autopay заблокирован, нельзя."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, user, _ = await _seed_legacy(db, custom=True, paid_until=NOW + timedelta(hours=20))
        import app.services.monitoring_service as monitoring_module

        service, _, _ = _service(monkeypatch, bot=SimpleNamespace(send_message=AsyncMock()))
        sent = AsyncMock(return_value=True)
        monkeypatch.setattr(monitoring_module.MonitoringService, '_send_message_with_logo', sent)

        assert await service._send_subscription_expiring_notification(user, sub, 1, legacy_terms=True) is True
        text = sent.await_args.kwargs['text']
        assert 'продлится автоматически' not in text
        assert 'прежние индивидуальные условия' in text
        assert 'выберите тариф' in text

        # И тот же признак реально доезжает из цикла проверки истекающих подписок.
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr(monitoring_module.MonitoringService, '_send_subscription_expiring_notification', notify)
        await service._check_expiring_subscriptions(db)
        assert notify.await_args is not None, 'уведомление об истечении не отправлялось'
        assert notify.await_args.kwargs['legacy_terms'] is True


@pytest.mark.asyncio
async def test_recurrent_card_topup_skips_policy_without_error(monkeypatch):
    """Рекуррентный платёж по карте отличает политику от сбоя расчёта."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, user, _ = await _seed_legacy(db, custom=True, paid_until=NOW + timedelta(days=1))
        import app.services.recurrent_payment_service as recurrent_module

        recorder = _Recorder()
        monkeypatch.setattr(recurrent_module, 'logger', recorder)
        # Прод отдаёт сюда подписку с уже подгруженным тарифом.
        subscription = (
            await db.execute(
                select(Subscription).options(selectinload(Subscription.tariff)).where(Subscription.id == sub.id)
            )
        ).scalar_one()

        result = await recurrent_module._process_single_subscription(
            db, subscription, user, None, payment_service=None, subscription_service=None
        )

        assert result == 'skipped'
        assert recorder.events('error') == []
        assert any('политике LTE' in event for event in recorder.events('info'))


def _quota_stub(state):
    return SimpleNamespace(legacy_state=state)


def test_predicate_holds_at_the_paid_end_boundary():
    """Ровно на границе оплаченного срока старый guard уже молчит, новый — нет."""
    end = datetime.now(UTC).replace(microsecond=0)
    quota = _quota_stub(
        {
            'original_paid_until': end.isoformat(),
            'runtime': {},
            'snapshot': {'renewal_requires_expiry': True, 'subscription': {'is_trial': False}},
        }
    )
    assert legacy_renewal_requires_expiry(quota, now=end) is False
    assert legacy_autopay_blocked(quota, now=end) is True
    assert legacy_autopay_blocked(quota, now=end + timedelta(days=365)) is True


def test_predicate_is_false_for_trial_missing_state_and_explicit_purchase():
    end = datetime.now(UTC).replace(microsecond=0)
    base = {
        'original_paid_until': end.isoformat(),
        'runtime': {},
        'snapshot': {'renewal_requires_expiry': True, 'subscription': {'is_trial': False}},
    }
    assert legacy_autopay_blocked(_quota_stub(None)) is False

    trial = {**base, 'snapshot': {'renewal_requires_expiry': True, 'subscription': {'is_trial': True}}}
    assert legacy_autopay_blocked(_quota_stub(trial), now=end - timedelta(days=1)) is False

    standard = {**base, 'snapshot': {'renewal_requires_expiry': False, 'subscription': {'is_trial': False}}}
    assert legacy_autopay_blocked(_quota_stub(standard), now=end - timedelta(days=1)) is False

    # Клиент явно купил новый тариф: update_entitlement кладёт pending_new_period,
    # advance_period превращает его в new_limit_bytes — запрет снимается.
    scheduled = {**base, 'runtime': {'pending_new_period': {'starts_at': end.isoformat(), 'limit_bytes': 0}}}
    assert legacy_autopay_blocked(_quota_stub(scheduled), now=end) is False

    started = {**base, 'runtime': {'new_period_started_at': end.isoformat(), 'new_limit_bytes': 60 * 1024**3}}
    assert legacy_autopay_blocked(_quota_stub(started), now=end) is False

    # Автоматический скаффолд после окончания базы покупкой не является.
    scaffold = {**base, 'runtime': {'new_period_started_at': end.isoformat(), 'base_ended': True}}
    assert legacy_autopay_blocked(_quota_stub(scaffold), now=end) is True


@pytest.mark.asyncio
async def test_explicit_new_purchase_unblocks_autopay(monkeypatch):
    """После явного выбора тарифа клиентом автопродление снова работает."""
    from app.services.lte_quota_service import update_entitlement

    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, quota = await _seed_legacy(db, custom=True, paid_until=NOW - timedelta(minutes=30))
        assert legacy_autopay_blocked(quota) is True

        sub.status = SubscriptionStatus.EXPIRED.value
        target = await db.get(Tariff, sub.tariff_id)
        await update_entitlement(db, sub, target, now=datetime.now(UTC))
        await db.commit()

        assert legacy_autopay_blocked(await db.get(LteQuota, quota.subscription_id)) is False
