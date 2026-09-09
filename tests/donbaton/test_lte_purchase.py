"""LTE purchases use real balance, bucket and receipt writes in one SQLite transaction.

Only external delivery and post-payment notifications are replaced. These tests
exercise rollback and durable retries, not PostgreSQL row-lock concurrency.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    LteQuota,
    LteTopup,
    PaymentMethod,
    PromoGroup,
    Subscription,
    Tariff,
    Transaction,
    TransactionType,
    User,
    UserPromoGroup,
    tariff_promo_groups,
)
from app.services import lte_topup_purchase_service as purchases
from app.services.lte_quota_service import initialize_quota
from app.services.remnawave_retry_queue import remnawave_retry_queue
from app.services.subscription_service import SubscriptionService
from tests.fixtures.sqlite_memory import memory_session


WIFI = '00000000-0000-4000-8000-000000000101'
LTE = '00000000-0000-4000-8000-000000000102'
GIB = 1024**3
START_BALANCE = 50_000
PACKAGE_PRICE = 7_900
REQUEST_ID = 'purchase-retry-0001'
TABLES = (
    User.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    Subscription.__table__,
    LteQuota.__table__,
    LteTopup.__table__,
    Transaction.__table__,
)


@pytest.fixture(autouse=True)
def external_boundaries(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    effects = AsyncMock()
    sync = AsyncMock(return_value=SimpleNamespace(id=101))
    enqueue = Mock()
    monkeypatch.setattr(purchases, 'emit_transaction_side_effects', effects)
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', sync)
    monkeypatch.setattr(remnawave_retry_queue, 'enqueue', enqueue)
    return SimpleNamespace(effects=effects, sync=sync, enqueue=enqueue)


async def load_purchase_objects(db, subscription_id=1):
    user = (
        await db.execute(
            select(User)
            .where(User.id == 1)
            .options(
                selectinload(User.promo_group),
                selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    subscription = await db.get(Subscription, subscription_id, populate_existing=True)
    return user, subscription


async def seed(db, **subscription_overrides):
    now = datetime.now(UTC)
    user = User(
        id=1,
        telegram_id=901001,
        first_name='LTE purchase test',
        status='active',
        balance_kopeks=START_BALANCE,
        remnawave_id=101,
        promo_group=None,
        user_promo_groups=[],
    )
    tariff = Tariff(
        id=1,
        name='Test Wi-Fi + LTE',
        network_category='wifi_lte',
        traffic_limit_gb=0,
        lte_traffic_limit_gb=60,
        lte_period_days=30,
        allowed_squads=[WIFI, LTE],
        period_prices={'30': 29_900},
        is_daily=False,
        custom_days_enabled=False,
        custom_traffic_enabled=False,
        is_trial_available=False,
        show_in_gift=False,
        traffic_topup_enabled=True,
        traffic_topup_packages={'20': PACKAGE_PRICE, '60': 19_900},
    )
    db.add_all([user, tariff])
    await db.flush()
    subscription = Subscription(
        id=1,
        user_id=1,
        tariff_id=tariff.id,
        network_category='wifi_lte',
        status='active',
        is_trial=False,
        end_date=now + timedelta(days=15),
        connected_squads=[WIFI, LTE],
        remnawave_id=101,
    )
    db.add(subscription)
    await db.flush()
    quota = await initialize_quota(db, subscription, tariff, now=now)
    # An exhausted base allowance makes the newly purchased amount observable.
    quota.used_bytes = quota.limit_bytes
    quota.base_used_bytes = quota.limit_bytes
    quota.accounting_state = 'ready'
    quota.counter_panel_id = 101
    quota.counter_bytes = quota.limit_bytes
    quota.last_observed_at = now
    for key, value in subscription_overrides.items():
        setattr(subscription, key, value)
    await db.commit()
    return await load_purchase_objects(db)


async def persisted_state(db):
    balance = await db.scalar(select(User.balance_kopeks).where(User.id == 1))
    buckets = list((await db.execute(select(LteTopup).order_by(LteTopup.id))).scalars())
    receipts = list((await db.execute(select(Transaction).order_by(Transaction.id))).scalars())
    return balance, buckets, receipts


async def buy(db, user, subscription, **overrides):
    arguments = dict(gb=20, request_id=REQUEST_ID, expected_price_kopeks=PACKAGE_PRICE)
    arguments.update(overrides)
    return await purchases.purchase_lte_topup(db, user, subscription, **arguments)


@pytest.mark.asyncio
async def test_debit_bucket_and_receipt_roll_back_together_on_failure(monkeypatch, external_boundaries):
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db)
        real_create_transaction = purchases.create_transaction

        async def fail_after_real_receipt(*args, **kwargs):
            await real_create_transaction(*args, **kwargs)
            # All three real SQL writes exist inside the uncommitted transaction.
            assert await db.scalar(select(User.balance_kopeks).where(User.id == 1)) == START_BALANCE - PACKAGE_PRICE
            assert await db.scalar(select(func.count()).select_from(LteTopup)) == 1
            assert await db.scalar(select(func.count()).select_from(Transaction)) == 1
            raise RuntimeError('injected failure after receipt flush')

        monkeypatch.setattr(purchases, 'create_transaction', fail_after_real_receipt)
        with pytest.raises(RuntimeError, match='after receipt flush'):
            await buy(db, user, subscription)

        balance, buckets, receipts = await persisted_state(db)
        assert balance == START_BALANCE
        assert buckets == []
        assert receipts == []
        external_boundaries.effects.assert_not_awaited()
        external_boundaries.sync.assert_not_awaited()

        # A retry after a rolled-back attempt must succeed with the same key.
        monkeypatch.setattr(purchases, 'create_transaction', real_create_transaction)
        user, subscription = await load_purchase_objects(db)
        result = await buy(db, user, subscription)
        assert result['success'] is True
        assert result['replayed'] is False
        assert result['new_balance_kopeks'] == START_BALANCE - PACKAGE_PRICE


@pytest.mark.asyncio
async def test_duplicate_request_survives_new_session_without_second_debit(monkeypatch, external_boundaries):
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db)
        first = await buy(db, user, subscription)
        assert first['replayed'] is False
        assert first['lte_quota']['remaining_bytes'] == 20 * GIB
        await db.rollback()  # end the read transaction before opening another session

        async with AsyncSession(bind=db.bind, expire_on_commit=False, autoflush=False) as retry_db:
            retry_user, retry_subscription = await load_purchase_objects(retry_db)
            second = await buy(retry_db, retry_user, retry_subscription)
            assert second['replayed'] is True
            assert second['amount_paid_kopeks'] == PACKAGE_PRICE
            assert second['new_balance_kopeks'] == START_BALANCE - PACKAGE_PRICE
            balance, buckets, receipts = await persisted_state(retry_db)

            assert balance == START_BALANCE - PACKAGE_PRICE
            assert len(buckets) == len(receipts) == 1
            assert buckets[0].amount_bytes == 20 * GIB
            assert buckets[0].used_bytes == 0
            assert buckets[0].expires_at - buckets[0].created_at == timedelta(days=30)
            assert receipts[0].amount_kopeks == -PACKAGE_PRICE
            assert receipts[0].type == TransactionType.SUBSCRIPTION_PAYMENT.value
            assert receipts[0].payment_method == PaymentMethod.BALANCE.value
            assert buckets[0].operation_id == receipts[0].external_id == f'lte-topup:1:{REQUEST_ID}'

        external_boundaries.effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_reused_request_with_another_package_is_409_without_another_charge(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db)
        await buy(db, user, subscription)
        user, subscription = await load_purchase_objects(db)

        with pytest.raises(purchases.LtePurchaseError) as raised:
            await buy(db, user, subscription, gb=60, expected_price_kopeks=19_900)

        assert raised.value.status == 409
        assert raised.value.code == 'request_conflict'
        balance, buckets, receipts = await persisted_state(db)
        assert balance == START_BALANCE - PACKAGE_PRICE
        assert len(buckets) == len(receipts) == 1
        assert buckets[0].amount_bytes == 20 * GIB


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['expired', 'disabled', 'trial'])
async def test_unavailable_subscription_does_not_charge(monkeypatch, external_boundaries, state):
    async with memory_session(monkeypatch, TABLES) as db:
        overrides = {
            'expired': {'end_date': datetime.now(UTC) - timedelta(seconds=1)},
            'disabled': {'status': 'disabled'},
            'trial': {'is_trial': True},
        }[state]
        user, subscription = await seed(db, **overrides)

        with pytest.raises(purchases.LtePurchaseError, match='действующей платной'):
            await buy(db, user, subscription)

        balance, buckets, receipts = await persisted_state(db)
        assert balance == START_BALANCE
        assert buckets == receipts == []
        external_boundaries.effects.assert_not_awaited()
        external_boundaries.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_expected_price_mismatch_is_409_without_charge(monkeypatch, external_boundaries):
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db)
        with pytest.raises(purchases.LtePurchaseError) as raised:
            await buy(db, user, subscription, expected_price_kopeks=PACKAGE_PRICE - 100)

        assert raised.value.code == 'price_changed'
        assert raised.value.status == 409
        balance, buckets, receipts = await persisted_state(db)
        assert balance == START_BALANCE
        assert buckets == receipts == []
        external_boundaries.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_none_after_real_rollback_returns_durable_purchase_and_queues_retry(
    monkeypatch,
    external_boundaries,
):
    async def panel_failure(self, db, subscription):
        # Match the production service's error boundary: rollback expires all ORM
        # objects, then it returns None instead of raising the panel exception.
        await db.rollback()

    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', panel_failure)
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db)
        result = await buy(db, user, subscription)

        assert result['success'] is True
        assert result['replayed'] is False
        assert result['new_balance_kopeks'] == START_BALANCE - PACKAGE_PRICE
        assert result['lte_quota']['remaining_bytes'] == 20 * GIB
        external_boundaries.enqueue.assert_called_once_with(subscription_id=1, user_id=1, action='update')
        balance, buckets, receipts = await persisted_state(db)
        assert balance == START_BALANCE - PACKAGE_PRICE
        assert len(buckets) == len(receipts) == 1
        external_boundaries.effects.assert_awaited_once()
