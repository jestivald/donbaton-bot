"""Real ledger persistence and access transitions, without a live panel/payment."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import LteQuota, LteTopup, PromoGroup, Subscription, Tariff, User, tariff_promo_groups
from app.external.remnawave_api import TrafficLimitStrategy, UserStatus
from app.services.lte_quota_service import (
    LtePolicyError,
    advance_period,
    apply_panel_policy,
    consume_bytes,
    credit_topup,
    get_quota_status,
    initialize_quota,
    observe_counter,
    reconcile_subscription,
    update_entitlement,
    validate_subscription_transition,
    validate_tariff_for_sale,
)
from tests.fixtures.sqlite_memory import memory_session


WIFI = '00000000-0000-4000-8000-000000000001'
LTE = '00000000-0000-4000-8000-000000000002'
NOW = datetime.now(UTC).replace(microsecond=0)
TABLES = [
    User.__table__,
    PromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    Subscription.__table__,
    LteQuota.__table__,
    LteTopup.__table__,
]


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)


def tariff(**overrides):
    values = dict(
        name='Wi-Fi + LTE',
        network_category='wifi_lte',
        traffic_limit_gb=0,
        lte_traffic_limit_gb=60,
        lte_period_days=30,
        allowed_squads=[WIFI, LTE],
        period_prices={'30': 29900},
        is_daily=False,
        custom_traffic_enabled=False,
        is_trial_available=False,
        show_in_gift=False,
        custom_days_enabled=False,
        traffic_topup_enabled=True,
        traffic_topup_packages={'10': 5900},
    )
    values.update(overrides)
    return Tariff(**values)


async def seed(db, **sub_overrides):
    plan = tariff()
    user = User(id=1, telegram_id=123, first_name='Test', status='active', remnawave_id=11)
    db.add_all([user, plan])
    await db.flush()
    values = dict(
        id=1,
        user_id=1,
        status='active',
        is_trial=False,
        tariff_id=plan.id,
        end_date=NOW + timedelta(days=90),
        connected_squads=[WIFI, LTE],
        remnawave_id=11,
        remnawave_short_id='lte001',
    )
    values.update(sub_overrides)
    sub = Subscription(**values)
    db.add(sub)
    await db.flush()
    quota = await initialize_quota(db, sub, plan, now=NOW)
    quota.limit_bytes = 100  # deliberately tiny real ledger to exercise exhaustion
    await db.commit()
    return sub, quota, plan


def sample(lifetime, *, panel_id=11, current=None):
    return SimpleNamespace(
        id=panel_id,
        status=UserStatus.ACTIVE,
        user_traffic=SimpleNamespace(lifetime_used_traffic_bytes=lifetime, used_traffic_bytes=current or 0),
    )


@pytest.mark.asyncio
async def test_exhaustion_preserves_wifi_across_reset_retry_and_topup(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await seed(db)
        payload = {'status': UserStatus.ACTIVE, 'traffic_limit_bytes': 999, 'active_internal_squads': ['stale']}
        pending = await apply_panel_policy(db, sub, payload, now=NOW)
        assert pending['active_internal_squads'] == [WIFI]
        initial = await apply_panel_policy(db, sub, payload, panel_user=sample(1000), now=NOW)
        assert initial['active_internal_squads'] == [WIFI, LTE]
        exhausted = await apply_panel_policy(db, sub, payload, panel_user=sample(1100), now=NOW + timedelta(seconds=30))
        assert exhausted['active_internal_squads'] == [WIFI]
        assert exhausted['status'] == UserStatus.ACTIVE
        assert exhausted['traffic_limit_bytes'] == 0
        assert exhausted['traffic_limit_strategy'] == TrafficLimitStrategy.NO_RESET
        await db.commit()
        # Restart / manual current-counter reset / duplicate sample cannot refill.
        await db.refresh(quota)
        for _ in range(2):
            again = await apply_panel_policy(
                db, sub, payload, panel_user=sample(1100, current=0), now=NOW + timedelta(seconds=60)
            )
            assert again['active_internal_squads'] == [WIFI]
            assert quota.used_bytes == 100
        first = await credit_topup(db, sub, bytes_amount=40, operation_id='payment:1', now=NOW)
        second = await credit_topup(db, sub, bytes_amount=40, operation_id='payment:1', now=NOW)
        assert first.id == second.id
        restored = await apply_panel_policy(db, sub, payload, panel_user=sample(1100), now=NOW + timedelta(seconds=90))
        assert restored['active_internal_squads'] == [WIFI, LTE]
        assert (await get_quota_status(db, sub, now=NOW + timedelta(seconds=90)))['remaining_bytes'] == 40


@pytest.mark.asyncio
async def test_pending_paid_state_survives_locked_reread_with_autoflush_disabled(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        await apply_panel_policy(db, sub, {}, panel_user=sample(30), now=NOW + timedelta(seconds=1))
        await apply_panel_policy(db, sub, {}, panel_user=sample(50), now=NOW + timedelta(seconds=2))
        assert quota.used_bytes == 50
        await db.commit()
        await db.refresh(quota)
        assert quota.used_bytes == 50


@pytest.mark.asyncio
async def test_regression_and_changed_identity_fail_closed_for_lte_only(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(1000), now=NOW)
        await apply_panel_policy(db, sub, {}, panel_user=sample(1040), now=NOW + timedelta(seconds=1))
        regressed = await apply_panel_policy(db, sub, {}, panel_user=sample(10), now=NOW + timedelta(seconds=2))
        assert regressed['active_internal_squads'] == [WIFI]
        assert quota.accounting_state == 'regressed'
        assert quota.counter_bytes == 1040
        recovered = await apply_panel_policy(db, sub, {}, panel_user=sample(1050), now=NOW + timedelta(seconds=3))
        assert recovered['active_internal_squads'] == [WIFI, LTE]
        assert quota.used_bytes == 50
        changed = await apply_panel_policy(
            db, sub, {}, panel_user=sample(1050, panel_id=22), now=NOW + timedelta(seconds=4)
        )
        assert changed['active_internal_squads'] == [WIFI]
        assert quota.accounting_state == 'identity_changed'
        assert quota.counter_panel_id == 11


@pytest.mark.asyncio
async def test_early_renewal_and_tariff_edits_do_not_refill(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plan = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        await apply_panel_policy(db, sub, {}, panel_user=sample(100), now=NOW + timedelta(seconds=1))
        anchor = quota.period_ends_at
        sub.end_date += timedelta(days=90)
        plan.lte_traffic_limit_gb = 120  # administrative edit doesn't rewrite paid snapshot
        await initialize_quota(db, sub, plan, now=NOW + timedelta(days=10))
        assert quota.limit_bytes == 100
        assert quota.used_bytes == 100
        assert quota.period_ends_at == anchor


@pytest.mark.asyncio
async def test_topup_validity_survives_period_rollover_and_expired_activation(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        bucket = await credit_topup(
            db, sub, bytes_amount=40, operation_id='payment:rollover', now=NOW + timedelta(days=20)
        )
        await apply_panel_policy(db, sub, {}, panel_user=sample(100), now=NOW + timedelta(days=30))
        assert quota.period_ends_at == NOW + timedelta(days=60)
        assert bucket.expires_at == NOW + timedelta(days=50)
        assert bucket.used_bytes == 0
        # Topup expires before this recurring base, so it is spent first.
        await apply_panel_policy(db, sub, {}, panel_user=sample(110), now=NOW + timedelta(days=30, seconds=1))
        assert bucket.used_bytes == 10
        assert quota.base_used_bytes == 0
        wifi = tariff(
            network_category='wifi',
            lte_traffic_limit_gb=None,
            lte_period_days=None,
            allowed_squads=[WIFI],
            traffic_topup_enabled=False,
            traffic_topup_packages={},
        )
        with pytest.raises(LtePolicyError, match='после окончания'):
            await update_entitlement(db, sub, wifi, now=NOW + timedelta(days=31))
        sub.end_date = NOW - timedelta(seconds=1)
        await update_entitlement(db, sub, wifi, now=NOW + timedelta(days=31))
        sub.end_date = NOW + timedelta(days=61)
        carried = await apply_panel_policy(db, sub, {}, panel_user=sample(110), now=NOW + timedelta(days=31))
        assert carried['active_internal_squads'] == [WIFI, LTE]
        assert bucket.amount_bytes - bucket.used_bytes == 30
        assert quota.period_ends_at == NOW + timedelta(days=61)
        assert (await get_quota_status(db, sub, now=NOW + timedelta(days=31)))['has_carried_lte'] is True
        ended = await apply_panel_policy(db, sub, {}, panel_user=sample(110), now=NOW + timedelta(days=50))
        assert ended['active_internal_squads'] == [WIFI]


@pytest.mark.asyncio
async def test_expired_paid_activation_requires_fresh_baseline_and_preserves_topups(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plan = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(1000), now=NOW)
        await apply_panel_policy(db, sub, {}, panel_user=sample(1100), now=NOW + timedelta(seconds=1))
        await credit_topup(db, sub, bytes_amount=40, operation_id='renewal-extra', now=NOW)
        sub.end_date = NOW - timedelta(seconds=1)
        await update_entitlement(db, sub, plan, now=NOW + timedelta(seconds=2))
        sub.end_date = NOW + timedelta(days=30)
        await db.commit()
        assert quota.accounting_state == 'pending'
        pending = await apply_panel_policy(db, sub, {}, now=NOW + timedelta(seconds=3))
        assert pending['active_internal_squads'] == [WIFI]
        restored = await apply_panel_policy(db, sub, {}, panel_user=sample(1110), now=NOW + timedelta(seconds=4))
        assert restored['active_internal_squads'] == [WIFI, LTE]
        assert quota.used_bytes == 0
        assert (await get_quota_status(db, sub, now=NOW + timedelta(seconds=4)))['topup_bytes'] == 40


@pytest.mark.asyncio
@pytest.mark.parametrize('sub_status', ['disabled', 'expired'])
async def test_topup_and_sampling_never_reactivate_disabled_subscription(monkeypatch, sub_status):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db, status=sub_status)
        await credit_topup(db, sub, bytes_amount=40, operation_id='payment:no-reactivate', now=NOW)
        payload = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert payload['active_internal_squads'] == []
        assert sub.status == sub_status


@pytest.mark.asyncio
async def test_stale_telemetry_and_disabled_accounting_preserve_wifi(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        stale = await apply_panel_policy(db, sub, {}, now=NOW + timedelta(minutes=5))
        assert stale['active_internal_squads'] == [WIFI]
        assert (await get_quota_status(db, sub, now=NOW + timedelta(minutes=5)))['accounting_state'] == 'stale'
        monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', False)
        disabled = await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        assert disabled['active_internal_squads'] == [WIFI]


@pytest.mark.asyncio
async def test_conflicting_operation_id_does_not_grant_more_bytes(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, _, _ = await seed(db)
        await credit_topup(db, sub, bytes_amount=40, operation_id='same', now=NOW)
        with pytest.raises(LtePolicyError, match='другой покупки'):
            await credit_topup(db, sub, bytes_amount=80, operation_id='same', now=NOW)
        assert (await get_quota_status(db, sub, now=NOW))['topup_bytes'] == 40


@pytest.mark.asyncio
async def test_legacy_payload_unchanged_even_when_feature_enabled():
    sub = SimpleNamespace(network_category=None)
    payload = {'traffic_limit_bytes': 1024**4, 'active_internal_squads': ['legacy']}
    assert await apply_panel_policy(None, sub, payload) is payload


def test_sales_require_separate_verified_squads(monkeypatch):
    validate_tariff_for_sale(tariff())
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', WIFI)
    with pytest.raises(LtePolicyError, match='непересекающиеся'):
        validate_tariff_for_sale(tariff())


def test_sales_require_operator_accounting_confirmation(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', False)
    with pytest.raises(LtePolicyError):
        validate_tariff_for_sale(tariff())
    validate_tariff_for_sale(tariff(network_category=None))


def test_paid_legacy_transition_requires_migration():
    with pytest.raises(LtePolicyError, match='оплаченные остатки'):
        validate_subscription_transition(SimpleNamespace(network_category=None, is_trial=False), tariff())
    with pytest.raises(LtePolicyError, match='миграции'):
        validate_subscription_transition(SimpleNamespace(network_category='wifi_lte'), tariff(network_category=None))


def test_period_is_fixed_days_across_february_and_downtime():
    start = datetime(2028, 1, 31, 12, tzinfo=UTC)
    quota = SimpleNamespace(
        period_days=30,
        period_starts_at=start,
        period_ends_at=start + timedelta(days=30),
        limit_bytes=100,
        used_bytes=73,
        base_used_bytes=73,
    )
    advance_period(quota, start + timedelta(days=95))
    assert quota.period_starts_at == start + timedelta(days=90)
    assert quota.period_ends_at == start + timedelta(days=120)
    assert quota.used_bytes == 0


def test_earliest_expiry_counts_upload_download_sum_once():
    quota = SimpleNamespace(period_ends_at=NOW + timedelta(days=30), limit_bytes=100, used_bytes=0, base_used_bytes=0)
    bucket = SimpleNamespace(id=1, amount_bytes=50, used_bytes=0, expires_at=NOW + timedelta(days=5))
    consume_bytes(quota, [bucket], 20 + 40, NOW)
    assert bucket.used_bytes == 50
    assert quota.base_used_bytes == 10
    assert quota.used_bytes == 60


def test_negative_or_noninteger_telemetry_cannot_refill():
    quota = SimpleNamespace(accounting_state='ready', counter_bytes=1000, counter_panel_id=11)
    observe_counter(quota, [], panel_id=11, lifetime_bytes=-1, now=NOW)
    assert quota.accounting_state == 'missing'
    assert quota.counter_bytes == 1000


@pytest.mark.asyncio
async def test_uninitialized_managed_tariff_cannot_fall_through_to_upstream():
    sub = SimpleNamespace(network_category=None, tariff=tariff())
    with pytest.raises(LtePolicyError, match='инициализированной'):
        await apply_panel_policy(None, sub, {'traffic_limit_bytes': 0, 'active_internal_squads': [WIFI, LTE]})


@pytest.mark.asyncio
async def test_single_panel_user_renewal_projects_exhausted_lte(monkeypatch):
    from app.services.subscription_service import SubscriptionService

    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, plan = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        await apply_panel_policy(db, sub, {}, panel_user=sample(100), now=NOW + timedelta(seconds=1))
        await db.commit()
        sub.tariff = plan
        user = await db.get(User, sub.user_id)
        api = SimpleNamespace(
            get_user_by_id=AsyncMock(return_value=sample(100)), update_user=AsyncMock(return_value=sample(100))
        )
        await SubscriptionService()._create_or_update_remnawave_user_single(
            api,
            user,
            sub,
            db=db,
            user_tag=None,
            hwid_limit=None,
            ext_squad_uuid=None,
            reset_traffic=False,
            reset_reason=None,
        )
        payload = api.update_user.call_args.kwargs
        assert payload['status'] == UserStatus.ACTIVE
        assert payload['traffic_limit_bytes'] == 0
        assert payload['traffic_limit_strategy'] == TrafficLimitStrategy.NO_RESET
        assert payload['active_internal_squads'] == [WIFI]
        assert quota.used_bytes == 100
        assert sub.connected_squads == [WIFI, LTE]


@pytest.mark.asyncio
async def test_worker_commits_usage_and_only_removes_lte(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        await db.commit()
        user = await db.get(User, sub.user_id)
        api = SimpleNamespace(
            get_user_by_id=AsyncMock(return_value=sample(100)), update_user=AsyncMock(return_value=sample(100))
        )
        await reconcile_subscription(db, sub, api, user=user)
        assert api.update_user.call_args.kwargs['active_internal_squads'] == [WIFI]
        await db.refresh(quota)
        assert quota.used_bytes == 100
        assert sub.status == 'active'


@pytest.mark.asyncio
async def test_worker_skips_identical_panel_patch_but_commits_new_usage(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(1000), now=NOW)
        await db.commit()
        current = sample(1012)
        current.traffic_limit_bytes = 0
        current.traffic_limit_strategy = TrafficLimitStrategy.NO_RESET
        current.active_internal_squads = [{'uuid': LTE}, {'uuid': WIFI}]  # order is immaterial
        api = SimpleNamespace(get_user_by_id=AsyncMock(return_value=current), update_user=AsyncMock())
        returned = await reconcile_subscription(db, sub, api, user=await db.get(User, sub.user_id))
        assert returned is current
        api.update_user.assert_not_awaited()
        await db.refresh(quota)
        assert quota.used_bytes == 12 and quota.counter_bytes == 1012
        assert quota.last_observed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [UserStatus.ACTIVE, UserStatus.DISABLED, UserStatus.LIMITED])
async def test_worker_repairs_changed_access_and_preserves_administrative_disable(monkeypatch, status):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, quota, _ = await seed(db)
        await apply_panel_policy(db, sub, {}, panel_user=sample(0), now=NOW)
        await db.commit()
        current = sample(100)
        current.status = status
        current.traffic_limit_bytes = 0
        current.traffic_limit_strategy = TrafficLimitStrategy.NO_RESET
        current.active_internal_squads = [{'uuid': WIFI}, {'uuid': LTE}]
        api = SimpleNamespace(
            get_user_by_id=AsyncMock(return_value=current), update_user=AsyncMock(return_value=current)
        )
        await reconcile_subscription(db, sub, api, user=await db.get(User, sub.user_id))
        api.update_user.assert_awaited_once()
        payload = api.update_user.call_args.kwargs
        assert payload['active_internal_squads'] == ([] if status == UserStatus.DISABLED else [WIFI])
        if status == UserStatus.LIMITED:
            assert payload['status'] == UserStatus.ACTIVE
        elif status == UserStatus.DISABLED:
            assert payload.get('status', UserStatus.DISABLED) == UserStatus.DISABLED
        await db.refresh(quota)
        assert quota.used_bytes == 100
