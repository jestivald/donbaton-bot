"""DonBaton LTE accounting, independent of Remnawave's global limit.

All mutations lock the subscription before the quota; callers commit together
with their purchase. Samples are cumulative *logical bytes*, after the staged
WIFI=0/LTE=1 cutover. A period boundary belongs to the first successful sample
after that boundary: its outstanding delta is charged before rollover. This
customer-favouring sampling tolerance is deliberately not a last-byte guarantee.
"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import or_, select

from app.config import settings
from app.database.models import LteOrphanQuota, LteQuota, LteTopup, Subscription, Tariff, TrafficPurchase, User
from app.external.remnawave_api import TrafficLimitStrategy, UserStatus
from app.services.lte_accounting_service import (
    LegacyAccountingError,
    advance_legacy_state,
    build_legacy_state,
    ceil_units,
    legacy_budget,
    legacy_compatibility_end,
    legacy_device_limit,
    legacy_is_unlimited,
    legacy_pool_boundary,
    physical_counter_delta,
    timestamp,
    valid_telemetry_heartbeat,
)


logger = structlog.get_logger(__name__)
GIB = 1024**3
# Здесь важно только членство, но порядок держим как в donbaton_catalog.
NETWORK_CATEGORIES = ('wifi_lte', 'wifi')
_UNSET_HEARTBEAT = object()


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


def _telemetry_node_ids():
    try:
        nodes = {str(UUID(item.strip())) for item in settings.DONBATON_LTE_NODE_UUIDS.split(',') if item.strip()}
    except (ValueError, AttributeError) as error:
        raise LtePolicyError('Неверные UUID нод подтверждения LTE-учёта') from error
    if len(nodes) != 2:
        raise LtePolicyError('Для подтверждения LTE-учёта нужны UUID двух проверенных LTE-нод')
    return sorted(nodes)


async def _read_telemetry_heartbeat(*, now=None):
    from app.utils.cache import cache

    epoch = settings.DONBATON_LTE_ACCOUNTING_EPOCH
    if not epoch:
        return None
    try:
        async with asyncio.timeout(2):
            return await cache.get(f'donbaton:lte:telemetry:{epoch}')
    except TimeoutError:
        logger.warning('LTE telemetry Redis read timed out')
        return None


async def _with_telemetry_status(status, *, now, heartbeat=_UNSET_HEARTBEAT):
    if heartbeat is _UNSET_HEARTBEAT:
        heartbeat = await _read_telemetry_heartbeat(now=now)
    try:
        healthy = valid_telemetry_heartbeat(
            heartbeat,
            epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
            node_ids=_telemetry_node_ids(),
            now=now,
            max_age_seconds=settings.DONBATON_LTE_MAX_SAMPLE_AGE_SECONDS,
        )
    except LtePolicyError:
        healthy = False
    if not healthy and status['accounting_state'] == 'ready':
        status['accounting_state'] = 'telemetry_unavailable'
    return status


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
    if not settings.DONBATON_LTE_ACCOUNTING_EPOCH:
        raise LtePolicyError('Не задана подтверждённая эпоха LTE-учёта')
    _telemetry_node_ids()
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
    if (
        target_managed
        and not is_managed(subscription)
        and not getattr(subscription, 'is_trial', False)
        and (not getattr(subscription, 'end_date', None) or _now(subscription.end_date) > datetime.now(UTC))
    ):
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


def legacy_renewal_requires_expiry(quota, *, now=None):
    state = getattr(quota, 'legacy_state', None)
    return bool(
        state
        and state['snapshot']['renewal_requires_expiry']
        and not (state['snapshot']['subscription'] or {}).get('is_trial', False)
        and _now(now) < legacy_compatibility_end(state)
    )


async def validate_legacy_renewal(db, subscription, *, now=None):
    if not is_managed(subscription):
        return
    quota = await _quota(db, subscription)
    if legacy_renewal_requires_expiry(quota, now=now):
        raise LtePolicyError(
            'Прежние индивидуальные условия действуют до окончания оплаченного срока. '
            'Сейчас можно докупить LTE, а новый тариф выбрать после окончания подписки.'
        )


async def validate_legacy_purchase_transition(db, subscription, tariff, *, now=None):
    """The synchronous quote guard cannot see valid legacy TrafficPurchase rows."""
    validate_subscription_transition(subscription, tariff)
    if is_managed(subscription) or getattr(tariff, 'network_category', None) not in NETWORK_CATEGORIES:
        return
    valid_purchase = (
        await db.execute(
            select(TrafficPurchase.id)
            .where(TrafficPurchase.subscription_id == subscription.id, TrafficPurchase.expires_at > _now(now))
            .limit(1)
        )
    ).scalar_one_or_none()
    if valid_purchase is not None:
        raise LtePolicyError('Сначала необходимо перенести действующие докупки старого тарифа без потери остатка')


def _import_state(snapshot, migration_id, plan_sha256, counter_epoch, target_limit_bytes, now):
    try:
        return build_legacy_state(
            snapshot=snapshot,
            migration_id=migration_id,
            plan_sha256=plan_sha256,
            counter_epoch=counter_epoch,
            target_limit_bytes=target_limit_bytes,
            now=now,
        )
    except LegacyAccountingError as error:
        raise LtePolicyError(str(error)) from error


def _same_import(quota, state):
    old = getattr(quota, 'legacy_state', None)
    keys = ('migration_id', 'plan_sha256', 'counter_epoch', 'snapshot', 'target_limit_bytes', 'target_tariff_id')
    if old is None or any(old.get(key) != state.get(key) for key in keys):
        raise LtePolicyError('Подписка уже имеет другой LTE-ledger или снимок миграции')
    return quota


def _import_period(state, now):
    return dict(
        limit_bytes=0,
        used_bytes=0,
        base_used_bytes=0,
        period_days=30,
        period_starts_at=now,
        period_ends_at=_legacy_period_end(state),
        counter_panel_id=state['snapshot']['panel']['id'],
        accounting_state='prepared',
        legacy_state=state,
    )


def _legacy_period_end(state):
    end = legacy_compatibility_end(state)
    reset = state['runtime']['next_reset_at']
    return min(end, timestamp(reset)) if reset else end


async def import_legacy_quota(
    db, subscription, tariff, *, snapshot, migration_id, plan_sha256, counter_epoch, now=None
):
    """Explicit, idempotent transfer; the caller owns the transaction and drain."""
    from app.services.donbaton_catalog import validate_tariff_definition

    now = _now(now)
    validate_tariff_definition(tariff)
    if tariff.network_category != 'wifi_lte':
        raise LtePolicyError('Старые LTE-права переносятся в технический тариф Wi-Fi + LTE')
    wifi, lte = _squad_sets()
    if set(tariff.allowed_squads or []) != set(wifi) | set(lte):
        raise LtePolicyError('Тариф переноса должен использовать проверенные группы Wi-Fi и LTE')
    state = _import_state(snapshot, migration_id, plan_sha256, counter_epoch, tariff.lte_traffic_limit_gb * GIB, now)
    state['target_tariff_id'] = tariff.id
    source = state['snapshot']['subscription']
    if source is None or source['id'] != subscription.id or source['user_id'] != subscription.user_id:
        raise LtePolicyError('Снимок принадлежит другой подписке')
    # Same lock order as paid purchases. Existing quota is checked before the
    # source fields because a successful import deliberately changes those fields.
    await db.execute(select(User.id).where(User.id == subscription.user_id).with_for_update())
    existing = await _quota(db, subscription, lock=True)
    if existing:
        return _same_import(existing, state)
    await db.refresh(subscription)
    if is_managed(subscription):
        raise LtePolicyError('Нельзя импортировать старый снимок поверх новой подписки')
    current = {
        name: getattr(subscription, name)
        for name in ('id', 'user_id', 'tariff_id', 'status', 'is_trial', 'device_limit')
    }
    current.update(
        end_date=_now(subscription.end_date).isoformat(),
        traffic_limit_gb=subscription.traffic_limit_gb or 0,
        purchased_traffic_gb=subscription.purchased_traffic_gb or 0,
        traffic_reset_at=_now(subscription.traffic_reset_at).isoformat() if subscription.traffic_reset_at else None,
    )
    if any(source.get(name) != value for name, value in current.items()):
        raise LtePolicyError('Подписка изменилась после снимка миграции')
    user = await db.get(User, subscription.user_id)
    if (subscription.remnawave_id or user.remnawave_id) != state['snapshot']['panel']['id']:
        raise LtePolicyError('Panel ID подписки не совпадает со снимком')
    purchases = list(
        (
            await db.execute(
                select(TrafficPurchase)
                .where(TrafficPurchase.subscription_id == subscription.id)
                .order_by(TrafficPurchase.id)
                .with_for_update()
            )
        ).scalars()
    )
    current_grants = [
        {
            'id': purchase.id,
            'amount_units': purchase.traffic_gb * GIB,
            'created_at': _now(purchase.created_at).isoformat(),
            'expires_at': _now(purchase.expires_at).isoformat(),
        }
        for purchase in purchases
    ]
    if current_grants != state['snapshot']['grants']:
        raise LtePolicyError('Докупки изменились после снимка миграции')
    quota = LteQuota(subscription_id=subscription.id, **_import_period(state, now))
    subscription.network_category = 'wifi_lte'
    subscription.tariff_id = tariff.id
    subscription.traffic_limit_gb = 0
    subscription.purchased_traffic_gb = 0
    subscription.traffic_reset_at = None
    subscription.traffic_used_gb = 0.0
    subscription.device_limit = legacy_device_limit(state)
    subscription.connected_squads = list(tariff.allowed_squads)
    if state['snapshot']['panel']['status'] == 'DISABLED':
        subscription.status = 'disabled'
    elif subscription.status == 'limited' and subscription.end_date > now:
        subscription.status = 'active'
    db.add(quota)
    await db.flush()
    return quota


async def import_orphan_quota(db, *, snapshot, migration_id, plan_sha256, counter_epoch, now=None):
    now = _now(now)
    state = _import_state(snapshot, migration_id, plan_sha256, counter_epoch, 0, now)
    if state['snapshot']['subscription'] is not None or state['unlimited']:
        raise LtePolicyError('Отдельный ledger предназначен только для конечных прав без bot-подписки')
    panel_id = state['snapshot']['panel']['id']
    # Serializes concurrent orphan imports, including when no ledger row exists.
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy import text

        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': -panel_id})
    linked = (
        await db.execute(select(Subscription.id).where(Subscription.remnawave_id == panel_id).limit(1))
    ).scalar_one_or_none()
    user_link = (await db.execute(select(User.id).where(User.remnawave_id == panel_id).limit(1))).scalar_one_or_none()
    if linked is not None or user_link is not None:
        raise LtePolicyError('Panel ID связан с bot-пользователем и не является orphan')
    existing = (
        await db.execute(select(LteOrphanQuota).where(LteOrphanQuota.panel_id == panel_id).with_for_update())
    ).scalar_one_or_none()
    if existing:
        return _same_import(existing, state)
    quota = LteOrphanQuota(
        panel_id=panel_id,
        end_date=timestamp(state['original_paid_until']),
        original_status=state['snapshot']['panel']['status'],
        **_import_period(state, now),
    )
    db.add(quota)
    await db.flush()
    return quota


def _activate_legacy_quota(quota, *, migration_id, counter_epoch, panel_id, lifetime_bytes, now):
    state = getattr(quota, 'legacy_state', None)
    if not state or state['migration_id'] != migration_id or state['counter_epoch'] != counter_epoch:
        raise LtePolicyError('Эпоха или идентификатор миграции не совпадает')
    panel = state['snapshot']['panel']
    if type(panel_id) is not int or panel_id != panel['id']:
        raise LtePolicyError('Идентификатор панели изменился после снимка')
    if type(lifetime_bytes) is not int or lifetime_bytes != panel['lifetime_bytes']:
        raise LtePolicyError('Счётчик изменился после снимка: необходима сверка границы ×17→1')
    if state['activated_at']:
        return quota  # never lower a baseline after an idempotent operator retry
    if now < timestamp(state['prepared_at']):
        raise LtePolicyError('Активация не может предшествовать подготовке')
    state = deepcopy(state)
    state['activated_at'] = now.isoformat()
    quota.legacy_state = state
    quota.counter_panel_id = panel_id
    quota.counter_bytes = lifetime_bytes
    quota.last_observed_at = now
    quota.accounting_state = 'ready'
    return quota


async def activate_legacy_quota(db, subscription, *, migration_id, counter_epoch, panel_id, lifetime_bytes, now=None):
    quota = await _quota(db, subscription, lock=True)
    if quota is None:
        raise LtePolicyError('Сначала подготовьте ledger миграции')
    return _activate_legacy_quota(
        quota,
        migration_id=migration_id,
        counter_epoch=counter_epoch,
        panel_id=panel_id,
        lifetime_bytes=lifetime_bytes,
        now=_now(now),
    )


async def activate_orphan_quota(db, quota, *, migration_id, counter_epoch, panel_id, lifetime_bytes, now=None):
    await db.flush()
    quota = (
        await db.execute(
            select(LteOrphanQuota)
            .where(LteOrphanQuota.panel_id == quota.panel_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return _activate_legacy_quota(
        quota,
        migration_id=migration_id,
        counter_epoch=counter_epoch,
        panel_id=panel_id,
        lifetime_bytes=lifetime_bytes,
        now=_now(now),
    )


def advance_period(quota, now, *, paid_until=None):
    """Skip missed periods without banking allowances or changing the anchor."""
    now = _now(now)
    pending_period = getattr(quota, 'pending_period', None)
    if pending_period and timestamp(pending_period['starts_at']) <= now:
        quota.limit_bytes = pending_period['limit_bytes']
        quota.next_limit_bytes = None
        quota.period_days = 30
        quota.period_starts_at = timestamp(pending_period['starts_at'])
        quota.period_ends_at = quota.period_starts_at + timedelta(days=30)
        quota.used_bytes = 0
        quota.base_used_bytes = 0
        quota.trial_ends_at = None
        quota.pending_period = None
    if getattr(quota, 'trial_ends_at', None):
        return False  # a trial is a single allowance, never a recurring refill
    old_state = getattr(quota, 'legacy_state', None)
    if old_state:
        state = advance_legacy_state(old_state, now)
        runtime = state['runtime']
        paid_boundary = legacy_compatibility_end(state)
        pending = runtime.get('pending_new_period')
        if pending and timestamp(pending['starts_at']) <= now:
            start = timestamp(pending['starts_at'])
            quota.limit_bytes = pending['limit_bytes']
            runtime['new_limit_bytes'] = pending['limit_bytes']
            runtime['new_period_started_at'] = start.isoformat()
            del runtime['pending_new_period']
            quota.period_starts_at = start
            quota.period_ends_at = start + timedelta(days=30)
            quota.next_limit_bytes = None
            quota.used_bytes = 0
            quota.base_used_bytes = 0
        elif runtime['base_ended'] and not runtime['new_period_started_at']:
            # A scaffold with zero base is safe for expired access. An early
            # paid extension establishes its first new period at the immutable
            # old end; a later purchase explicitly schedules its activation.
            quota.limit_bytes = state['target_limit_bytes'] if paid_until and _now(paid_until) > paid_boundary else 0
            quota.period_starts_at = paid_boundary
            quota.period_ends_at = paid_boundary + timedelta(days=30)
            quota.next_limit_bytes = None
            quota.used_bytes = 0
            quota.base_used_bytes = 0
            runtime['new_period_started_at'] = paid_boundary.isoformat()
        elif not runtime['base_ended']:
            if runtime['last_reset_at'] != old_state['runtime']['last_reset_at']:
                quota.used_bytes = 0
            quota.period_starts_at = timestamp(runtime['last_reset_at'] or state['snapshot']['captured_at'])
            quota.period_ends_at = _legacy_period_end(state)
            quota.legacy_state = state
            return state != old_state
        quota.legacy_state = state
    if now < quota.period_ends_at:
        return bool(old_state and quota.legacy_state != old_state)
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
    state = deepcopy(getattr(quota, 'legacy_state', None))
    if state and legacy_is_unlimited(state):
        state['runtime']['used_units'] += amount * state['snapshot']['divisor']
        quota.legacy_state = state
        quota.used_bytes += amount
        return
    if state and state['runtime']['phase'] != 'finished':
        buckets.append((legacy_pool_boundary(state), -2, state))
    buckets.extend((bucket.expires_at, bucket.id or 0, bucket) for bucket in topups if bucket.expires_at > now)
    remaining = amount
    for _, _, bucket in sorted(buckets, key=lambda item: (item[0], item[1])):
        if bucket is quota:
            consumed = min(remaining, max(0, quota.limit_bytes - quota.base_used_bytes))
            quota.base_used_bytes += consumed
        elif bucket is state:
            consumed = min(remaining, legacy_budget(state).remaining_bytes)
            state['runtime']['used_units'] += consumed * state['snapshot']['divisor']
        else:
            consumed = min(remaining, bucket.amount_bytes - bucket.used_bytes)
            bucket.used_bytes += consumed
        remaining -= consumed
        if not remaining:
            break
    # Include sampled overshoot in actual usage, even after allowance exhaustion.
    quota.used_bytes += amount
    if state:
        if remaining and state['runtime']['phase'] != 'finished':
            state['runtime']['used_units'] += remaining * state['snapshot']['divisor']
        quota.legacy_state = state


def observe_counter(quota, topups, *, panel_id, lifetime_bytes, now=None, paid_until=None):
    """Never forgive usage by lowering a baseline or accepting another identity."""
    now = _now(now)
    state = getattr(quota, 'legacy_state', None)
    if state and not state['activated_at']:
        quota.accounting_state = 'prepared'
        return
    if state and (
        quota.accounting_state == 'epoch_mismatch' or state['counter_epoch'] != settings.DONBATON_LTE_ACCOUNTING_EPOCH
    ):
        quota.accounting_state = 'epoch_mismatch'
        return  # operator reconciliation is needed for any possibly mixed delta
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
        delta = lifetime_bytes - quota.counter_bytes
        if state:
            try:
                delta = physical_counter_delta(
                    previous_panel_id=quota.counter_panel_id,
                    current_panel_id=panel_id,
                    previous_counter=quota.counter_bytes,
                    current_counter=lifetime_bytes,
                    expected_epoch=state['counter_epoch'],
                    sample_epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
                )
            except LegacyAccountingError:
                quota.accounting_state = 'epoch_mismatch'
                return
        consume_bytes(quota, topups, delta, allocation_at)
    quota.counter_panel_id = panel_id
    quota.counter_bytes = lifetime_bytes
    quota.last_observed_at = now
    quota.accounting_state = 'ready'
    advance_period(quota, now, paid_until=paid_until)


def _status(quota, topups, now):
    active_topups = [bucket for bucket in topups if bucket.expires_at > now]
    # Display the committed period until a successful counter sample closes it.
    remaining = max(0, quota.limit_bytes - quota.base_used_bytes)
    remaining += sum(bucket.amount_bytes - bucket.used_bytes for bucket in active_topups)
    legacy = getattr(quota, 'legacy_state', None)
    unlimited = bool(legacy and legacy_is_unlimited(legacy))
    legacy_limit = legacy_budget(legacy).limit_bytes if legacy else 0
    if legacy:
        remaining += legacy_budget(legacy).remaining_bytes
    state = quota.accounting_state
    if state == 'ready' and (
        quota.last_observed_at is None
        or now - quota.last_observed_at > timedelta(seconds=settings.DONBATON_LTE_MAX_SAMPLE_AGE_SECONDS)
    ):
        state = 'stale'
    limit = quota.limit_bytes + legacy_limit
    total = limit + sum(bucket.amount_bytes for bucket in active_topups)
    mode = 'rolling_30_days'
    if getattr(quota, 'trial_ends_at', None):
        mode = 'trial'
    if legacy:
        if not legacy['runtime']['base_ended']:
            mode = (
                'legacy_no_reset' if legacy['snapshot']['panel']['reset_strategy'] == 'NO_RESET' else 'legacy_calendar'
            )
        elif legacy['runtime']['phase'] == 'sidecar':
            mode = 'rolling_30_days_with_legacy_pool'
    pool_bytes = legacy_limit if legacy and legacy['runtime']['base_ended'] else 0
    pool_expiry = None
    if pool_bytes:
        active_expiries = [
            timestamp(grant['expires_at'])
            for grant in legacy['snapshot']['grants']
            if grant['id'] in legacy['runtime']['installed_grant_ids'] and timestamp(grant['expires_at']) > now
        ]
        pool_expiry = min(active_expiries).isoformat() if active_expiries else legacy['runtime']['next_reset_at']
    return {
        'limit_bytes': 0 if unlimited else limit,
        'next_period_limit_bytes': quota.next_limit_bytes,
        'used_bytes': 0 if unlimited else max(0, total - remaining),
        'observed_period_bytes': quota.used_bytes,
        'topup_bytes': sum(bucket.amount_bytes for bucket in active_topups),
        'remaining_bytes': 0 if unlimited else remaining,
        'period_ends_at': quota.period_ends_at.isoformat(),
        'exhausted': not unlimited and remaining <= 0,
        'is_unlimited': unlimited,
        'legacy_compatibility_until': legacy_compatibility_end(legacy).isoformat() if legacy else None,
        'period_mode': mode,
        'renewal_requires_expiry': legacy_renewal_requires_expiry(quota, now=now),
        'has_carried_lte': any(bucket.amount_bytes > bucket.used_bytes for bucket in active_topups)
        or bool(legacy and legacy['runtime']['base_ended'] and legacy_budget(legacy).remaining_bytes > 0),
        'legacy_pool_bytes': pool_bytes,
        'legacy_pool_expires_at': pool_expiry,
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


async def initialize_trial_quota(db, subscription, *, legacy_traffic_limit_gb, now=None):
    """One new trial allowance expressed in real bytes, with no legacy import."""
    now = _now(now)
    if not settings.DONBATON_LTE_ENABLED or not settings.DONBATON_LTE_ACCOUNTING_READY:
        raise LtePolicyError('Новый учёт пробного LTE пока не включён')
    if type(legacy_traffic_limit_gb) is not int or legacy_traffic_limit_gb <= 0:
        raise LtePolicyError('Пробный LTE должен иметь положительный конечный лимит')
    wifi, lte = _squad_sets()
    existing = await _quota(db, subscription, lock=True)
    if existing:
        return existing
    if not subscription.is_trial or subscription.end_date <= now:
        raise LtePolicyError('Пробный пакет выдаётся только действующей пробной подписке')
    quota = LteQuota(
        subscription_id=subscription.id,
        limit_bytes=ceil_units(legacy_traffic_limit_gb * GIB, 17),
        used_bytes=0,
        base_used_bytes=0,
        period_days=30,
        period_starts_at=now,
        period_ends_at=subscription.end_date,
        trial_ends_at=subscription.end_date,
        accounting_state='pending',
    )
    subscription.network_category = 'wifi_lte'
    subscription.traffic_limit_gb = 0
    subscription.connected_squads = list(wifi) + list(lte)
    db.add(quota)
    await db.flush()
    return quota


async def activate_pending_trial_quota(db, subscription, *, now=None):
    """Start an unused pending trial at payment time; retries never move dates."""
    if not is_managed(subscription) or not subscription.is_trial:
        return False
    quota = await _quota(db, subscription, lock=True)
    if quota is None or quota.trial_ends_at is None:
        return False
    if subscription.status != 'pending':
        return True
    if (
        quota.counter_bytes is not None
        or quota.last_observed_at is not None
        or quota.used_bytes
        or quota.base_used_bytes
    ):
        raise LtePolicyError('Пробный пакет уже использовался: повторная активация не должна выдавать трафик')
    now = _now(now)
    duration = quota.trial_ends_at - quota.period_starts_at
    if duration <= timedelta(0) or now < quota.period_starts_at:
        raise LtePolicyError('Неверная сохранённая длительность пробного пакета')
    subscription.start_date = now
    subscription.end_date = now + duration
    subscription.status = 'trial'
    quota.period_starts_at = now
    quota.period_ends_at = now + duration
    quota.trial_ends_at = now + duration
    quota.panel_access_cleared_at = None
    return True


async def update_entitlement(db, subscription, tariff, *, now=None):
    validate_subscription_transition(subscription, tariff)
    validate_tariff_for_sale(tariff)
    await validate_legacy_renewal(db, subscription, now=now)
    if not is_managed(subscription) and _now(subscription.end_date) <= _now(now):
        await validate_legacy_purchase_transition(db, subscription, tariff, now=now)
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
    converting_trial = subscription.is_trial
    if _now(subscription.end_date) <= now or converting_trial:
        subscription.device_limit = tariff.device_limit
        quota.panel_access_cleared_at = None
        if quota.legacy_state:
            # Preserve the old lifetime checkpoint and charge its outstanding
            # delta to old rights before granting the new paid activation.
            # Clearing it here would silently forgive unobserved topup usage.
            state = deepcopy(quota.legacy_state)
            if converting_trial:
                state['runtime']['trial_converted_at'] = now.isoformat()
            state['runtime']['pending_new_period'] = {
                'starts_at': now.isoformat(),
                'limit_bytes': (tariff.lte_traffic_limit_gb or 0) * GIB,
            }
            quota.legacy_state = state
            if quota.accounting_state not in ('identity_changed', 'epoch_mismatch', 'regressed'):
                quota.accounting_state = 'pending'
            return quota
        # Even an ordinary new-plan subscription may have unobserved traffic
        # against paid topups. Settle the old checkpoint before granting a new
        # base; installing a lower/empty baseline here would forgive that usage.
        quota.pending_period = {'starts_at': now.isoformat(), 'limit_bytes': (tariff.lte_traffic_limit_gb or 0) * GIB}
        if quota.accounting_state not in ('identity_changed', 'epoch_mismatch', 'regressed'):
            quota.accounting_state = 'pending'
        return quota
    quota.next_limit_bytes = (tariff.lte_traffic_limit_gb or 0) * GIB
    return quota


async def credit_topup(db, subscription, *, bytes_amount, operation_id, now=None, valid_days=30):
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
    if subscription.network_category != 'wifi_lte':
        raise LtePolicyError('LTE-пакеты доступны в тарифах Wi-Fi + LTE')
    if quota is None:
        raise LtePolicyError('LTE-квота подписки не инициализирована')
    if (
        quota.legacy_state
        and legacy_is_unlimited(quota.legacy_state)
        and _now(now) < legacy_compatibility_end(quota.legacy_state)
    ):
        raise LtePolicyError('LTE уже безлимитен до окончания прежних оплаченных условий — докупка не требуется')
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
    now = _now(now)
    result = await _with_telemetry_status(_status(quota, await _topups(db, subscription.id), now), now=now)
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


async def apply_panel_policy(db, subscription, payload, *, panel_user=None, now=None, heartbeat=_UNSET_HEARTBEAT):
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
    unused_pending_trial = (
        subscription.status == 'pending'
        and subscription.is_trial
        and quota.trial_ends_at is not None
        and quota.counter_bytes is None
    )
    if panel_user is not None and accounting_enabled and not unused_pending_trial:
        panel_id, lifetime_bytes = _sample_values(panel_user)
        observe_counter(
            quota,
            topups,
            panel_id=panel_id,
            lifetime_bytes=lifetime_bytes,
            now=now,
            paid_until=subscription.end_date,
        )
    if quota.legacy_state and now < legacy_compatibility_end(quota.legacy_state):
        subscription.device_limit = legacy_device_limit(quota.legacy_state)
        result['hwid_device_limit'] = subscription.device_limit
    status = await _with_telemetry_status(_status(quota, topups, now), now=now, heartbeat=heartbeat)
    subscription._lte_quota_status = status
    active = subscription.status in ('active', 'trial') and _now(subscription.end_date) > now
    active = active and _value(payload.get('status', 'ACTIVE')) not in ('DISABLED', 'EXPIRED', 'LIMITED')
    if active:
        result['active_internal_squads'] = list(wifi)
        if accounting_enabled and status['accounting_state'] == 'ready' and not status['exhausted']:
            result['active_internal_squads'] += list(lte)
    return result


async def reconcile_subscription(db, subscription, api, *, user=None, heartbeat=_UNSET_HEARTBEAT):
    """Serialize sample, ledger mutation and panel projection against purchases."""
    if not is_managed(subscription):
        return None
    await _lock_subscription(db, subscription)
    await db.refresh(subscription)
    panel_id = subscription.remnawave_id or (getattr(user, 'remnawave_id', None) if user else None)
    if not panel_id:
        return None
    panel_user = await api.get_user_by_id(panel_id)
    kwargs = await apply_panel_policy(
        db, subscription, {'user_id': panel_id}, panel_user=panel_user, heartbeat=heartbeat
    )
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
    # Sampling must remain durable when access already matches. Rewriting an
    # unchanged squad set can otherwise enqueue thousands of needless node jobs.
    updated = panel_user if _panel_policy_matches(panel_user, kwargs) else await _write_policy(api, kwargs)
    quota = await _quota(db, subscription)
    if quota:
        quota.panel_access_cleared_at = datetime.now(UTC) if not kwargs.get('active_internal_squads') else None
    await db.commit()
    return updated


async def _write_policy(api, payload):
    """Отправить посчитанную политику через единственного писателя панели.

    Писать сюда напрямую нельзя: с 4.8.0 все обращения к ``update_user`` живут в
    ``app/services/panel_sync/writer.py``, и сторож ``tests/services/panel_sync/
    test_no_bypass.py`` следит за этим. Решение «писать ли вообще» остаётся
    здесь — его принимает ``_panel_policy_matches`` выше по стеку, и оно важное:
    панель на каждый PATCH ставит ноды в очередь на перезапуск, а этот цикл
    ходит по всему управляемому флоту.
    """
    from app.services.panel_sync.writer import patch_panel_policy

    fields = dict(payload)
    return await patch_panel_policy(api, user_id=fields.pop('user_id'), fields=fields)


def _panel_policy_matches(panel_user, payload):
    known_fields = {
        'user_id',
        'traffic_limit_bytes',
        'traffic_limit_strategy',
        'active_internal_squads',
        'status',
        'hwid_device_limit',
    }
    if not set(payload) <= known_fields:
        return False
    if payload.get('user_id') != getattr(panel_user, 'id', None):
        return False
    for field in ('traffic_limit_bytes', 'traffic_limit_strategy', 'status', 'hwid_device_limit'):
        if field in payload and _value(payload[field]) != _value(getattr(panel_user, field, None)):
            return False
    if 'active_internal_squads' in payload:
        current = getattr(panel_user, 'active_internal_squads', None)
        if not isinstance(current, list):
            return False
        identifiers = [item.get('uuid') if isinstance(item, dict) else getattr(item, 'uuid', None) for item in current]
        if any(not isinstance(value, str) for value in identifiers):
            return False
        if set(identifiers) != set(payload['active_internal_squads']):
            return False
    return True


async def reconcile_orphan_quota(db, quota, api, *, now=None, heartbeat=_UNSET_HEARTBEAT):
    """Preserve finite panel access without creating an application identity."""
    now = _now(now)
    await db.flush()
    quota = (
        await db.execute(
            select(LteOrphanQuota)
            .where(LteOrphanQuota.panel_id == quota.panel_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    linked = (
        await db.execute(select(Subscription.id).where(Subscription.remnawave_id == quota.panel_id).limit(1))
    ).scalar_one_or_none()
    user_link = (
        await db.execute(select(User.id).where(User.remnawave_id == quota.panel_id).limit(1))
    ).scalar_one_or_none()
    if linked is not None or user_link is not None:
        raise LtePolicyError('Orphan получил bot-подписку: сначала передайте ledger без двойного управления')
    panel_user = await api.get_user_by_id(quota.panel_id)
    enabled = settings.DONBATON_LTE_ENABLED and settings.DONBATON_LTE_ACCOUNTING_READY
    if enabled:
        panel_id, lifetime = _sample_values(panel_user)
        observe_counter(quota, [], panel_id=panel_id, lifetime_bytes=lifetime, now=now)
    status = await _with_telemetry_status(_status(quota, [], now), now=now, heartbeat=heartbeat)
    payload = dict(
        user_id=quota.panel_id,
        traffic_limit_bytes=0,
        traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        active_internal_squads=[],
    )
    wifi, lte = _squad_sets()
    active = (
        quota.original_status in ('ACTIVE', 'LIMITED')
        and quota.end_date > now
        and _value(getattr(panel_user, 'status', None)) in ('ACTIVE', 'LIMITED')
    )
    if active:
        payload['active_internal_squads'] = list(wifi)
        if enabled and status['accounting_state'] == 'ready' and not status['exhausted']:
            payload['active_internal_squads'] += list(lte)
        if _value(getattr(panel_user, 'status', None)) == 'LIMITED':
            payload['status'] = UserStatus.ACTIVE
    updated = panel_user if _panel_policy_matches(panel_user, payload) else await _write_policy(api, payload)
    quota.panel_access_cleared_at = now if not payload['active_internal_squads'] else None
    await db.commit()
    return updated


async def run_quota_cycle(subscription_service):
    """One independent polling pass; no Telegram, payment or email effects."""
    from app.database.database import AsyncSessionLocal

    if not subscription_service.is_configured:
        return
    # Also project WIFI-only when an operator disables an already-used feature.
    async with AsyncSessionLocal() as db:
        ids = list(
            (
                await db.execute(
                    select(Subscription.id)
                    .outerjoin(LteQuota, LteQuota.subscription_id == Subscription.id)
                    .where(
                        Subscription.network_category.in_(NETWORK_CATEGORIES),
                        or_(
                            LteQuota.panel_access_cleared_at.is_(None),
                            (Subscription.end_date > datetime.now(UTC))
                            & Subscription.status.in_(('active', 'trial', 'limited')),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        orphan_ids = list(
            (
                await db.execute(
                    select(LteOrphanQuota.panel_id).where(
                        or_(
                            LteOrphanQuota.panel_access_cleared_at.is_(None),
                            (LteOrphanQuota.end_date > datetime.now(UTC))
                            & LteOrphanQuota.original_status.in_(('ACTIVE', 'LIMITED')),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    async with subscription_service.get_api_client() as api:
        semaphore = asyncio.Semaphore(10)
        heartbeat = await _read_telemetry_heartbeat()

        async def reconcile_one(subscription_id):
            async with semaphore, AsyncSessionLocal() as db:
                try:
                    sub = await db.get(Subscription, subscription_id)
                    if sub is None:
                        return
                    user = await db.get(User, sub.user_id)
                    await reconcile_subscription(db, sub, api, user=user, heartbeat=heartbeat)
                except Exception as error:
                    await db.rollback()
                    logger.error('LTE quota reconciliation failed', subscription_id=subscription_id, error=str(error))

        async def reconcile_orphan(panel_id):
            async with semaphore, AsyncSessionLocal() as db:
                try:
                    quota = await db.get(LteOrphanQuota, panel_id)
                    if quota is not None:
                        await reconcile_orphan_quota(db, quota, api, heartbeat=heartbeat)
                except Exception as error:
                    await db.rollback()
                    logger.error('Orphan LTE reconciliation failed', panel_id=panel_id, error=str(error))

        await asyncio.gather(
            *(reconcile_one(subscription_id) for subscription_id in ids),
            *(reconcile_orphan(panel_id) for panel_id in orphan_ids),
        )
