"""DonBaton LTE accounting, independent of Remnawave's global limit.

All mutations lock the subscription before the quota; callers commit together
with their purchase. Samples are cumulative *logical bytes*, after the staged
WIFI=0/LTE=1 cutover. A period boundary belongs to the first successful sample
after that boundary: its outstanding delta is charged before rollover. This
customer-favouring sampling tolerance is deliberately not a last-byte guarantee.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import or_, select

from app.config import settings
from app.database.models import LteOrphanQuota, LteQuota, LteTopup, Subscription, Tariff, TrafficPurchase, User
from app.external.remnawave_api import TrafficLimitStrategy, UserStatus
from app.services import lte_telemetry_alerts
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
    live_telemetry_proof,
    physical_counter_delta,
    telemetry_within_grace,
    timestamp,
    valid_telemetry_heartbeat,
)


logger = structlog.get_logger(__name__)
GIB = 1024**3
# Здесь важно только членство, но порядок держим как в donbaton_catalog.
NETWORK_CATEGORIES = ('wifi_lte', 'wifi')
_UNSET_HEARTBEAT = object()
_UNRESOLVED_MEMORY = object()
_TELEMETRY_REDIS_TIMEOUT = 2


@dataclass(frozen=True, slots=True)
class TelemetryEvidence:
    """One cycle's resolution of producer evidence, shared by every subscription.

    `payload` is the live lease exactly as read, still re-checked for age per
    subscription: a slow cycle must not extend a heartbeat. `last_valid` is the
    remembered proof behind the grace window, resolved once so an outage cannot
    turn one Redis read into one per user.

    The rest describes the cycle for the owner's alerts. `proven`/`missing`
    partition the configured nodes by the current lease (no current lease: all
    missing). `failed` says why a missing route is missing when a current lease
    tells: the producer's code, or `unmonitored` for a route it does not check.
    `state` is the gate decision: `live` (enough routes proven), `grace` (none,
    but the last proof is recent; `grace_remaining` seconds left) or `closed`.
    """

    payload: object = None
    last_valid: object = _UNRESOLVED_MEMORY
    proven: tuple = ()
    missing: tuple = ()
    failed: dict = dataclass_field(default_factory=dict)
    state: str = 'unknown'
    grace_remaining: int | None = None


@dataclass(frozen=True, slots=True)
class LteNoticeCandidate:
    """A customer (or a panel-only right) whose LTE just crossed a notice line.

    Produced by the polling cycle only after the notice mark was committed with
    the sample, so a failed delivery may lose this period's message but can
    never repeat it. ``subscription_id`` is set for a bot customer; a panel-only
    right carries ``panel_id`` / ``panel_username`` for the admin chat instead.
    """

    event: str  # 'low' | 'exhausted'
    remaining_bytes: int
    topup_bytes: int = 0
    subscription_id: int | None = None
    panel_id: int | None = None
    panel_username: str | None = None


@dataclass(frozen=True, slots=True)
class LteNoticeMarks:
    """Notice marks after a decision, plus the event to announce (if any)."""

    low_sent_at: datetime | None
    exhausted_sent_at: datetime | None
    event: str | None = None


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
    if not nodes:
        raise LtePolicyError('Для подтверждения LTE-учёта нужен UUID хотя бы одной проверенной LTE-ноды')
    return sorted(nodes)


_REPORTED_TELEMETRY_CONFIG = set()


def _warn_once(event, **fields):
    """A configuration mismatch is logged once per distinct value, not every cycle."""
    key = (event, tuple(sorted(fields.items())))
    if key not in _REPORTED_TELEMETRY_CONFIG:
        _REPORTED_TELEMETRY_CONFIG.add(key)
        logger.warning(event, **fields)


def _telemetry_min_routes(nodes):
    """Operator's minimum of proven routes; above the node count it is clamped, never fatal."""
    requested = settings.DONBATON_LTE_TELEMETRY_MIN_ROUTES
    if type(requested) is not int or requested < 1:
        _warn_once('LTE telemetry minimum routes is invalid, using 1', requested=str(requested))
        return 1
    if requested > len(nodes):
        _warn_once(
            'LTE telemetry minimum routes exceeds configured nodes, clamped', requested=requested, nodes=len(nodes)
        )
    return requested


async def _read_telemetry_heartbeat(*, now=None):
    from app.utils.cache import cache

    epoch = settings.DONBATON_LTE_ACCOUNTING_EPOCH
    if not epoch:
        return None
    try:
        async with asyncio.timeout(_TELEMETRY_REDIS_TIMEOUT):
            return await cache.get(f'donbaton:lte:telemetry:{epoch}')
    except TimeoutError:
        logger.warning('LTE telemetry Redis read timed out')
        return None


def _telemetry_memory_key(epoch):
    return f'donbaton:lte:telemetry:{epoch}:last-valid'


async def _remember_valid_heartbeat(payload, *, now):
    """Anchor the grace window in Redis so it survives a bot restart."""
    from app.utils.cache import cache

    # The memory must expire on its own: a forgotten key may not resurrect LTE
    # after a long outage, so it dies once no grace decision could rest on it.
    expire = settings.DONBATON_LTE_TELEMETRY_GRACE_SECONDS + settings.DONBATON_LTE_MAX_SAMPLE_AGE_SECONDS
    lease = {'payload': payload, 'seen_at': now.isoformat()}
    try:
        async with asyncio.timeout(_TELEMETRY_REDIS_TIMEOUT):
            await cache.set(_telemetry_memory_key(settings.DONBATON_LTE_ACCOUNTING_EPOCH), lease, expire=expire)
    except TimeoutError:
        logger.warning('LTE telemetry grace memory write timed out')
    return lease


async def _read_remembered_heartbeat():
    from app.utils.cache import cache

    epoch = settings.DONBATON_LTE_ACCOUNTING_EPOCH
    if not epoch:
        return None
    try:
        async with asyncio.timeout(_TELEMETRY_REDIS_TIMEOUT):
            return await cache.get(_telemetry_memory_key(epoch))
    except TimeoutError:
        logger.warning('LTE telemetry grace memory read timed out')
        return None


def _live_heartbeat_is_valid(payload, *, now):
    try:
        nodes = _telemetry_node_ids()
        return valid_telemetry_heartbeat(
            payload,
            epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
            node_ids=nodes,
            now=now,
            max_age_seconds=settings.DONBATON_LTE_MAX_SAMPLE_AGE_SECONDS,
            min_routes=_telemetry_min_routes(nodes),
        )
    except LtePolicyError:
        return False


def _memory_is_within_grace(last_valid, *, now):
    try:
        nodes = _telemetry_node_ids()
        return telemetry_within_grace(
            last_valid,
            epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
            node_ids=nodes,
            now=now,
            grace_seconds=settings.DONBATON_LTE_TELEMETRY_GRACE_SECONDS,
            min_routes=_telemetry_min_routes(nodes),
        )
    except LtePolicyError:
        return False


def _telemetry_routes(payload, *, now):
    """Per-route reading of the cycle's lease; `accepted` tells whether it opens LTE."""
    try:
        nodes = _telemetry_node_ids()
    except LtePolicyError:
        return False, {}
    proof = live_telemetry_proof(
        payload,
        epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        node_ids=nodes,
        now=now,
        max_age_seconds=settings.DONBATON_LTE_MAX_SAMPLE_AGE_SECONDS,
        min_routes=_telemetry_min_routes(nodes),
    )
    if proof is None:
        return False, {'missing': tuple(nodes)}
    if proof.extra:
        _warn_once('LTE telemetry lease names unconfigured nodes, ignored', nodes=proof.extra)
    failed = {node_id: proof.failed.get(node_id, 'unmonitored') for node_id in proof.missing}
    return proof.accepted, {'proven': proof.proven, 'missing': proof.missing, 'failed': failed}


def _lte_accounting_active():
    return bool(
        settings.DONBATON_LTE_ENABLED
        and settings.DONBATON_LTE_ACCOUNTING_READY
        and settings.DONBATON_LTE_ACCOUNTING_EPOCH
    )


def _grace_remaining_seconds(last_valid, *, now):
    try:
        elapsed = (timestamp(now) - timestamp(last_valid['seen_at'])).total_seconds()
        return max(0, int(settings.DONBATON_LTE_TELEMETRY_GRACE_SECONDS - elapsed))
    except (LegacyAccountingError, KeyError, TypeError):
        return None


async def _resolve_telemetry_evidence(*, now=None):
    """Read the live lease once per cycle and resolve the grace anchor with it.

    The memory is refreshed here, where the lease is actually read, so a cycle
    over hundreds of subscriptions still performs one Redis write, and the
    single warning below reports one outage instead of one per user. A partial
    v2 lease with enough proven routes is live telemetry like a complete one.
    """
    now = _now(now)
    payload = await _read_telemetry_heartbeat(now=now)
    accepted, routes = _telemetry_routes(payload, now=now)
    if settings.DONBATON_LTE_TELEMETRY_GRACE_SECONDS <= 0 or not settings.DONBATON_LTE_ACCOUNTING_EPOCH:
        if not accepted:
            _log_lte_closed(last_valid=None)
        return TelemetryEvidence(payload=payload, last_valid=None, state='live' if accepted else 'closed', **routes)
    if accepted:
        last_valid = await _remember_valid_heartbeat(payload, now=now)
        return TelemetryEvidence(payload=payload, last_valid=last_valid, state='live', **routes)
    last_valid = await _read_remembered_heartbeat()
    if _memory_is_within_grace(last_valid, now=now):
        remaining = _grace_remaining_seconds(last_valid, now=now)
        logger.warning(
            'LTE telemetry missing, within grace',
            epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
            grace_seconds=settings.DONBATON_LTE_TELEMETRY_GRACE_SECONDS,
            remaining_seconds=remaining,
        )
        return TelemetryEvidence(
            payload=payload, last_valid=last_valid, state='grace', grace_remaining=remaining, **routes
        )
    _log_lte_closed(last_valid=last_valid)
    return TelemetryEvidence(payload=payload, last_valid=last_valid, state='closed', **routes)


def _log_lte_closed(*, last_valid):
    """Closing LTE used to be silent: 'within grace' lines just stopped."""
    if not _lte_accounting_active():
        return
    seen_at = last_valid.get('seen_at') if isinstance(last_valid, dict) else None
    logger.warning(
        'LTE telemetry missing beyond grace, LTE closed',
        epoch=settings.DONBATON_LTE_ACCOUNTING_EPOCH,
        grace_seconds=settings.DONBATON_LTE_TELEMETRY_GRACE_SECONDS,
        last_valid_seen_at=seen_at if isinstance(seen_at, str) else None,
    )


async def _with_telemetry_status(status, *, now, heartbeat=_UNSET_HEARTBEAT):
    if heartbeat is _UNSET_HEARTBEAT:
        evidence = await _resolve_telemetry_evidence(now=now)
    elif isinstance(heartbeat, TelemetryEvidence):
        evidence = heartbeat
    else:
        evidence = TelemetryEvidence(payload=heartbeat)
    healthy = _live_heartbeat_is_valid(evidence.payload, now=now)
    if not healthy and settings.DONBATON_LTE_TELEMETRY_GRACE_SECONDS > 0:
        last_valid = evidence.last_valid
        if last_valid is _UNRESOLVED_MEMORY:
            last_valid = await _read_remembered_heartbeat()
        healthy = _memory_is_within_grace(last_valid, now=now)
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


#: Текст правила владельца: смена сетевого тарифа — это обычная покупка.
TARIFF_SWITCH_AS_PURCHASE = (
    'Смена тарифа Wi-Fi / Wi-Fi + LTE — это покупка нового тарифа на полный срок: '
    'оставшиеся дни текущего тарифа сохраняются и добавляются к новому сроку.'
)


def is_paid_tariff_switch(subscription, tariff) -> bool:
    """Покупка ДРУГОГО сетевого тарифа поверх живой сетевой подписки.

    Модель владельца: доплаты и пропорции нет — клиент покупает новый тариф на
    полный период по обычной цене, остаток текущего срока сохраняется и
    добавляется к новому (``extend_subscription``, ветка ``is_tariff_change``).
    Разрешено только для пары «управляемый → управляемый»: legacy↔managed
    по-прежнему требует явной миграции, а пробная подписка платного остатка не
    имеет.
    """
    return bool(
        settings.DONBATON_TARIFF_SWITCH_ENABLED
        and is_managed(subscription)
        and getattr(tariff, 'network_category', None) in NETWORK_CATEGORIES
        and not getattr(subscription, 'is_trial', False)
        and getattr(subscription, 'status', None) == 'active'
        and getattr(subscription, 'tariff_id', None) != getattr(tariff, 'id', None)
    )


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
        and not is_paid_tariff_switch(subscription, tariff)
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


#: Ключи runtime, которых нет в свежем ``build_legacy_state``: каждый появляется
#: только когда клиент применил оплаченное решение. ``purchased_at`` ставит
#: ``update_entitlement`` и никто не удаляет; остальные оставлены ради ledger'ов,
#: записанных до его появления (``pending_new_period`` живёт до активации,
#: ``advance_period`` заменяет его на ``new_limit_bytes``).
_LEGACY_PURCHASE_MARKERS = ('purchased_at', 'pending_new_period', 'new_limit_bytes', 'trial_converted_at')


def legacy_purchase_recorded(state) -> bool:
    """Клиент сам оплатил новые условия поверх перенесённых.

    Проверяем присутствие ключа, а не истинность значения: тариф «Только Wi-Fi»
    не содержит LTE-пакета (``lte_traffic_limit_gb in (None, 0)``), поэтому
    ``update_entitlement`` кладёт ``pending_new_period.limit_bytes = 0``, а
    ``advance_period`` переносит ``new_limit_bytes = 0`` и удаляет pending.
    Проверка на истинность приняла бы такую покупку за её отсутствие и вернула
    бы запрет автопродления навсегда.
    """
    runtime = (state or {}).get('runtime') or {}
    return any(marker in runtime for marker in _LEGACY_PURCHASE_MARKERS)


def legacy_autopay_blocked(quota, *, now=None) -> bool:
    """Фоновое автопродление не оплачивает перенесённые индивидуальные условия.

    До конца оплаченного срока действуют прежние условия, после него тариф
    выбирает сам клиент: технический target, проставленный импортом, новым
    согласием на оплату не является. Поэтому, в отличие от
    ``legacy_renewal_requires_expiry``, запрет не снимается на границе
    ``original_paid_until`` — иначе в окне «недавно истёкших» (2 часа) autopay
    успел бы молча списать деньги за технический тариф.

    Снимает запрет только явная покупка клиента (``legacy_purchase_recorded``).
    Само по себе движение ``end_date`` мимо ``update_entitlement`` — админское
    продление, бонусные дни — покупкой не является и запрет не снимает:
    оплаченное решение о новом тарифе принимает клиент. Автоматический скаффолд
    после окончания базы ставит один ``new_period_started_at``, поэтому этот
    ключ в маркеры не входит.
    """
    if legacy_renewal_requires_expiry(quota, now=now):
        return True
    state = getattr(quota, 'legacy_state', None)
    if not (state and state['snapshot']['renewal_requires_expiry']):
        return False
    if (state['snapshot']['subscription'] or {}).get('is_trial', False):
        return False
    return not legacy_purchase_recorded(state)


async def autopay_blocked_by_legacy(db, subscription, *, now=None) -> bool:
    """Обёртка предиката для фоновых циклов: читает квоту подписки без блокировки."""
    if not is_managed(subscription):
        return False
    return legacy_autopay_blocked(await _quota(db, subscription), now=now)


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


def _mark_legacy_purchase(quota, now):
    """Неубиваемо отметить в перенесённом ledger явную платную покупку клиента."""
    state = getattr(quota, 'legacy_state', None)
    if not state or state['runtime'].get('purchased_at'):
        return
    state = deepcopy(state)
    state['runtime']['purchased_at'] = _now(now).isoformat()
    quota.legacy_state = state


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
    switching = is_paid_tariff_switch(subscription, tariff)
    # A paid switch of a live subscription is handled separately below. Trial
    # conversion and same-tariff updates preserve the current period; a paid
    # activation after expiry establishes a new period.
    subscription.network_category = tariff.network_category
    subscription.traffic_limit_gb = 0
    now = _now(now)
    converting_trial = subscription.is_trial
    # Любая ветка ниже применяет уже оплаченное решение клиента, поэтому отметка
    # ставится один раз здесь и переживает все дальнейшие переносы состояния:
    # объём LTE нового тарифа может быть нулевым, и маркеры-значения потерялись
    # бы (см. legacy_purchase_recorded).
    _mark_legacy_purchase(quota, now)
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
    if switching:
        return _switch_entitlement(quota, subscription, tariff)
    quota.next_limit_bytes = (tariff.lte_traffic_limit_gb or 0) * GIB
    return quota


def _switch_entitlement(quota, subscription, tariff):
    """New plan terms start now; the paid remainder of the cycle is not reset.

    The owner's rule is a purchase, not a proration: the customer pays the full
    price of the new plan, so its LTE allowance applies to the running cycle at
    once — an upgrade tops the cycle up by the difference, a downgrade cuts it.
    ``pending_period`` deliberately stays unused here: it zeroes usage, and a
    9 → 10 → 9 loop would mint free bytes every hop.
    """
    limit_bytes = (tariff.lte_traffic_limit_gb or 0) * GIB
    subscription.device_limit = tariff.device_limit
    state = quota.legacy_state
    if state and not state['runtime']['base_ended']:
        # Old terms are paid until original_paid_until: the new allowance takes
        # over at that boundary (advance_period reads target_limit_bytes), while
        # devices change at once through apply_panel_policy's max() below.
        # legacy_state is plain JSON without MutableDict — mutate a copy.
        updated = deepcopy(state)
        updated['target_limit_bytes'] = limit_bytes
        updated['target_tariff_id'] = tariff.id
        updated['target_device_limit'] = tariff.device_limit or 0
        quota.legacy_state = updated
        return quota
    quota.limit_bytes = limit_bytes
    quota.next_limit_bytes = limit_bytes
    return quota


async def describe_tariff_switch(db, subscription, tariff, *, now=None) -> dict:
    """What the customer actually gets when buying another managed plan.

    One description for every surface: the bot confirmation screens, the
    cabinet and the Mini App must not each invent their own answer to "when
    does the new LTE allowance start" — in the legacy compatibility window it
    starts at the old paid end, everywhere else immediately.
    """
    from app.services.tariff_switch_policy import remaining_days_for_switch, should_reset_devices_on_switch

    now = _now(now)
    current = await db.get(Tariff, subscription.tariff_id) if getattr(subscription, 'tariff_id', None) else None
    quota = await _quota(db, subscription)
    state = getattr(quota, 'legacy_state', None)
    legacy_until = legacy_compatibility_end(state) if state and not state['runtime']['base_ended'] else None
    if legacy_until is not None and legacy_until <= now:
        legacy_until = None
    limit_gb = int(getattr(tariff, 'lte_traffic_limit_gb', None) or 0)
    device_limit = int(getattr(tariff, 'device_limit', None) or 0)
    if legacy_until is not None:
        device_limit = max(device_limit, legacy_device_limit(state))
    return {
        'mode': 'after_legacy' if legacy_until is not None else 'immediate',
        'current_category': getattr(subscription, 'network_category', None),
        'new_category': getattr(tariff, 'network_category', None),
        'device_limit_now': device_limit,
        'devices_reset': should_reset_devices_on_switch(current, tariff),
        'lte_limit_gb_now': None if legacy_until is not None else limit_gb,
        'lte_limit_gb_next': limit_gb,
        'lte_effective_at': legacy_until.isoformat() if legacy_until is not None else None,
        'legacy_until': legacy_until.isoformat() if legacy_until is not None else None,
        'remaining_days': remaining_days_for_switch(getattr(subscription, 'end_date', None), now),
    }


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
        # Inside the compatibility window the panel keeps the rights actually
        # paid for. A plan bought during that window raises devices at once and
        # never takes away what the old terms already granted.
        target_devices = quota.legacy_state.get('target_device_limit') or 0
        subscription.device_limit = max(legacy_device_limit(quota.legacy_state), target_devices)
        result['hwid_device_limit'] = subscription.device_limit
    status = await _with_telemetry_status(_status(quota, topups, now), now=now, heartbeat=heartbeat)
    subscription._lte_quota_status = status
    active = subscription.status in ('active', 'trial') and _now(subscription.end_date) > now
    active = active and _value(payload.get('status', 'ACTIVE')) not in ('DISABLED', 'EXPIRED', 'LIMITED')
    if active:
        result['active_internal_squads'] = list(wifi)
        if accounting_enabled and status['accounting_state'] == 'ready' and not status['exhausted']:
            result['active_internal_squads'] += list(lte)
    elif _value(payload.get('status', 'EXPIRED')) not in ('DISABLED', 'LIMITED'):
        from app.services.donbaton_payment_window import payment_window

        # Окно оплаты: срок кончился, но ещё N часов остаётся один сервер —
        # Telegram и наша инфраструктура, чтобы клиент дошёл до бота и заплатил.
        # Ни Wi-Fi, ни LTE. Статус и дата — тоже часть проекции: без ACTIVE и
        # даты в будущем панель погасит аккаунт сама. DISABLED в наборе полей —
        # блокировка владельца или подписки, окна там нет.
        window = payment_window(subscription, now=now)
        if window is not None:
            result.update(
                status=UserStatus.ACTIVE,
                active_internal_squads=[window.squad_uuid],
                expire_at=window.until,
            )
    return result


def low_notice_threshold_bytes() -> int:
    """«LTE почти закончился» threshold in real bytes; 0 switches that notice off."""
    return max(0, int(settings.DONBATON_LTE_LOW_NOTICE_GB * GIB))


def decide_lte_notice(
    status,
    *,
    low_sent_at,
    exhausted_sent_at,
    eligible,
    telemetry_live,
    is_trial=False,
    audience_enabled=True,
    now=None,
) -> LteNoticeMarks:
    """Decide the LTE notice marks and the one event to announce; no I/O.

    Marks are re-armed from the ledger alone: a balance above the threshold
    (top-up, new period, manual credit) clears both, and any positive balance
    clears «exhausted», so the next exhaustion is announced again. New marks
    need confirmed accounting — ``ready`` on a live telemetry lease, never the
    grace window — and a customer who really has LTE: finite and with a non-zero
    capacity. Wi-Fi-only capacity is zero: it is always ``exhausted`` in the
    ledger and must never be told so. The mark is set before sending, so a
    message goes out at most once per crossing; «exhausted» also sets «low»,
    because the low line was crossed on the way.
    """
    if status is None or status.get('is_unlimited'):
        return LteNoticeMarks(low_sent_at, exhausted_sent_at)
    remaining = int(status.get('remaining_bytes') or 0)
    threshold = low_notice_threshold_bytes()
    if remaining > threshold:
        low_sent_at = None
    if remaining > 0:
        exhausted_sent_at = None
    capacity = int(status.get('limit_bytes') or 0) + int(status.get('topup_bytes') or 0)
    confirmed = (
        telemetry_live
        and status.get('accounting_state') == 'ready'
        and settings.DONBATON_LTE_ENABLED
        and settings.DONBATON_LTE_ACCOUNTING_READY
    )
    allowed = (
        settings.DONBATON_LTE_NOTICE_ENABLED
        and audience_enabled
        and eligible
        and (not is_trial or settings.DONBATON_LTE_NOTICE_TRIALS)
    )
    if not (confirmed and allowed and capacity > 0):
        return LteNoticeMarks(low_sent_at, exhausted_sent_at)
    now = _now(now)
    if remaining <= 0:
        if settings.DONBATON_LTE_EXHAUSTED_NOTICE_ENABLED and exhausted_sent_at is None:
            return LteNoticeMarks(low_sent_at or now, now, 'exhausted')
    elif remaining <= threshold and low_sent_at is None:
        return LteNoticeMarks(now, exhausted_sent_at, 'low')
    return LteNoticeMarks(low_sent_at, exhausted_sent_at)


def _apply_notice_marks(quota, status, **decision) -> str | None:
    """Write the decided marks onto the locked ledger row; return the event."""
    marks = decide_lte_notice(
        status,
        low_sent_at=quota.low_notice_sent_at,
        exhausted_sent_at=quota.exhausted_notice_sent_at,
        **decision,
    )
    # Assign only real changes: an unchanged row must stay out of the UPDATE.
    if marks.low_sent_at is not quota.low_notice_sent_at:
        quota.low_notice_sent_at = marks.low_sent_at
    if marks.exhausted_sent_at is not quota.exhausted_notice_sent_at:
        quota.exhausted_notice_sent_at = marks.exhausted_sent_at
    return marks.event


def _lease_is_live(heartbeat, now) -> bool:
    """The cycle's own live lease, re-checked for age; the grace memory never counts.

    Grace keeps LTE open on an unconfirmed ledger for a while; a message about
    the balance must not rest on that. No evidence at all means not live.
    """
    if heartbeat is _UNSET_HEARTBEAT:
        return False
    payload = heartbeat.payload if isinstance(heartbeat, TelemetryEvidence) else heartbeat
    return _live_heartbeat_is_valid(payload, now=now)


def _subscription_notice(quota, subscription, user, kwargs, heartbeat):
    status = getattr(subscription, '_lte_quota_status', None)
    now = datetime.now(UTC)
    # The final panel projection already folds in every veto (user blocked,
    # panel disable, expiry): no squads means the customer has no access at all,
    # and «Wi-Fi keeps working» would be a false promise.
    eligible = bool(
        kwargs.get('active_internal_squads')
        and subscription.status in ('active', 'trial')
        and subscription.end_date is not None
        and _now(subscription.end_date) > now
        and user is not None
        and user.status == 'active'
    )
    event = _apply_notice_marks(
        quota,
        status,
        eligible=eligible,
        telemetry_live=_lease_is_live(heartbeat, now),
        is_trial=bool(subscription.is_trial),
        now=now,
    )
    if event is None:
        return None
    return LteNoticeCandidate(
        event=event,
        remaining_bytes=status['remaining_bytes'],
        topup_bytes=status['topup_bytes'],
        subscription_id=subscription.id,
    )


async def reconcile_subscription(db, subscription, api, *, user=None, heartbeat=_UNSET_HEARTBEAT, notices=None):
    """Serialize sample, ledger mutation and panel projection against purchases.

    With a ``notices`` list (the polling cycle) the LTE notice marks are decided
    on the same locked row and committed together with the sample; a candidate
    is appended only after that commit. Nothing is sent from here.
    """
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
        kwargs.pop('expire_at', None)  # заблокированному окна оплаты нет
    # The payment window (`expire_at` in the projection) reopens an account the
    # panel expired by the passed end date: that is exactly its job.
    window_open = 'expire_at' in kwargs
    panel_status = _value(getattr(panel_user, 'status', None))
    # Do not reactivate a panel administrator's disable; ordinary paid renewal
    # remains the authoritative path for a status change.
    if panel_status == 'DISABLED' or (panel_status == 'EXPIRED' and not window_open):
        kwargs['active_internal_squads'] = []
        if window_open:
            kwargs.pop('status', None)
            kwargs.pop('expire_at', None)
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
    candidate = None
    if quota:
        quota.panel_access_cleared_at = datetime.now(UTC) if not kwargs.get('active_internal_squads') else None
        if notices is not None:
            candidate = _subscription_notice(quota, subscription, user, kwargs, heartbeat)
    await db.commit()
    if candidate is not None:
        notices.append(candidate)
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
    user_id = fields.pop('user_id')
    # Дату пишет только окно оплаты — отдельным аргументом, чтобы общий вход
    # политики не стал лазейкой для произвольной даты.
    window_until = fields.pop('expire_at', None)
    return await patch_panel_policy(api, user_id=user_id, fields=fields, payment_window_until=window_until)


def _panel_policy_matches(panel_user, payload):
    from app.services.donbaton_payment_window import same_panel_date

    known_fields = {
        'user_id',
        'traffic_limit_bytes',
        'traffic_limit_strategy',
        'active_internal_squads',
        'status',
        'hwid_device_limit',
        'expire_at',
    }
    if not set(payload) <= known_fields:
        return False
    if payload.get('user_id') != getattr(panel_user, 'id', None):
        return False
    for field in ('traffic_limit_bytes', 'traffic_limit_strategy', 'status', 'hwid_device_limit'):
        if field in payload and _value(payload[field]) != _value(getattr(panel_user, field, None)):
            return False
    # Дата окна детерминирована (end_date + окно), поэтому совпавшая дата значит
    # «уже записано» — без этого каждый проход переписывал бы аккаунт в окне.
    if 'expire_at' in payload and not same_panel_date(payload['expire_at'], getattr(panel_user, 'expire_at', None)):
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


async def reconcile_orphan_quota(db, quota, api, *, now=None, heartbeat=_UNSET_HEARTBEAT, notices=None):
    """Preserve finite panel access without creating an application identity.

    There is nobody to message here: with ``DONBATON_LTE_NOTICE_ADMIN_ORPHANS``
    the notice candidate goes to the admin chat instead (see
    ``reconcile_subscription`` for the mark and commit order).
    """
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
    candidate = None
    if notices is not None:
        event = _apply_notice_marks(
            quota,
            status,
            eligible=bool(active and payload['active_internal_squads']),
            telemetry_live=_lease_is_live(heartbeat, now),
            audience_enabled=settings.DONBATON_LTE_NOTICE_ADMIN_ORPHANS,
            now=now,
        )
        if event is not None:
            candidate = LteNoticeCandidate(
                event=event,
                remaining_bytes=status['remaining_bytes'],
                topup_bytes=status['topup_bytes'],
                panel_id=quota.panel_id,
                panel_username=getattr(panel_user, 'username', None),
            )
    await db.commit()
    if candidate is not None:
        notices.append(candidate)
    return updated


async def run_quota_cycle(subscription_service, bot=None):
    """One independent polling pass; no payment or email effects.

    Telegram only in two bounded, non-raising ways: the owner's telemetry-route
    alerts (``lte_telemetry_alerts.report_cycle``, sent here once per pass) and the
    customers' LTE notice candidates, which are only returned — the caller sends
    them after the pass (``MonitoringService._send_lte_notices``).
    """
    from app.database.database import AsyncSessionLocal

    if not subscription_service.is_configured:
        return []
    from app.services.donbaton_payment_window import cycle_candidates

    selected_at = datetime.now(UTC)
    reachable = [
        LteQuota.panel_access_cleared_at.is_(None),
        (Subscription.end_date > selected_at) & Subscription.status.in_(('active', 'trial', 'limited')),
    ]
    # Окно оплаты держит подписку в цикле и после снятия доступа: иначе её,
    # истёкшую до включения окна, цикл уже не выбрал бы, а конец окна снять
    # было бы некому.
    window_clause = cycle_candidates(selected_at)
    if window_clause is not None:
        reachable.append(window_clause)
    # Also project WIFI-only when an operator disables an already-used feature.
    async with AsyncSessionLocal() as db:
        ids = list(
            (
                await db.execute(
                    select(Subscription.id)
                    .outerjoin(LteQuota, LteQuota.subscription_id == Subscription.id)
                    .where(
                        Subscription.network_category.in_(NETWORK_CATEGORIES),
                        or_(*reachable),
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
    # LTE notice candidates whose marks this pass committed; the caller sends them.
    notices: list[LteNoticeCandidate] = []
    async with subscription_service.get_api_client() as api:
        semaphore = asyncio.Semaphore(10)
        # Resolve the producer evidence once: every subscription below shares
        # this snapshot, and only its age is re-checked per user.
        evidence = await _resolve_telemetry_evidence()
        await lte_telemetry_alerts.report_cycle(evidence, bot=bot)

        async def reconcile_one(subscription_id):
            async with semaphore, AsyncSessionLocal() as db:
                try:
                    sub = await db.get(Subscription, subscription_id)
                    if sub is None:
                        return
                    user = await db.get(User, sub.user_id)
                    await reconcile_subscription(db, sub, api, user=user, heartbeat=evidence, notices=notices)
                except Exception as error:
                    await db.rollback()
                    logger.error('LTE quota reconciliation failed', subscription_id=subscription_id, error=str(error))

        async def reconcile_orphan(panel_id):
            async with semaphore, AsyncSessionLocal() as db:
                try:
                    quota = await db.get(LteOrphanQuota, panel_id)
                    if quota is not None:
                        await reconcile_orphan_quota(db, quota, api, heartbeat=evidence, notices=notices)
                except Exception as error:
                    await db.rollback()
                    logger.error('Orphan LTE reconciliation failed', panel_id=panel_id, error=str(error))

        await asyncio.gather(
            *(reconcile_one(subscription_id) for subscription_id in ids),
            *(reconcile_orphan(panel_id) for panel_id in orphan_ids),
        )
    return notices
