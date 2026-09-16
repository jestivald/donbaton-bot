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
    GIB,
    LtePolicyError,
    advance_period,
    credit_topup,
    get_quota_status,
    legacy_autopay_blocked,
    legacy_renewal_requires_expiry,
    update_entitlement,
)
from app.services.lte_topup_purchase_service import quote_lte_package
from app.services.pricing_engine import PricingEngine, pricing_engine
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
        sub, user, quota = await _seed_legacy(db, custom=True, paid_until=NOW + timedelta(days=1))
        service, _, _ = _service(monkeypatch)
        await service._process_autopayments(db)

        # Витрина докупки, которую видит клиент, остаётся рабочей...
        tariff = await db.get(Tariff, sub.tariff_id)
        assert quote_lte_package(user, tariff, 10)['price_kopeks'] == 5900
        # ...и байты реально зачисляются на заблокированную для autopay подписку.
        before = (await get_quota_status(db, sub, now=NOW))['remaining_bytes']
        await credit_topup(db, sub, bytes_amount=20 * GIB, operation_id='legacy-topup', now=NOW)
        await db.commit()
        assert (await get_quota_status(db, sub, now=NOW))['remaining_bytes'] == before + 20 * GIB

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
async def test_expired_terms_are_not_described_in_the_future_tense(monkeypatch):
    """В окне «недавно истёкших» дата уже в прошлом — «действуют до» было бы ложью."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await _seed_legacy(db, custom=True, paid_until=NOW - timedelta(minutes=30))
        sub.status = SubscriptionStatus.EXPIRED.value
        await db.commit()
        bot = SimpleNamespace(send_message=AsyncMock())
        service, _, _ = _service(monkeypatch, bot=bot)

        await service._process_autopayments(db)

        text = bot.send_message.await_args.kwargs['text']
        assert 'действуют до' not in text
        assert 'закончился' in text


@pytest.mark.asyncio
async def test_silent_channel_does_not_burn_the_single_notification(monkeypatch):
    """Ключ дедупа ставится только после отправки: молчание — не уведомление."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_legacy(db, custom=True, paid_until=NOW + timedelta(days=1))
        cache = _MemoryCache()
        # Бот ещё не поднят — объясниться нечем.
        service, _, _ = _service(monkeypatch, bot=None, cache=cache)
        await service._process_autopayments(db)
        assert cache.store == {}

        bot = SimpleNamespace(send_message=AsyncMock())
        service.bot = bot
        service._notified_users = set()
        await service._process_autopayments(db)
        assert bot.send_message.await_count == 1
        assert cache.store, 'после отправки дедуп не выставлен'


@pytest.mark.asyncio
async def test_disabled_autopay_gets_the_same_advice(monkeypatch):
    """С выключенным автоплатежом «включите автоплатёж» тоже не сработает."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, user, _ = await _seed_legacy(db, custom=True, paid_until=NOW + timedelta(hours=20), autopay=False)
        import app.services.monitoring_service as monitoring_module

        service, _, _ = _service(monkeypatch, bot=SimpleNamespace(send_message=AsyncMock()))
        sent = AsyncMock(return_value=True)
        monkeypatch.setattr(monitoring_module.MonitoringService, '_send_message_with_logo', sent)

        assert await service._send_subscription_expiring_notification(user, sub, 1, legacy_terms=True) is True
        text = sent.await_args.kwargs['text']
        assert 'Включите автоплатеж' not in text
        assert 'Продлите подписку вручную' not in text
        assert 'выберите тариф' in text

        # Признак считается независимо от autopay_enabled, иначе до текста не дойдёт.
        notify = AsyncMock(return_value=True)
        monkeypatch.setattr(monitoring_module.MonitoringService, '_send_subscription_expiring_notification', notify)
        await service._check_expiring_subscriptions(db)
        assert notify.await_args is not None, 'уведомление об истечении не отправлялось'
        assert notify.await_args.kwargs['legacy_terms'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'status,paid_until',
    [('active', timedelta(days=1)), ('expired', timedelta(minutes=-30))],
)
async def test_recurrent_card_charge_is_skipped_before_pricing(monkeypatch, status, paid_until):
    """Рекуррент не должен списывать с карты «под продление», которого не будет.

    После original_paid_until validate_legacy_renewal молчит, поэтому без
    проверки предиката до расчёта EXPIRED-подписка доходила до перебора карт.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, user, _ = await _seed_legacy(db, custom=True, paid_until=NOW + paid_until)
        sub.status = status
        await db.commit()
        import app.services.recurrent_payment_service as recurrent_module

        recorder = _Recorder()
        monkeypatch.setattr(recurrent_module, 'logger', recorder)
        pricing = AsyncMock()
        monkeypatch.setattr(pricing_engine, 'calculate_renewal_price', pricing)
        # Прод отдаёт сюда подписку с уже подгруженным тарифом.
        subscription = (
            await db.execute(
                select(Subscription).options(selectinload(Subscription.tariff)).where(Subscription.id == sub.id)
            )
        ).scalar_one()

        result = await recurrent_module._process_single_subscription(
            db, subscription, user, None, payment_service=None, subscription_service=None
        )

        assert result == 'skipped', 'рекуррент дошёл до перебора карт'
        pricing.assert_not_awaited()
        assert recorder.events('error') == []
        assert any('индивидуальные условия' in event for event in recorder.events('info'))


@pytest.mark.asyncio
async def test_lte_configuration_failure_stays_an_error(monkeypatch):
    """Выключенный DONBATON_LTE_ENABLED — авария по всем тарифам, а не политика."""
    async with memory_session(monkeypatch, TABLES) as db:
        sub, user, _ = await _seed_legacy(db, custom=False, paid_until=NOW + timedelta(days=1))
        import app.services.recurrent_payment_service as recurrent_module

        recorder = _Recorder()
        monkeypatch.setattr(recurrent_module, 'logger', recorder)
        monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', False)
        subscription = (
            await db.execute(
                select(Subscription).options(selectinload(Subscription.tariff)).where(Subscription.id == sub.id)
            )
        ).scalar_one()

        result = await recurrent_module._process_single_subscription(
            db, subscription, user, None, payment_service=None, subscription_service=None
        )

        assert result == 'skipped'
        assert recorder.events('error'), 'конфигурационная авария ушла в info'


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

    # Клиент явно купил новый тариф: update_entitlement ставит purchased_at и
    # pending_new_period, advance_period превращает второй в new_limit_bytes.
    purchased = {**base, 'runtime': {'purchased_at': end.isoformat()}}
    assert legacy_autopay_blocked(_quota_stub(purchased), now=end) is False

    scheduled = {**base, 'runtime': {'pending_new_period': {'starts_at': end.isoformat(), 'limit_bytes': 0}}}
    assert legacy_autopay_blocked(_quota_stub(scheduled), now=end) is False

    converted = {**base, 'runtime': {'trial_converted_at': end.isoformat()}}
    assert legacy_autopay_blocked(_quota_stub(converted), now=end) is False

    # Маркер читается по присутствию ключа, а не по истинности: тариф «Только
    # Wi-Fi» записывает нулевой объём LTE, и ledger, записанный до появления
    # purchased_at, отличается от неперенесённого только этим ключом.
    for limit_bytes in (60 * GIB, 0):
        started = {**base, 'runtime': {'new_period_started_at': end.isoformat(), 'new_limit_bytes': limit_bytes}}
        assert legacy_autopay_blocked(_quota_stub(started), now=end) is False

    # Автоматический скаффолд после окончания базы покупкой не является.
    scaffold = {**base, 'runtime': {'new_period_started_at': end.isoformat(), 'base_ended': True}}
    assert legacy_autopay_blocked(_quota_stub(scaffold), now=end) is True


def _wifi_only_tariff():
    """Каталог запрещает LTE-пакет в категории «Только Wi-Fi» (lte_traffic_limit_gb = 0)."""
    return Tariff(
        id=7,
        name='Только Wi-Fi',
        network_category='wifi',
        traffic_limit_gb=0,
        lte_traffic_limit_gb=None,
        lte_period_days=None,
        device_limit=3,
        allowed_squads=[WIFI],
        period_prices={'30': 19900},
        is_daily=False,
        custom_traffic_enabled=False,
        is_trial_available=False,
        show_in_gift=False,
        custom_days_enabled=False,
        traffic_topup_enabled=False,
        traffic_topup_packages={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('plan', ['wifi_lte', 'wifi'])
async def test_explicit_new_purchase_unblocks_autopay(monkeypatch, plan):
    """После явного выбора тарифа клиентом автопродление снова работает.

    Тариф «Только Wi-Fi» не содержит LTE-пакета, поэтому покупка записывает
    нулевой объём: маркер обязан пережить и это, и последующий advance_period,
    который удаляет pending_new_period.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, quota = await _seed_legacy(db, custom=True, paid_until=NOW - timedelta(minutes=30))
        assert legacy_autopay_blocked(quota) is True

        if plan == 'wifi':
            target = _wifi_only_tariff()
            db.add(target)
            await db.flush()
        else:
            target = await db.get(Tariff, sub.tariff_id)
        now = datetime.now(UTC)
        sub.status = SubscriptionStatus.EXPIRED.value
        await update_entitlement(db, sub, target, now=now)
        await db.commit()
        assert legacy_autopay_blocked(await db.get(LteQuota, quota.subscription_id)) is False

        # Ближайший цикл переносит pending_new_period в new_limit_bytes и удаляет
        # исходный ключ — для wifi оба значения нулевые.
        advance_period(quota, now + timedelta(seconds=1), paid_until=sub.end_date)
        await db.commit()
        runtime = (await db.get(LteQuota, quota.subscription_id)).legacy_state['runtime']
        assert 'pending_new_period' not in runtime
        assert legacy_autopay_blocked(await db.get(LteQuota, quota.subscription_id)) is False, (
            'запрет вернулся после активации купленного периода'
        )


@pytest.mark.asyncio
async def test_days_only_extension_of_an_expired_legacy_subscription_marks_the_purchase(monkeypatch):
    """Бонусные дни истёкшей legacy-подписке идут через update_entitlement.

    Пока окно совместимости не закрылось, extend_subscription вообще не
    пропускает управляемую подписку (validate_legacy_renewal в
    _prepare_donbaton_subscription). А когда оно закрылось, срок уже истёк, и
    ветка `expired` уводит в update_entitlement — отметка ставится.
    """
    from app.database.crud.subscription import extend_subscription

    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, quota = await _seed_legacy(db, custom=True, paid_until=NOW + timedelta(days=1))
        with pytest.raises(LtePolicyError, match='индивидуальные условия'):
            await extend_subscription(db, sub, 30)

    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, quota = await _seed_legacy(db, custom=True, paid_until=NOW - timedelta(minutes=30))
        assert legacy_autopay_blocked(quota) is True
        await extend_subscription(db, sub, 30)
        assert legacy_autopay_blocked(await db.get(LteQuota, 1)) is False


@pytest.mark.asyncio
async def test_direct_end_date_write_leaves_the_block_on_the_markers(monkeypatch):
    """Админ выставил дату напрямую — запрет держат именно маркеры покупки.

    admin_users.py:1393 пишет subscription.end_date мимо extend_subscription, так
    что ни guard, ни update_entitlement не срабатывают. Подписка становится живой
    уже за original_paid_until: первая ветка предиката молчит, и всё держится на
    отсутствии отметки. Последующее ОПЛАЧЕННОЕ продление того же тарифа идёт в
    initialize_quota и отметки тоже не ставит — задокументированная граница
    (docs/DONBATON.md), снимает запрет только явный выбор тарифа.
    """
    from app.database.crud.subscription import extend_subscription

    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, quota = await _seed_legacy(db, custom=True, paid_until=NOW - timedelta(minutes=30))
        sub.end_date = NOW + timedelta(days=30)
        await db.commit()

        assert legacy_renewal_requires_expiry(await db.get(LteQuota, 1)) is False
        assert legacy_autopay_blocked(await db.get(LteQuota, 1)) is True

        await extend_subscription(db, sub, 30)
        assert legacy_autopay_blocked(await db.get(LteQuota, 1)) is True
        assert (await db.get(LteQuota, 1)).legacy_state is not None

        await update_entitlement(db, sub, await db.get(Tariff, sub.tariff_id), now=datetime.now(UTC))
        await db.commit()
        assert legacy_autopay_blocked(await db.get(LteQuota, 1)) is False
