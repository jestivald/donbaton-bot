"""Paid legacy preservation across a real ledger, purchase and access projection."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import LteOrphanQuota, LteQuota, LteTopup, Subscription, Tariff, TrafficPurchase, User
from app.external.remnawave_api import UserStatus
from app.services.lte_accounting_service import advance_legacy_state, ceil_units, legacy_budget, timestamp
from app.services.lte_quota_service import (
    GIB,
    LtePolicyError,
    activate_legacy_quota,
    activate_orphan_quota,
    activate_pending_trial_quota,
    apply_panel_policy,
    credit_topup,
    get_quota_status,
    import_legacy_quota,
    import_orphan_quota,
    initialize_quota,
    initialize_trial_quota,
    observe_counter,
    reconcile_orphan_quota,
    reconcile_subscription,
    run_quota_cycle,
    update_entitlement,
    validate_legacy_purchase_transition,
    validate_legacy_renewal,
)
from tests.donbaton.test_lte_quota import LTE, NOW, TABLES as BASIC_TABLES, WIFI, sample, tariff
from tests.fixtures.sqlite_memory import memory_session


TABLES = [*BASIC_TABLES, TrafficPurchase.__table__, LteOrphanQuota.__table__]
EPOCH = 'physical-lte-cutover-test'
BASELINE = 9_123_456_789_012_345
IMPORT = dict(migration_id='synthetic-cutover', plan_sha256='a' * 64, counter_epoch=EPOCH)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)


async def legacy_seed(
    db,
    *,
    base_gb=17,
    used_bytes=170,
    paid_until=None,
    grants=None,
    status='active',
    panel_status='ACTIVE',
    strategy='MONTH_ROLLING',
    devices=3,
    custom=False,
    is_trial=False,
):
    end = paid_until or NOW + timedelta(days=90)
    old = Tariff(id=1, name='Old plan', traffic_limit_gb=base_gb, device_limit=devices, period_prices={'30': 29900})
    target = tariff(id=9, device_limit=3)
    user = User(id=1, telegram_id=123, first_name='Synthetic', status='active', remnawave_id=11)
    db.add_all([old, target, user])
    await db.flush()
    grants = grants or []
    purchased = sum(amount for amount, _ in grants)
    sub = Subscription(
        id=1,
        user_id=1,
        tariff_id=1,
        status=status,
        is_trial=is_trial,
        device_limit=devices,
        traffic_limit_gb=base_gb + purchased if base_gb else 0,
        purchased_traffic_gb=purchased,
        end_date=end,
        remnawave_id=11,
        connected_squads=['00000000-0000-4000-8000-000000000099'],
        traffic_reset_at=min((expiry for _, expiry in grants), default=None),
    )
    db.add(sub)
    await db.flush()
    rows = []
    for amount, expiry in grants:
        row = TrafficPurchase(
            subscription_id=sub.id,
            traffic_gb=amount,
            created_at=NOW - timedelta(days=10),
            expires_at=expiry,
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    snapshot = {
        'version': 1,
        'captured_at': NOW.isoformat(),
        'divisor': 17,
        'reset_timezone': 'UTC',
        'renewal_requires_expiry': custom,
        'panel': {
            'id': 11,
            'status': panel_status,
            'created_at': (NOW - timedelta(days=400)).isoformat(),
            'expire_at': end.isoformat(),
            'limit_bytes': sub.traffic_limit_gb * GIB,
            'used_bytes': used_bytes,
            'lifetime_bytes': BASELINE,
            'reset_strategy': strategy,
            'last_reset_at': (NOW - timedelta(days=5)).isoformat(),
            'active_internal_squads': list(sub.connected_squads),
        },
        'subscription': {
            name: getattr(sub, name)
            for name in (
                'id',
                'user_id',
                'tariff_id',
                'status',
                'is_trial',
                'device_limit',
                'traffic_limit_gb',
                'purchased_traffic_gb',
            )
        },
        'grants': [
            {
                'id': row.id,
                'amount_units': row.traffic_gb * GIB,
                'created_at': row.created_at.isoformat(),
                'expires_at': row.expires_at.isoformat(),
            }
            for row in rows
        ],
    }
    snapshot['subscription'].update(
        end_date=end.isoformat(),
        traffic_reset_at=sub.traffic_reset_at.isoformat() if sub.traffic_reset_at else None,
    )
    await db.commit()
    return sub, target, snapshot, rows


async def activate(db, sub, target, snapshot):
    quota = await import_legacy_quota(db, sub, target, snapshot=snapshot, **IMPORT, now=NOW)
    await activate_legacy_quota(
        db,
        sub,
        migration_id=IMPORT['migration_id'],
        counter_epoch=EPOCH,
        panel_id=11,
        lifetime_bytes=BASELINE,
        now=NOW,
    )
    await db.commit()
    return quota


@pytest.mark.asyncio
async def test_import_preserves_exact_snapshot_and_requires_confirmed_epoch(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, used_bytes=17 * GIB - 16)
        original = deepcopy(snapshot)
        quota = await import_legacy_quota(db, sub, target, snapshot=snapshot, **IMPORT, now=NOW)
        assert sub.end_date.isoformat() == snapshot['subscription']['end_date']
        assert sub.device_limit == snapshot['subscription']['device_limit']
        assert sub.traffic_limit_gb == 0 and sub.network_category == 'wifi_lte'
        blocked = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW)
        assert blocked['active_internal_squads'] == [WIFI]
        assert quota.accounting_state == 'prepared'
        for changes in ({'lifetime_bytes': BASELINE + 1}, {'panel_id': 12}, {'counter_epoch': 'unknown'}):
            args = dict(migration_id=IMPORT['migration_id'], counter_epoch=EPOCH, panel_id=11, lifetime_bytes=BASELINE)
            with pytest.raises(LtePolicyError):
                await activate_legacy_quota(db, sub, **dict(args, **changes), now=NOW)
        await activate(db, sub, target, snapshot)
        ready = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW)
        assert ready['active_internal_squads'] == [WIFI, LTE]
        assert (await get_quota_status(db, sub, now=NOW))['remaining_bytes'] == 1
        exhausted = await apply_panel_policy(
            db, sub, {}, panel_user=sample(BASELINE + 1), now=NOW + timedelta(seconds=1)
        )
        assert exhausted['active_internal_squads'] == [WIFI]
        assert quota.counter_bytes == BASELINE + 1  # never divided historical lifetime
        assert quota.legacy_state['snapshot'] == original == snapshot
        # A retry after samples must not lower the checkpoint or refill the pool.
        await activate(db, sub, target, snapshot)
        assert quota.counter_bytes == BASELINE + 1
        assert legacy_budget(quota.legacy_state).remaining_bytes == 0
        assert (await db.execute(select(func.count()).select_from(LteQuota))).scalar_one() == 1


@pytest.mark.asyncio
async def test_import_rejects_stale_subscription_purchase_or_different_plan(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, grants = await legacy_seed(db, grants=[(1, NOW + timedelta(days=30))])
        sub.device_limit = 7
        with pytest.raises(LtePolicyError, match='Подписка изменилась'):
            await import_legacy_quota(db, sub, target, snapshot=snapshot, **IMPORT, now=NOW)
        sub.device_limit = 3
        grants[0].expires_at += timedelta(days=1)
        with pytest.raises(LtePolicyError, match='Докупки изменились'):
            await import_legacy_quota(db, sub, target, snapshot=snapshot, **IMPORT, now=NOW)
        grants[0].expires_at -= timedelta(days=1)
        await activate(db, sub, target, snapshot)
        with pytest.raises(LtePolicyError, match='другой'):
            await import_legacy_quota(
                db,
                sub,
                target,
                snapshot=snapshot,
                **dict(IMPORT, plan_sha256='b' * 64),
                now=NOW,
            )
        assert (await db.execute(select(func.count()).select_from(TrafficPurchase))).scalar_one() == 1


@pytest.mark.asyncio
async def test_epoch_mismatch_is_sticky_and_cannot_charge_a_mixed_delta(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db)
        quota = await activate(db, sub, target, snapshot)
        monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', 'another-epoch')
        result = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + 17), now=NOW)
        assert result['active_internal_squads'] == [WIFI]
        assert quota.counter_bytes == BASELINE
        monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + 17), now=NOW)
        assert quota.accounting_state == 'epoch_mismatch'
        assert quota.counter_bytes == BASELINE


@pytest.mark.asyncio
async def test_original_year_calendar_and_early_extension_do_not_change_paid_terms(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        original_end = NOW + timedelta(days=365)
        sub, target, snapshot, _ = await legacy_seed(db, paid_until=original_end, used_bytes=12345)
        quota = await activate(db, sub, target, snapshot)
        original_state = deepcopy(quota.legacy_state)
        sub.end_date += timedelta(days=30)
        target.lte_traffic_limit_gb = 120  # new administrative value cannot rewrite the paid snapshot
        await initialize_quota(db, sub, target, now=NOW + timedelta(days=1))
        assert quota.legacy_state == original_state
        assert quota.counter_bytes == BASELINE
        observe_counter(
            quota, [], panel_id=11, lifetime_bytes=BASELINE + 10, now=NOW + timedelta(days=100), paid_until=sub.end_date
        )
        assert not quota.legacy_state['runtime']['base_ended']
        assert quota.legacy_state['runtime']['used_units'] == 0  # missed calendar resets don't bank allowances
        assert quota.limit_bytes == 0  # the new recurring base hasn't started
        assert (await get_quota_status(db, sub, now=NOW + timedelta(days=100)))['limit_bytes'] == GIB
        observe_counter(quota, [], panel_id=11, lifetime_bytes=BASELINE + 10, now=original_end, paid_until=sub.end_date)
        assert quota.period_starts_at == original_end
        assert quota.period_ends_at == original_end + timedelta(days=30)
        assert quota.limit_bytes == 60 * GIB  # original selected future tariff snapshot
        assert quota.legacy_state['original_paid_until'] == original_end.isoformat()


@pytest.mark.asyncio
async def test_custom_devices_preserved_and_early_renewal_blocked_but_topup_allowed(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, devices=15, custom=True)
        await activate(db, sub, target, snapshot)
        with pytest.raises(LtePolicyError, match='индивидуальные'):
            await validate_legacy_renewal(db, sub, now=NOW)
        await credit_topup(db, sub, bytes_amount=20, operation_id='custom-topup', now=NOW)
        sub.device_limit = 3
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW)
        assert sub.device_limit == payload['hwid_device_limit'] == 15
        end = sub.end_date
        sub.status = 'expired'
        await update_entitlement(db, sub, target, now=end + timedelta(seconds=1))
        assert sub.device_limit == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('panel_limit,expected', [(None, 0), (0, 0), (8, 8)])
async def test_import_preserves_actual_panel_device_rights_and_original_bot_audit(monkeypatch, panel_limit, expected):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, devices=1, custom=True)
        snapshot['panel']['hwid_device_limit'] = panel_limit
        quota = await activate(db, sub, target, snapshot)
        assert quota.legacy_state['snapshot']['subscription']['device_limit'] == 1
        assert quota.legacy_state['snapshot']['panel']['hwid_device_limit'] is panel_limit
        assert sub.device_limit == expected
        sub.device_limit = 1  # A generic sync must not reduce the actual preserved right.
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW)
        assert sub.device_limit == payload['hwid_device_limit'] == expected
        end = sub.end_date
        sub.status = 'expired'
        await update_entitlement(db, sub, target, now=end + timedelta(seconds=1))
        assert sub.device_limit == target.device_limit
        assert quota.legacy_state['snapshot']['subscription']['device_limit'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('panel_limit', [-1, True, '3', 3.0])
async def test_import_rejects_ambiguous_panel_device_limits(monkeypatch, panel_limit):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db)
        snapshot['panel']['hwid_device_limit'] = panel_limit
        with pytest.raises(LtePolicyError, match='hwid_device_limit'):
            await import_legacy_quota(db, sub, target, snapshot=snapshot, **IMPORT, now=NOW)


@pytest.mark.asyncio
async def test_unlimited_is_explicit_and_cannot_sell_useless_topups(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, base_gb=0, custom=True, strategy='NO_RESET')
        await activate(db, sub, target, snapshot)
        status = await get_quota_status(db, sub, now=NOW)
        assert status['is_unlimited'] and not status['exhausted']
        assert status['period_mode'] == 'legacy_no_reset'
        assert status['period_ends_at'] == sub.end_date.isoformat()
        with pytest.raises(LtePolicyError, match='безлимитен'):
            await credit_topup(db, sub, bytes_amount=100, operation_id='unneeded', now=NOW)
        assert (await db.execute(select(func.count()).select_from(LteTopup))).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('old_status', 'panel_status', 'expected_status', 'expected_squads'),
    [
        ('limited', 'LIMITED', 'active', [WIFI]),
        ('disabled', 'DISABLED', 'disabled', []),
    ],
)
async def test_limited_keeps_wifi_and_administrative_disable_is_preserved(
    monkeypatch,
    old_status,
    panel_status,
    expected_status,
    expected_squads,
):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(
            db,
            status=old_status,
            panel_status=panel_status,
            used_bytes=17 * GIB,
        )
        await activate(db, sub, target, snapshot)
        current = sample(BASELINE)
        current.status = UserStatus(panel_status)
        api = SimpleNamespace(get_user_by_id=AsyncMock(return_value=current), update_user=AsyncMock())
        await reconcile_subscription(db, sub, api, user=await db.get(User, 1))
        assert sub.status == expected_status
        assert api.update_user.call_args.kwargs['active_internal_squads'] == expected_squads


@pytest.mark.asyncio
async def test_expired_legacy_without_valid_grants_can_buy_but_live_grants_require_import(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, _, grants = await legacy_seed(
            db,
            paid_until=NOW - timedelta(days=1),
            status='expired',
            grants=[(1, NOW + timedelta(days=5))],
        )
        with pytest.raises(LtePolicyError, match='действующие докупки'):
            await validate_legacy_purchase_transition(db, sub, target, now=NOW)
        grants[0].expires_at = NOW - timedelta(seconds=1)
        await db.flush()
        await validate_legacy_purchase_transition(db, sub, target, now=NOW)
        quota = await update_entitlement(db, sub, target, now=NOW)
        assert quota.limit_bytes == 60 * GIB and quota.accounting_state == 'pending'


@pytest.mark.asyncio
async def test_new_trial_is_physical_once_only_and_paid_conversion_starts_full_period(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, _, _ = await legacy_seed(db, is_trial=True, paid_until=NOW + timedelta(days=3))
        quota = await initialize_trial_quota(db, sub, legacy_traffic_limit_gb=20, now=NOW)
        assert quota.limit_bytes == ceil_units(20 * GIB, 17)
        assert quota.legacy_state is None
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW)
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + 17), now=NOW + timedelta(seconds=1))
        assert quota.used_bytes == 17  # new traffic is never multiplied
        await initialize_trial_quota(db, sub, legacy_traffic_limit_gb=1000, now=NOW + timedelta(seconds=2))
        assert quota.used_bytes == 17 and quota.limit_bytes == ceil_units(20 * GIB, 17)
        observe_counter(quota, [], panel_id=11, lifetime_bytes=BASELINE + 17, now=NOW + timedelta(days=100))
        assert quota.used_bytes == 17 and quota.period_ends_at == sub.end_date
        await update_entitlement(db, sub, target, now=NOW + timedelta(days=101))
        assert quota.pending_period is not None
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + 17), now=NOW + timedelta(days=101))
        assert quota.limit_bytes == 60 * GIB and quota.trial_ends_at is None
        assert quota.period_ends_at == NOW + timedelta(days=131)


@pytest.mark.asyncio
async def test_imported_trial_purchase_finishes_trial_window_without_preserving_trial_devices(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, is_trial=True, devices=1, custom=True)
        quota = await activate(db, sub, target, snapshot)
        await update_entitlement(db, sub, target, now=NOW + timedelta(seconds=1))
        sub.is_trial = False
        sub.device_limit = target.device_limit
        sub.end_date = NOW + timedelta(days=30, seconds=1)
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + 10), now=NOW + timedelta(seconds=2))
        assert quota.limit_bytes == 60 * GIB
        assert sub.device_limit == target.device_limit == 3
        assert quota.legacy_state['snapshot']['subscription']['device_limit'] == 1
        assert quota.period_starts_at == NOW + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_orphan_rights_use_no_fictitious_user_and_do_not_refill_no_reset(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, source, _ = await legacy_seed(db, strategy='NO_RESET', used_bytes=17 * GIB - 17)
        source['subscription'] = None
        source['panel']['id'] = 77
        quota = await import_orphan_quota(db, snapshot=source, **IMPORT, now=NOW)
        await activate_orphan_quota(
            db,
            quota,
            migration_id=IMPORT['migration_id'],
            counter_epoch=EPOCH,
            panel_id=77,
            lifetime_bytes=BASELINE,
            now=NOW,
        )
        current = sample(BASELINE + 1, panel_id=77)
        api = SimpleNamespace(get_user_by_id=AsyncMock(return_value=current), update_user=AsyncMock())
        await reconcile_orphan_quota(db, quota, api, now=NOW + timedelta(seconds=1))
        assert api.update_user.call_args.kwargs['active_internal_squads'] == [WIFI]
        assert quota.counter_bytes == BASELINE + 1
        assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
        assert (await db.execute(select(func.count()).select_from(Subscription))).scalar_one() == 1
        await reconcile_orphan_quota(db, quota, api, now=quota.end_date)
        assert api.update_user.call_args.kwargs['active_internal_squads'] == []
        assert quota.panel_access_cleared_at == quota.end_date


@pytest.mark.asyncio
async def test_tail_credit_is_once_only_and_charges_every_byte_after_base_expiry(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        end = NOW + timedelta(days=1)
        sub, target, snapshot, _ = await legacy_seed(
            db,
            base_gb=17,
            used_bytes=170,
            paid_until=end,
            strategy='NO_RESET',
            grants=[(17, end + timedelta(days=10))],
        )
        quota = await activate(db, sub, target, snapshot)
        advanced = advance_legacy_state(quota.legacy_state, end)
        assert legacy_budget(advanced).remaining_bytes == GIB
        assert advance_legacy_state(advanced, end) == advanced
        quota.legacy_state = advanced
        observe_counter(quota, [], panel_id=11, lifetime_bytes=BASELINE + 1, now=end + timedelta(seconds=1))
        assert legacy_budget(quota.legacy_state).remaining_bytes == GIB - 1
        assert quota.legacy_state['runtime']['base_expiry_credit_units'] == 17 * GIB
        assert quota.legacy_state['snapshot'] == snapshot


@pytest.mark.asyncio
async def test_wifi_purchase_preserves_tail_and_new_topup_then_revokes_lte_at_expiry(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        end = NOW + timedelta(days=1)
        tail_end = end + timedelta(days=10)
        sub, target, snapshot, _ = await legacy_seed(
            db,
            base_gb=17,
            used_bytes=17 * GIB,
            paid_until=end,
            strategy='NO_RESET',
            grants=[(17, tail_end)],
        )
        quota = await activate(db, sub, target, snapshot)
        topup = await credit_topup(db, sub, bytes_amount=100, operation_id='carry', now=NOW)
        wifi = tariff(
            id=7,
            network_category='wifi',
            device_limit=3,
            lte_traffic_limit_gb=None,
            lte_period_days=None,
            allowed_squads=[WIFI],
            traffic_topup_enabled=False,
            traffic_topup_packages={},
        )
        db.add(wifi)
        await db.flush()
        sub.status = 'expired'
        await update_entitlement(db, sub, wifi, now=end)
        sub.end_date = end + timedelta(days=30)
        sub.status = 'active'
        sub.tariff_id = wifi.id
        result = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=end)
        assert result['active_internal_squads'] == [WIFI, LTE]
        assert quota.limit_bytes == 0
        assert (await get_quota_status(db, sub, now=end))['remaining_bytes'] == GIB + 100
        assert (await get_quota_status(db, sub, now=end))['has_carried_lte']
        # The old tail expires sooner than the new purchased bucket.
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + 1), now=end + timedelta(seconds=1))
        assert legacy_budget(quota.legacy_state).remaining_bytes == GIB - 1
        assert topup.used_bytes == 0
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + 1), now=tail_end)
        assert (await get_quota_status(db, sub, now=tail_end))['remaining_bytes'] == 100
        ended = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + 1), now=NOW + timedelta(days=30))
        assert ended['active_internal_squads'] == [WIFI]
        assert not (await get_quota_status(db, sub, now=NOW + timedelta(days=30)))['has_carried_lte']


@pytest.mark.asyncio
async def test_calendar_reset_refills_only_old_pool_and_clears_tail_credit(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        end = NOW + timedelta(hours=1)
        sub, target, snapshot, _ = await legacy_seed(
            db,
            base_gb=17,
            used_bytes=17 * GIB,
            paid_until=end,
            grants=[(17, NOW + timedelta(days=40))],
        )
        quota = await activate(db, sub, target, snapshot)
        reset = timestamp(quota.legacy_state['runtime']['next_reset_at'])
        sub.end_date += timedelta(days=30)
        topup = await credit_topup(db, sub, bytes_amount=100, operation_id='new-standalone', now=NOW)
        topup.used_bytes = 25
        observe_counter(quota, [topup], panel_id=11, lifetime_bytes=BASELINE, now=end, paid_until=sub.end_date)
        assert quota.limit_bytes == 60 * GIB
        assert quota.legacy_state['runtime']['base_expiry_credit_units'] == 17 * GIB
        observe_counter(quota, [topup], panel_id=11, lifetime_bytes=BASELINE, now=reset, paid_until=sub.end_date)
        assert quota.legacy_state['runtime']['base_expiry_credit_units'] == 0
        assert quota.legacy_state['runtime']['used_units'] == 0
        assert legacy_budget(quota.legacy_state).remaining_bytes == GIB
        assert topup.used_bytes == 25
        assert quota.period_starts_at == end  # old reset doesn't refill the independent new base


@pytest.mark.asyncio
async def test_pending_trial_starts_saved_duration_at_payment_and_retry_never_refills(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _, _ = await legacy_seed(db, is_trial=True, status='pending', paid_until=NOW + timedelta(days=3))
        quota = await initialize_trial_quota(db, sub, legacy_traffic_limit_gb=20, now=NOW)
        before_payment = await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW)
        assert before_payment['active_internal_squads'] == []
        assert quota.counter_bytes is None
        paid_at = NOW + timedelta(days=10)
        assert await activate_pending_trial_quota(db, sub, now=paid_at)
        assert sub.start_date == quota.period_starts_at == paid_at
        assert sub.end_date == quota.trial_ends_at == paid_at + timedelta(days=3)
        assert sub.status == 'trial'
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=paid_at)
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE + 20), now=paid_at + timedelta(seconds=1))
        assert await activate_pending_trial_quota(db, sub, now=paid_at + timedelta(days=1))
        assert sub.end_date == paid_at + timedelta(days=3)
        assert quota.used_bytes == 20


@pytest.mark.asyncio
async def test_ordinary_expired_activation_settles_unobserved_paid_topup_before_new_base(monkeypatch):
    from tests.donbaton.test_lte_quota import seed

    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, target = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(100), now=NOW)
        await apply_panel_policy(db, sub, {}, panel_user=sample(200), now=NOW + timedelta(seconds=1))
        topup = await credit_topup(db, sub, bytes_amount=80, operation_id='last-unobserved', now=NOW)
        sub.end_date = NOW - timedelta(seconds=1)
        sub.status = 'expired'
        await update_entitlement(db, sub, target, now=NOW + timedelta(seconds=2))
        sub.end_date = NOW + timedelta(days=30)
        sub.status = 'active'
        assert quota.counter_bytes == 200
        await apply_panel_policy(db, sub, {}, panel_user=sample(260), now=NOW + timedelta(seconds=3))
        assert topup.used_bytes == 60
        assert quota.limit_bytes == 60 * GIB
        assert quota.used_bytes == 0 and quota.base_used_bytes == 0
        await apply_panel_policy(db, sub, {}, panel_user=sample(260), now=NOW + timedelta(seconds=4))
        assert topup.used_bytes == 60  # duplicate sample cannot charge twice or repeat the paid grant


@pytest.mark.asyncio
async def test_expired_old_grant_drop_is_deferred_until_calendar_reset(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        expired_at = NOW - timedelta(hours=1)
        sub, target, snapshot, rows = await legacy_seed(
            db,
            base_gb=17,
            used_bytes=20 * GIB,
            grants=[(17, expired_at)],
        )
        quota = await activate(db, sub, target, snapshot)
        # This expired purchase is still installed in the exact live cap/cache.
        state = advance_legacy_state(quota.legacy_state, NOW)
        assert state['runtime']['installed_grant_ids'] == [rows[0].id]
        assert state['runtime']['limit_units'] == 34 * GIB
        reset_at = timestamp(state['runtime']['next_reset_at'])
        state = advance_legacy_state(state, reset_at)
        assert state['runtime']['installed_grant_ids'] == []
        assert state['runtime']['limit_units'] == 17 * GIB
        assert state['runtime']['used_units'] == 0
        assert state['snapshot'] == snapshot


@pytest.mark.asyncio
async def test_no_reset_expiry_preserves_old_forced_clamp_without_resetting_new_topup(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(
            db,
            base_gb=17,
            used_bytes=20 * GIB,
            strategy='NO_RESET',
            grants=[(17, NOW - timedelta(hours=1))],
        )
        quota = await activate(db, sub, target, snapshot)
        topup = await credit_topup(db, sub, bytes_amount=100, operation_id='independent', now=NOW)
        topup.used_bytes = 30
        await apply_panel_policy(db, sub, {}, panel_user=sample(BASELINE), now=NOW)
        assert quota.legacy_state['runtime']['installed_grant_ids'] == []
        assert legacy_budget(quota.legacy_state).remaining_bytes == GIB
        assert topup.used_bytes == 30


@pytest.mark.asyncio
async def test_panel_millisecond_precision_preserves_exact_bot_paid_end(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db)
        end = timestamp(snapshot['subscription']['end_date'])
        snapshot['panel']['expire_at'] = (end - timedelta(microseconds=951)).isoformat()
        quota = await activate(db, sub, target, snapshot)
        assert quota.legacy_state['original_paid_until'] == end.isoformat()
        assert quota.legacy_state['snapshot']['panel']['expire_at'] != end.isoformat()


@pytest.mark.asyncio
async def test_orphan_cannot_project_after_user_fallback_link_claims_the_panel_identity(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, source, _ = await legacy_seed(db)
        source['subscription'] = None
        source['panel']['id'] = 77
        quota = await import_orphan_quota(db, snapshot=source, **IMPORT, now=NOW)
        user = await db.get(User, 1)
        user.remnawave_id = 77
        api = SimpleNamespace(get_user_by_id=AsyncMock(), update_user=AsyncMock())
        with pytest.raises(LtePolicyError, match='передайте ledger'):
            await reconcile_orphan_quota(db, quota, api, now=NOW)
        api.get_user_by_id.assert_not_awaited()
        api.update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_cleanup_retries_outage_then_stops_polling_until_reactivation(monkeypatch):
    from app.database import database

    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(
            db,
            status='expired',
            panel_status='EXPIRED',
            paid_until=NOW - timedelta(days=1),
        )
        quota = await activate(db, sub, target, snapshot)
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(database, 'AsyncSessionLocal', maker)
        panel = sample(BASELINE)
        panel.status = UserStatus.EXPIRED
        api = SimpleNamespace(
            get_user_by_id=AsyncMock(side_effect=RuntimeError('synthetic panel outage')), update_user=AsyncMock()
        )

        @asynccontextmanager
        async def client():
            yield api

        service = SimpleNamespace(is_configured=True, get_api_client=client)
        await run_quota_cycle(service)
        await db.refresh(quota)
        assert quota.panel_access_cleared_at is None
        await db.commit()
        api.get_user_by_id.side_effect = None
        api.get_user_by_id.return_value = panel
        await run_quota_cycle(service)
        await db.refresh(quota)
        assert quota.panel_access_cleared_at is not None
        await db.commit()
        await run_quota_cycle(service)
        assert api.get_user_by_id.await_count == 2  # failed attempt plus successful cleanup
        sub.status = 'active'
        sub.end_date = NOW + timedelta(days=30)
        await db.commit()
        panel.status = UserStatus.ACTIVE
        await run_quota_cycle(service)
        assert api.get_user_by_id.await_count == 3
