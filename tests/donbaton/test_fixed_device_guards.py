"""Paid device slots are fixed for managed plans, including imported custom terms.

Real SQLite rows exercise API/CRUD locks and the saved-cart lookup. Only UI
context resolution, Redis, panel delivery, and the debit boundary are replaced.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import devices as api
from app.cabinet.schemas.subscription import DevicePurchaseRequest
from app.config import settings
from app.database.crud import subscription as subscription_crud, user as user_crud
from app.database.models import PromoGroup, Subscription, Tariff, User, UserPromoGroup, tariff_promo_groups
from app.handlers.subscription import devices as telegram
from app.services import subscription_auto_purchase_service as autopurchase
from app.services.lte_quota_service import LtePolicyError
from tests.fixtures.sqlite_memory import memory_session


BALANCE = 50_000
TABLES = (
    User.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    Subscription.__table__,
)


async def seed(db, *, category='wifi_lte', devices=15):
    user = User(
        id=1,
        telegram_id=901001,
        first_name='Device guard fixture',
        language='ru',
        status='active',
        balance_kopeks=BALANCE,
        promo_group=None,
        user_promo_groups=[],
    )
    tariff = Tariff(
        id=1,
        name='Device guard tariff',
        network_category=category,
        traffic_limit_gb=0,
        device_limit=3,
        max_device_limit=30,
        device_price_kopeks=1000,
        period_prices={'30': 29900},
        allowed_squads=[],
    )
    db.add_all([user, tariff])
    await db.flush()
    subscription = Subscription(
        id=1,
        user_id=1,
        tariff_id=1,
        tariff=tariff,
        network_category=category,
        device_limit=devices,
        traffic_limit_gb=0,
        status='active',
        is_trial=False,
        end_date=datetime.now(UTC) + timedelta(days=20),
    )
    db.add(subscription)
    await db.commit()
    return user, subscription


@pytest.fixture
def boundaries(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    debit, save_cart, delete_cart, sync = AsyncMock(return_value=False), AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(user_crud, 'subtract_user_balance', debit)
    monkeypatch.setattr(telegram, 'subtract_user_balance', debit)
    monkeypatch.setattr(api.user_cart_service, 'save_user_cart', save_cart)
    monkeypatch.setattr(autopurchase, '_delete_cart_for_subscription', delete_cart)
    monkeypatch.setattr(api.SubscriptionService, 'update_remnawave_user', sync)
    return SimpleNamespace(debit=debit, save_cart=save_cart, delete_cart=delete_cart, sync=sync)


async def assert_unchanged(db, devices, boundaries):
    assert await db.scalar(select(User.balance_kopeks).where(User.id == 1)) == BALANCE
    assert await db.scalar(select(Subscription.device_limit).where(Subscription.id == 1)) == devices
    boundaries.debit.assert_not_awaited()
    boundaries.save_cart.assert_not_awaited()
    boundaries.sync.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('category,devices', [('wifi', 0), ('wifi_lte', 15)])
@pytest.mark.parametrize(
    'endpoint', ['purchase_devices_legacy', 'purchase_devices', 'save_devices_cart', 'reduce_devices']
)
async def test_cabinet_mutations_reject_before_debit_or_replacing_original_devices(
    monkeypatch,
    boundaries,
    category,
    devices,
    endpoint,
):
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db, category=category, devices=devices)
        monkeypatch.setattr(api, 'resolve_subscription', AsyncMock(return_value=subscription))
        request = {'new_device_limit': 2} if endpoint == 'reduce_devices' else DevicePurchaseRequest(devices=1)
        with pytest.raises(HTTPException) as caught:
            await getattr(api, endpoint)(request=request, subscription_id=1, user=user, db=db)
        assert caught.value.status_code == 400
        assert 'закреплено' in caught.value.detail
        await assert_unchanged(db, devices, boundaries)


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['get_device_price', 'get_device_reduction_info'])
async def test_cabinet_quotes_do_not_offer_purchase_or_reduce_for_imported_unlimited_device_terms(
    monkeypatch,
    boundaries,
    endpoint,
):
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db, devices=0)
        monkeypatch.setattr(api, 'resolve_subscription', AsyncMock(return_value=subscription))
        result = await getattr(api, endpoint)(subscription_id=1, user=user, db=db)
        assert result['available'] is False and result['reason_code'] == 'fixed_network_plan'
        assert result['current_device_limit'] == 0
        await assert_unchanged(db, 0, boundaries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'handler,data',
    [
        ('handle_change_devices', 'subscription_change_devices'),
        ('confirm_change_devices', 'change_devices_16'),
        ('execute_change_devices', 'confirm_change_devices_16'),
        ('confirm_add_devices', 'add_devices_1'),
    ],
)
async def test_saved_telegram_callbacks_cannot_charge_managed_device_slots(monkeypatch, boundaries, handler, data):
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db)
        monkeypatch.setattr(telegram, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
        callback = SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        await getattr(telegram, handler)(callback, user, db)
        callback.answer.assert_awaited_once()
        assert 'закреплено' in callback.answer.call_args.args[0]
        assert callback.answer.call_args.kwargs['show_alert'] is True
        callback.message.edit_text.assert_not_awaited()
        await assert_unchanged(db, 15, boundaries)


@pytest.mark.asyncio
async def test_saved_add_devices_cart_after_payment_is_discarded_without_spending_credited_balance(
    monkeypatch, boundaries
):
    async with memory_session(monkeypatch, TABLES) as db:
        user, _ = await seed(db)
        cart = {'cart_mode': 'add_devices', 'devices_to_add': 2, 'price_kopeks': 5000, 'subscription_id': 1}
        result = await autopurchase._process_single_cart(db, user, cart)
        assert result is False
        boundaries.delete_cart.assert_awaited_once_with(1, cart)
        await assert_unchanged(db, 15, boundaries)


@pytest.mark.asyncio
async def test_shared_crud_rejects_managed_but_still_updates_ordinary_legacy_subscription(monkeypatch, boundaries):
    async with memory_session(monkeypatch, TABLES) as db:
        _, subscription = await seed(db, devices=15)
        with pytest.raises(LtePolicyError, match='закреплено'):
            await subscription_crud.add_subscription_devices(db, subscription, 1)
        await assert_unchanged(db, 15, boundaries)
        subscription.network_category = None
        await db.commit()
        monkeypatch.setattr('app.services.recurrent_amount.sync_recurrent_bindings_after_price_change', AsyncMock())
        changed = await subscription_crud.add_subscription_devices(db, subscription, 1)
        assert changed.device_limit == 16
        assert await db.scalar(select(Subscription.device_limit).where(Subscription.id == 1)) == 16


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['purchase_devices_legacy', 'purchase_devices'])
async def test_ordinary_legacy_cabinet_purchase_still_reaches_debit(monkeypatch, boundaries, endpoint):
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db, category=None, devices=3)
        monkeypatch.setattr(api, 'resolve_subscription', AsyncMock(return_value=subscription))
        with pytest.raises(HTTPException) as caught:
            await getattr(api, endpoint)(request=DevicePurchaseRequest(devices=1), subscription_id=1, user=user, db=db)
        # The debit boundary deliberately returns False; legacy pricing reached it.
        assert caught.value.status_code == 402
        boundaries.debit.assert_awaited_once()
        assert await db.scalar(select(Subscription.device_limit).where(Subscription.id == 1)) == 3
