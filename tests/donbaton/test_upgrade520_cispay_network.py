"""CisPay cannot bypass network billing, including delayed and repeated charges."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.cabinet.routes.subscription_modules import cispay_recurrent as api
from app.config import settings
from app.database.models import (
    CasheraSubscription,
    CisPaySubscription,
    CisPaySubscriptionCharge,
    LavaSubscription,
    PaidTrafficResetIntent,
    PlategaSubscription,
    PromoGroup,
    Subscription,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
    tariff_promo_groups,
)
from app.handlers.subscription import cispay_recurrent as telegram
from app.localization.texts import get_texts
from app.services import cispay_recurrent_checkout as checkout, cispay_recurrent_service as service
from app.services.cispay_recurrent import cispay_tariff_terms
from app.services.cispay_service import CisPayConfig
from tests.fixtures.sqlite_memory import memory_session


REMOTE = '00000000-0000-4000-8000-000000000002'
CHARGE = '00000000-0000-4000-8000-000000000003'
TABLES = [
    model.__table__
    for model in (
        User,
        PromoGroup,
        UserPromoGroup,
        Tariff,
        Subscription,
        CisPaySubscription,
        CisPaySubscriptionCharge,
        Transaction,
        PaidTrafficResetIntent,
        CasheraSubscription,
        LavaSubscription,
        PlategaSubscription,
    )
] + [tariff_promo_groups]


@pytest.fixture
def boundaries(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop')
    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(User, 'get_primary_promo_group', lambda self: None)
    policy = AsyncMock(
        return_value={
            'enabled': True,
            'reason': None,
            'checkout_policy': {'min_amount_kopeks': 100, 'max_amount_kopeks': 1000000},
        }
    )
    monkeypatch.setattr(checkout, 'get_cispay_recurrent_policy', policy)
    monkeypatch.setattr(telegram, 'get_cispay_recurrent_policy', policy)
    post = AsyncMock()
    monkeypatch.setattr(service.cispay_service, 'create_subscription', post)
    monkeypatch.setattr(service.cispay_service, 'snapshot', lambda: CisPayConfig(shop_id='shop', api_key='test'))
    side_effects = AsyncMock()
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', side_effects)
    monkeypatch.setattr('app.cabinet.routes.websocket.cabinet_ws_manager.send_to_user', AsyncMock())
    monkeypatch.setattr(
        'app.services.notification_delivery_service.notification_delivery_service.notify_balance_topup', AsyncMock()
    )
    monkeypatch.setattr(
        'app.services.notification_delivery_service.notification_delivery_service.notify_autopay_success', AsyncMock()
    )
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.admin_notification_service.AdminNotificationService.send_admin_notification', notify
    )
    delivery = AsyncMock()
    monkeypatch.setattr(service, 'deliver_cispay_charge', delivery)
    return SimpleNamespace(post=post, notify=notify, side_effects=side_effects, delivery=delivery)


async def seed(db, *, tariff_category=None, subscription_category=None, status=None, trial=False):
    user = User(id=1, telegram_id=1, status='active', balance_kopeks=2000, language='ru')
    tariff = Tariff(
        id=1,
        name='Test',
        device_limit=3,
        device_price_kopeks=1000,
        max_device_limit=10,
        network_category=tariff_category,
        traffic_limit_gb=0,
        period_prices={'30': 10000},
    )
    db.add_all([user, tariff])
    await db.flush()
    target = Subscription(
        id=1,
        user_id=1,
        tariff_id=1,
        tariff=tariff,
        status='active',
        is_trial=trial,
        device_limit=3,
        traffic_limit_gb=0,
        network_category=subscription_category,
        start_date=datetime.now(UTC),
        end_date=datetime.now(UTC) + timedelta(days=10),
    )
    db.add(target)
    await db.flush()
    agreement = None
    if status is not None:
        agreement = CisPaySubscription(
            id=1,
            user_id=1,
            owner_id=1,
            subscription_id=1,
            shop_id='shop',
            provider_id=None if status == 'CREATING' else REMOTE,
            checkout_id=str(uuid4()),
            purpose='Test',
            status=status,
            redirect_url='https://pay.example/test',
            snapshot={
                'tariff_id': 1,
                'device_limit': 3,
                'period_days': 30,
                'amount_kopeks': 10000,
                'tariff_terms': cispay_tariff_terms(tariff),
                'trial_source': {'tariff_id': 1, 'device_limit': 3} if trial else None,
            },
        )
        db.add(agreement)
    await db.commit()
    return user, tariff, target, agreement


def payment():
    return dict(
        id=CHARGE,
        subscription_id=REMOTE,
        store_id='shop',
        status='PAID',
        amount=10000,
        currency='RUB',
        payment_method='SBP',
        is_sandbox=False,
        paid_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('source', ['tariff', 'subscription'])
async def test_quote_and_direct_cabinet_request_reject_network_before_price_or_reservation(
    monkeypatch,
    boundaries,
    category,
    source,
):
    pricing = AsyncMock()
    reserve = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr('app.services.pricing_engine.pricing_engine.calculate_tariff_purchase_price', pricing)
    monkeypatch.setattr('app.database.crud.cispay_subscriptions.reserve_agreement', reserve)
    monkeypatch.setattr('app.services.tariff_purchase_quote.save_purchase_quote', save)
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, _, _ = await seed(
            db,
            tariff_category=category if source == 'tariff' else None,
            subscription_category=category if source == 'subscription' else None,
        )
        body = api.CisPayQuoteRequest(tariff_id=1, period_days=30, subscription_id=1)
        with pytest.raises(HTTPException) as error:
            await api.quote_cispay_recurrent(body, user=user, db=db)
        assert error.value.status_code == 409
        assert 'с баланса' in error.value.detail['code']
        if source == 'tariff':
            await db.refresh(user)
            with pytest.raises(ValueError, match='с баланса'):
                await checkout.quote_cispay_purchase(db, user=user, tariff_id=1, period_days=30, channel='telegram')
        assert await db.scalar(select(func.count()).select_from(CisPaySubscription)) == 0
    pricing.assert_not_awaited()
    reserve.assert_not_awaited()
    save.assert_not_awaited()
    boundaries.post.assert_not_awaited()


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
async def test_cached_quote_is_revalidated_before_reservation(monkeypatch, boundaries, category):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, _, _ = await seed(db)
        terms = await checkout._terms(
            db,
            user=user,
            tariff_id=1,
            period_days=30,
            device_limit=None,
            subscription_id=1,
            channel='cabinet',
        )
        tariff.network_category = category
        await db.commit()
        monkeypatch.setattr('app.services.tariff_purchase_quote.load_purchase_quote', AsyncMock(return_value=terms))
        with pytest.raises(HTTPException) as error:
            await api.purchase_cispay_recurrent(
                api.CisPayPurchaseRequest(quote_id=uuid4(), checkout_id=uuid4()), user=user, db=db
            )
        assert error.value.status_code == 409
        assert 'с баланса' in error.value.detail['code']
        assert await db.scalar(select(func.count()).select_from(CisPaySubscription)) == 0
    boundaries.post.assert_not_awaited()


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('status', ['CREATING', 'CREATION_UNKNOWN', 'PENDING', 'ACTIVE'])
async def test_create_and_checkout_retry_cannot_return_network_payment_url(monkeypatch, boundaries, category, status):
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, _, agreement = await seed(db, subscription_category=category, status=status)
        quote_id = str(uuid4())
        agreement.snapshot = dict(agreement.snapshot, quote_id=quote_id, channel='cabinet')
        await db.commit()
        with pytest.raises(ValueError, match='с баланса'):
            await service.create_cispay_agreement(db, attempt_id=1)
        with pytest.raises(ValueError, match='с баланса'):
            await checkout.purchase_cispay_quote(
                db,
                user=user,
                quote_id=quote_id,
                checkout_id=agreement.checkout_id,
                channel='cabinet',
            )
    boundaries.post.assert_not_awaited()


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('source', ['tariff', 'subscription'])
@pytest.mark.parametrize('trial', [False, True])
async def test_late_charge_credits_balance_once_without_extending_or_resetting(
    monkeypatch,
    boundaries,
    category,
    source,
    trial,
):
    extend = Mock()
    reset = AsyncMock()
    monkeypatch.setattr(Subscription, 'extend_subscription', extend)
    monkeypatch.setattr('app.services.traffic_base_grant_service.stage_paid_base_grant', reset)
    monkeypatch.setattr(
        'app.database.crud.subscription.extend_subscription', AsyncMock(side_effect=AssertionError('network extension'))
    )
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, target, agreement = await seed(
            db,
            tariff_category=category if source == 'tariff' else None,
            subscription_category=category if source == 'subscription' else None,
            status='ACTIVE',
            trial=trial,
        )
        before = target.end_date
        payload = payment()
        assert await service.process_cispay_subscription_charge(db, payload, bot=Mock())
        assert await service.process_cispay_subscription_charge(db, payload, bot=Mock())
        await db.refresh(user)
        await db.refresh(target)
        await db.refresh(agreement)
        assert user.balance_kopeks == 12000
        assert target.end_date == before and target.device_limit == 3 and target.is_trial == trial
        assert agreement.status == 'CANCEL_PENDING'
        charge = await db.scalar(select(CisPaySubscriptionCharge))
        assert charge.status == 'BALANCE'
        tx = await db.get(Transaction, charge.transaction_id)
        assert tx.type == 'deposit' and tx.amount_kopeks == 10000
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 1
        assert await db.scalar(select(func.count()).select_from(PaidTrafficResetIntent)) == 0
    extend.assert_not_called()
    reset.assert_not_awaited()
    boundaries.delivery.assert_not_awaited()
    boundaries.notify.assert_awaited_once()
    boundaries.side_effects.assert_awaited_once()


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
async def test_network_pending_agreement_can_be_seen_and_cancelled(monkeypatch, boundaries, category):
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, target, agreement = await seed(db, subscription_category=category, status='PENDING')
        state = await checkout.get_cispay_agreement_state(db, user_id=1, subscription_id=1)
        assert state['redirect_url'] is None
        rows = await telegram.cispay_menu_rows(db, user, target, get_texts('ru'))
        assert rows[0][0].callback_data == 'cisrec:menu:1'
        cancel = AsyncMock(
            return_value={
                'id': REMOTE,
                'customer_id': 'user-1',
                'amount': 10000,
                'currency': 'RUB',
                'payment_method': 'SBP',
                'interval_days': 30,
                'status': 'CANCELLED',
            }
        )
        monkeypatch.setattr(service.cispay_service, 'cancel_subscription', cancel)
        result = await api.cancel_cispay_recurrent(subscription_id=1, user=user, db=db)
        assert result['status'] == 'CANCELLED'
        await db.refresh(agreement)
        assert agreement.status == 'CANCELLED'
        cancel.assert_awaited_once()
    boundaries.post.assert_not_awaited()


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
async def test_network_offer_hidden_and_live_agreement_queued_for_cancellation(monkeypatch, boundaries, category):
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, target, _ = await seed(db, tariff_category=category)
        assert not telegram.tariff_has_cispay_period(tariff)
        assert await telegram.cispay_menu_rows(db, user, target, get_texts('ru')) == []
        db.add(
            CisPaySubscription(
                id=1,
                user_id=1,
                owner_id=1,
                subscription_id=1,
                shop_id='shop',
                provider_id=REMOTE,
                checkout_id=str(uuid4()),
                purpose='Test',
                status='ACTIVE',
                snapshot={'tariff_id': 1, 'device_limit': 3, 'period_days': 30, 'amount_kopeks': 10000},
            )
        )
        await db.commit()
        await service._queue_changed_agreement(db, 1)
        assert (await db.get(CisPaySubscription, 1)).status == 'CANCEL_PENDING'


async def test_ordinary_checkout_still_reaches_provider(monkeypatch, boundaries):
    boundaries.post.return_value = {
        'id': REMOTE,
        'customer_id': 'user-1',
        'amount': 10000,
        'currency': 'RUB',
        'payment_method': 'SBP',
        'interval_days': 30,
        'status': 'PENDING',
        'payment_url': 'https://pay.example/test',
    }
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, _, _ = await seed(db, status='CREATING')
        result = await service.create_cispay_agreement(db, attempt_id=1)
        assert result['status'] == 'PENDING' and result['redirect_url'] == 'https://pay.example/test'
    boundaries.post.assert_awaited_once()


async def test_ordinary_late_charge_still_extends_the_subscription(monkeypatch, boundaries):
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr('app.services.grace_access_echo.undo_grace_overlay_echo', AsyncMock())
    monkeypatch.setattr('app.services.recurrent_amount.resolve_true_renewal_amount', AsyncMock(return_value=10000))
    stage = AsyncMock()
    monkeypatch.setattr('app.services.traffic_base_grant_service.stage_paid_base_grant', stage)
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, target, _ = await seed(db, status='ACTIVE')
        before = target.end_date
        assert await service.process_cispay_subscription_charge(db, payment(), bot=Mock())
        assert target.end_date == before + timedelta(days=30)
        assert user.balance_kopeks == 2000
        assert (await db.scalar(select(CisPaySubscriptionCharge))).status == 'APPLIED'
        assert (await db.scalar(select(Transaction))).type == 'subscription_payment'
    boundaries.delivery.assert_awaited_once()
    boundaries.notify.assert_not_awaited()
    stage.assert_awaited_once()


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
async def test_old_telegram_purchase_callback_does_not_open_network_checkout(monkeypatch, boundaries, category):
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, _, _ = await seed(db, subscription_category=category)
        callback = SimpleNamespace(
            data='tariff_cispay:1', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
        )
        state = AsyncMock()
        state.get_data.return_value = {'selected_tariff_id': 1, 'selected_period': 30, 'subscription_id': 1}
        await telegram.purchase_cispay_from_tariff(callback, user, db, state)
        assert callback.answer.await_args.kwargs['show_alert'] is True
        callback.message.edit_text.assert_not_awaited()
        state.update_data.assert_not_awaited()
    boundaries.post.assert_not_awaited()
