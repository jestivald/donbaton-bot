"""DonBaton LTE accounting, independent of Remnawave's global limit.

All mutations lock the subscription before the quota; callers commit together
with their purchase. Samples are cumulative *logical bytes*, after the staged
WIFI=0/LTE=1 cutover. A period boundary belongs to the first successful sample
after that boundary: its outstanding delta is charged before rollover. This
customer-favouring sampling tolerance is deliberately not a last-byte guarantee.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.models import LteQuota, LteTopup, Subscription, Tariff
from app.external.remnawave_api import TrafficLimitStrategy, UserStatus


logger = structlog.get_logger(__name__)
GIB = 1024**3
NETWORK_CATEGORIES = ('wifi', 'wifi_lte')


class LtePolicyError(ValueError):
    """An explicit configuration, accounting or entitlement conflict."""


def is_managed(subscription) -> bool:
    return getattr(subscription, 'network_category', None) in NETWORK_CATEGORIES


def needs_managed_policy(subscription) -> bool:
    """Classify a preloaded tariff without triggering asynchronous lazy I/O."""
    loaded_tariff = vars(subscription).get('tariff')
    return is_managed(subscription) or getattr(loaded_tariff, 'network_category', None) in NETWORK_CATEGORIES


def _now(value=None):
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _value(value):
    return getattr(value, 'value', value)


def _squad_sets():
    def parse(raw):
        try:
            return tuple(dict.fromkeys(str(UUID(item.strip())) for item in raw.split(',') if item.strip()))
        except (ValueError, AttributeError) as error:
            raise LtePolicyError('Список серверов Wi-Fi/LTE содержит неверный UUID') from error

    wifi = parse(settings.DONBATON_WIFI_SQUAD_UUIDS)
    lte = parse(settings.DONBATON_LTE_SQUAD_UUIDS)
    if not wifi or not lte or set(wifi) & set(lte):
        raise LtePolicyError('Нужны отдельные непересекающиеся сквады Wi-Fi и LTE')
    return wifi, lte


def validate_tariff_for_sale(tariff) -> None:
    from app.services.donbaton_catalog import validate_tariff_definition

    category = getattr(tariff, 'network_category', None)
    if category is None:
        return
    try:
        validate_tariff_definition(tariff)
    except ValueError as error:
        raise LtePolicyError(str(error)) from error
    if category not in NETWORK_CATEGORIES:
        raise LtePolicyError('Неизвестная категория сети')
    if not settings.DONBATON_LTE_ENABLED or not settings.DONBATON_LTE_ACCOUNTING_READY:
        raise LtePolicyError('Новые тарифы пока не включены: ожидается проверка учёта и серверов')
    wifi, lte = _squad_sets()
    if getattr(tariff, 'traffic_limit_gb', 0) != 0:
        raise LtePolicyError('Для новых тарифов общий трафик должен быть безлимитным')
    if getattr(tariff, 'is_daily', False):
        raise LtePolicyError('Раздельная LTE-квота поддерживает периодные тарифы')
    if getattr(tariff, 'external_squad_uuid', None):
        raise LtePolicyError('Для новых тарифов используйте только проверенные внутренние сквады')
    allowed = set(getattr(tariff, 'allowed_squads', None) or [])
    expected = set(wifi) | (set(lte) if category == 'wifi_lte' else set())
    if allowed != expected:
        raise LtePolicyError('Сквады тарифа не совпадают с проверенной категорией Wi-Fi/LTE')
    limit = getattr(tariff, 'lte_traffic_limit_gb', None)
    period = getattr(tariff, 'lte_period_days', None)
    if category == 'wifi_lte' and (type(limit) is not int or limit <= 0 or type(period) is not int or period <= 0):
        raise LtePolicyError('Для Wi-Fi + LTE задайте положительные объём и период LTE')
    if category == 'wifi' and limit not in (None, 0):
        raise LtePolicyError('В тарифе Wi-Fi не должно быть включённого LTE-пакета')


def validate_subscription_transition(subscription, tariff) -> None:
    """Paid legacy balances need an explicit migration, never an implicit switch."""
    target_managed = getattr(tariff, 'network_category', None) in NETWORK_CATEGORIES
    if (
        (is_managed(subscription) or target_managed)
        and not getattr(subscription, 'is_trial', False)
        and getattr(subscription, 'tariff_id', None) != getattr(tariff, 'id', None)
        and getattr(subscription, 'status', None) in ('active', 'trial', 'limited')
        and getattr(subscription, 'end_date', None)
        and _now(subscription.end_date) > datetime.now(UTC)
    ):
        raise LtePolicyError(
            'До окончания подписки можно продлить текущий тариф или докупить LTE. '
            'Другой тариф можно выбрать после окончания подписки.'
        )
    if is_managed(subscription) and not target_managed:
        raise LtePolicyError('Переход на старый учёт требует отдельной миграции')
    if target_managed and not is_managed(subscription) and not getattr(subscription, 'is_trial', False):
        raise LtePolicyError('Сначала необходимо сохранить оплаченные остатки старого тарифа при миграции')


async def _lock_subscription(db, subscription):
    # Production disables autoflush. Persist pending paid state before a locked
    # reread so a refresh cannot silently discard an in-flight purchase.
    await db.flush()
    await db.execute(select(Subscription.id).where(Subscription.id == subscription.id).with_for_update())
    await db.refresh(
        subscription, ['network_category', 'status', 'end_date', 'traffic_limit_gb', 'tariff_id', 'remnawave_id']
    )


async def _quota(db, subscription, *, lock=False):
    if lock:
        await _lock_subscription(db, subscription)
    query = select(LteQuota).where(LteQuota.subscription_id == subscription.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one_or_none()


async def _topups(db, subscription_id):
    result = await db.execute(
        select(LteTopup).where(LteTopup.subscription_id == subscription_id).order_by(LteTopup.expires_at, LteTopup.id)
    )
    return list(result.scalars().all())


def advance_period(quota, now):
    """Skip missed periods without banking allowances or changing the anchor."""
    now = _now(now)
    if now < quota.period_ends_at:
        return False
    duration = timedelta(days=quota.period_days)
    elapsed = (now - quota.period_ends_at) // duration + 1
    quota.period_starts_at = quota.period_ends_at + duration * (elapsed - 1)
    quota.period_ends_at += duration * elapsed
    if getattr(quota, 'next_limit_bytes', None) is not None:
        quota.limit_bytes = quota.next_limit_bytes
        quota.next_limit_bytes = None
    quota.used_bytes = 0
    quota.base_used_bytes = 0
    return True


def consume_bytes(quota, topups, amount, now):
    """Consume valid buckets by expiry, including the recurring base bucket."""
    if type(amount) is not int or amount < 0:
        raise LtePolicyError('Расход должен быть целым неотрицательным числом байт')
    now = _now(now)
    buckets = [(quota.period_ends_at, -1, quota)]
    buckets.extend((bucket.expires_at, bucket.id or 0, bucket) for bucket in topups if bucket.expires_at > now)
    remaining = amount
    for _, _, bucket in sorted(buckets, key=lambda item: (item[0], item[1])):
        if bucket is quota:
            consumed = min(remaining, max(0, quota.limit_bytes - quota.base_used_bytes))
            quota.base_used_bytes += consumed
        else:
            consumed = min(remaining, bucket.amount_bytes - bucket.used_bytes)
            bucket.used_bytes += consumed
        remaining -= consumed
        if not remaining:
            break
    # Include sampled overshoot in actual usage, even after allowance exhaustion.
    quota.used_bytes += amount


def observe_counter(quota, topups, *, panel_id, lifetime_bytes, now=None):
    """Never forgive usage by lowering a baseline or accepting another identity."""
    now = _now(now)
    if type(panel_id) is not int or panel_id <= 0 or type(lifetime_bytes) is not int or lifetime_bytes < 0:
        quota.accounting_state = 'missing'
        return
    if quota.counter_panel_id is not None and quota.counter_panel_id != panel_id:
        quota.accounting_state = 'identity_changed'
        return
    if quota.counter_bytes is not None and lifetime_bytes < quota.counter_bytes:
        quota.accounting_state = 'regressed'
        return
    if quota.accounting_state == 'identity_changed':
        return  # operator reconciliation is required, even if an old ID reappears
    if quota.last_observed_at and now < quota.last_observed_at:
        return
    if quota.counter_bytes is not None:
        # A late sample cannot determine which side of a time boundary its bytes
        # belonged to. Charge before rollover; do not charge old bytes twice.
        allocation_at = min(now, quota.last_observed_at or now)
        consume_bytes(quota, topups, lifetime_bytes - quota.counter_bytes, allocation_at)
    quota.counter_panel_id = panel_id
    quota.counter_bytes = lifetime_bytes
    quota.last_observed_at = now
    quota.accounting_state = 'ready'
    advance_period(quota, now)


def _status(quota, topups, now):
    active_topups = [bucket for bucket in topups if bucket.expires_at > now]
    # Display the committed period until a successful counter sample closes it.
    remaining = max(0, quota.limit_bytes - quota.base_used_bytes)
    remaining += sum(bucket.amount_bytes - bucket.used_bytes for bucket in active_topups)
    state = quota.accounting_state
    if state == 'ready' and (
        quota.last_observed_at is None
        or now - quota.last_observed_at > timedelta(seconds=settings.DONBATON_LTE_MAX_SAMPLE_AGE_SECONDS)
    ):
        state = 'stale'
    total = quota.limit_bytes + sum(bucket.amount_bytes for bucket in active_topups)
    return {
        'limit_bytes': quota.limit_bytes,
        'next_period_limit_bytes': quota.next_limit_bytes,
        'used_bytes': max(0, total - remaining),
        'observed_period_bytes': quota.used_bytes,
        'topup_bytes': sum(bucket.amount_bytes for bucket in active_topups),
        'remaining_bytes': remaining,
        'period_ends_at': quota.period_ends_at.isoformat(),
        'exhausted': remaining <= 0,
        'accounting_state': state,
        'last_observed_at': quota.last_observed_at.isoformat() if quota.last_observed_at else None,
        'topups': [
            {'remaining_bytes': bucket.amount_bytes - bucket.used_bytes, 'expires_at': bucket.expires_at.isoformat()}
            for bucket in active_topups
        ],
    }


async def initialize_quota(db, subscription, tariff=None, *, now=None):
    now = _now(now)
    if tariff is None and getattr(subscription, 'tariff_id', None):
        tariff = await db.get(Tariff, subscription.tariff_id)
    if not is_managed(subscription) and getattr(tariff, 'network_category', None) not in NETWORK_CATEGORIES:
        return None
    existing = await _quota(db, subscription, lock=True)
    if existing is not None:
        return existing  # never refill or copy newer administrative tariff edits
    validate_tariff_for_sale(tariff)
    if getattr(tariff, 'network_category', None) not in NETWORK_CATEGORIES:
        raise LtePolicyError('У управляемой подписки отсутствуют условия тарифа')
    subscription.network_category = tariff.network_category
    subscription.traffic_limit_gb = 0
    days = tariff.lte_period_days or 30
    quota = LteQuota(
        subscription_id=subscription.id,
        limit_bytes=(tariff.lte_traffic_limit_gb or 0) * GIB,
        used_bytes=0,
        base_used_bytes=0,
        period_days=days,
        period_starts_at=now,
        period_ends_at=now + timedelta(days=days),
        accounting_state='pending',
    )
    db.add(quota)
    await db.flush()
    subscription._lte_quota_status = _status(quota, [], now)
    return quota


async def update_entitlement(db, subscription, tariff, *, now=None):
    validate_subscription_transition(subscription, tariff)
    validate_tariff_for_sale(tariff)
    if getattr(tariff, 'network_category', None) not in NETWORK_CATEGORIES:
        if is_managed(subscription):
            raise LtePolicyError('Переход с раздельного LTE на старый учёт требует миграции')
        return None
    quota = await initialize_quota(db, subscription, tariff, now=now)
    validate_subscription_transition(subscription, tariff)
    # Active paid tariff changes are rejected above. Trial conversion and
    # same-tariff updates preserve the current period; a paid activation
    # after expiry establishes a new period below.
    subscription.network_category = tariff.network_category
    subscription.traffic_limit_gb = 0
    now = _now(now)
    if _now(subscription.end_date) <= now:
        # A new paid activation after expiry buys a complete new period. Hold
        # LTE until the next fresh panel sample establishes its lifetime base.
        quota.limit_bytes = (tariff.lte_traffic_limit_gb or 0) * GIB
        quota.next_limit_bytes = None
        quota.period_starts_at = now
        quota.period_ends_at = now + timedelta(days=tariff.lte_period_days or 30)
        quota.period_days = tariff.lte_period_days or 30
        quota.used_bytes = 0
        quota.base_used_bytes = 0
        quota.counter_bytes = None
        quota.last_observed_at = None
        quota.accounting_state = 'pending'
        return quota
    quota.next_limit_bytes = (tariff.lte_traffic_limit_gb or 0) * GIB
    return quota


async def credit_topup(db, subscription, *, bytes_amount, operation_id, now=None, valid_days=30):
    if subscription.network_category != 'wifi_lte':
        raise LtePolicyError('LTE-пакеты доступны в тарифах Wi-Fi + LTE')
    if not settings.DONBATON_LTE_ENABLED or not settings.DONBATON_LTE_ACCOUNTING_READY:
        raise LtePolicyError('LTE-докупки пока выключены')
    _squad_sets()
    if type(bytes_amount) is not int or bytes_amount <= 0:
        raise LtePolicyError('Пакет должен содержать положительное число байт')
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        raise LtePolicyError('Нужен уникальный идентификатор операции до 128 символов')
    if type(valid_days) is not int or valid_days <= 0:
        raise LtePolicyError('Срок пакета должен быть положительным')
    quota = await _quota(db, subscription, lock=True)
    if quota is None:
        raise LtePolicyError('LTE-квота подписки не инициализирована')
    existing = (await db.execute(select(LteTopup).where(LteTopup.operation_id == operation_id))).scalar_one_or_none()
    if existing:
        if existing.subscription_id != subscription.id or existing.amount_bytes != bytes_amount:
            raise LtePolicyError('Идентификатор операции уже использован для другой покупки')
        return existing
    now = _now(now)
    bucket = LteTopup(
        subscription_id=subscription.id,
        operation_id=operation_id,
        amount_bytes=bytes_amount,
        used_bytes=0,
        created_at=now,
        expires_at=now + timedelta(days=valid_days),
    )
    db.add(bucket)
    await db.flush()
    return bucket


async def get_quota_status(db, subscription, *, now=None):
    if not is_managed(subscription):
        return None
    quota = await _quota(db, subscription)
    if quota is None:
        return None
    result = _status(quota, await _topups(db, subscription.id), _now(now))
    subscription._lte_quota_status = result
    return result


def quota_status_from_subscription(subscription, now=None):
    """Only return a deliberately preloaded snapshot, never lazy database I/O."""
    return getattr(subscription, '_lte_quota_status', None) if is_managed(subscription) else None


def _sample_values(panel_user):
    if isinstance(panel_user, dict):
        traffic = panel_user.get('userTraffic')
        return panel_user.get('id'), traffic.get('lifetimeUsedTrafficBytes') if isinstance(traffic, dict) else None
    traffic = getattr(panel_user, 'user_traffic', None)
    return getattr(panel_user, 'id', None), getattr(traffic, 'lifetime_used_traffic_bytes', None)


async def apply_panel_policy(db, subscription, payload, *, panel_user=None, now=None):
    """Override every managed payload, including sync_squads=False callers."""
    if not is_managed(subscription):
        if needs_managed_policy(subscription):
            raise LtePolicyError('Новая подписка не имеет инициализированной LTE-квоты')
        return payload
    now = _now(now)
    quota = await _quota(db, subscription, lock=True)
    subscription.traffic_limit_gb = 0
    result = dict(
        payload, traffic_limit_bytes=0, traffic_limit_strategy=TrafficLimitStrategy.NO_RESET, active_internal_squads=[]
    )
    if quota is None:
        result['status'] = UserStatus.DISABLED
        return result
    try:
        wifi, lte = _squad_sets()
    except LtePolicyError:
        result['status'] = UserStatus.DISABLED
        return result
    topups = await _topups(db, subscription.id)
    accounting_enabled = settings.DONBATON_LTE_ENABLED and settings.DONBATON_LTE_ACCOUNTING_READY
    if panel_user is not None and accounting_enabled:
        panel_id, lifetime_bytes = _sample_values(panel_user)
        observe_counter(quota, topups, panel_id=panel_id, lifetime_bytes=lifetime_bytes, now=now)
    status = _status(quota, topups, now)
    subscription._lte_quota_status = status
    active = subscription.status in ('active', 'trial') and _now(subscription.end_date) > now
    active = active and _value(payload.get('status', 'ACTIVE')) not in ('DISABLED', 'EXPIRED', 'LIMITED')
    if active:
        result['active_internal_squads'] = list(wifi)
        if (
            accounting_enabled
            and subscription.network_category == 'wifi_lte'
            and status['accounting_state'] == 'ready'
            and not status['exhausted']
        ):
            result['active_internal_squads'] += list(lte)
    return result


async def reconcile_subscription(db, subscription, api, *, user=None):
    """Serialize sample, ledger mutation and panel projection against purchases."""
    if not is_managed(subscription):
        return None
    await _lock_subscription(db, subscription)
    await db.refresh(subscription)
    panel_id = subscription.remnawave_id or (getattr(user, 'remnawave_id', None) if user else None)
    if not panel_id:
        return None
    panel_user = await api.get_user_by_id(panel_id)
    kwargs = await apply_panel_policy(db, subscription, {'user_id': panel_id}, panel_user=panel_user)
    if user is not None and user.status != 'active':
        kwargs.update(status=UserStatus.DISABLED, active_internal_squads=[])
    # Do not reactivate a panel administrator's disable; ordinary paid renewal
    # remains the authoritative path for a status change.
    if _value(getattr(panel_user, 'status', None)) in ('DISABLED', 'EXPIRED'):
        kwargs['active_internal_squads'] = []
    elif (
        _value(getattr(panel_user, 'status', None)) == 'LIMITED'
        and subscription.status in ('active', 'trial')
        and subscription.end_date > datetime.now(UTC)
        and (user is None or user.status == 'active')
    ):
        kwargs['status'] = UserStatus.ACTIVE
    updated = await api.update_user(**kwargs)
    await db.commit()
    return updated


async def run_quota_cycle(subscription_service):
    """One independent polling pass; no Telegram, payment or email effects."""
    from app.database.database import AsyncSessionLocal
    from app.database.models import User

    if not subscription_service.is_configured:
        return
    # Also project WIFI-only when an operator disables an already-used feature.
    async with AsyncSessionLocal() as db:
        ids = list(
            (await db.execute(select(Subscription.id).where(Subscription.network_category.in_(NETWORK_CATEGORIES))))
            .scalars()
            .all()
        )
    async with subscription_service.get_api_client() as api:
        semaphore = asyncio.Semaphore(10)

        async def reconcile_one(subscription_id):
            async with semaphore, AsyncSessionLocal() as db:
                try:
                    sub = await db.get(Subscription, subscription_id)
                    if sub is None:
                        return
                    user = await db.get(User, sub.user_id)
                    await reconcile_subscription(db, sub, api, user=user)
                except Exception as error:
                    await db.rollback()
                    logger.error('LTE quota reconciliation failed', subscription_id=subscription_id, error=str(error))

        await asyncio.gather(*(reconcile_one(subscription_id) for subscription_id in ids))
