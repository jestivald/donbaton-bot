"""Cutover rejects stale offers and credits already charged invoices exactly once.

SQLite stores actual payment/transaction/balance rows. Telegram, Redis,
notifications, referral rewards, and panel delivery are the replaced boundaries.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.models import (
    LteQuota,
    PromoGroup,
    Subscription,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
    YooKassaPayment,
    tariff_promo_groups,
)
from app.handlers import stars_payments as handlers
from app.services import legacy_checkout_guard as guard, subscription_auto_purchase_service as autopurchase
from app.services.payment import stars as stars_module
from app.services.payment_service import PaymentService
from app.services.subscription_purchase_service import MiniAppSubscriptionPurchaseService, PurchaseValidationError
from tests.fixtures.sqlite_memory import memory_session


BALANCE = 12345
TABLES = (
    User.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    Subscription.__table__,
    LteQuota.__table__,
    Transaction.__table__,
    YooKassaPayment.__table__,
)


def service():
    result = PaymentService.__new__(PaymentService)
    result.bot = None
    return result


async def seed(db, *, category=None, is_trial=False):
    now = datetime.now(UTC)
    user = User(
        id=1,
        telegram_id=901001,
        first_name='Cutover payment fixture',
        language='ru',
        balance_kopeks=BALANCE,
        status='active',
        has_made_first_topup=True,
        user_promo_groups=[],
        promo_group=None,
        subscriptions=[],
    )
    tariff = Tariff(
        id=1,
        name='Old tariff',
        network_category=None,
        is_active=False,
        device_limit=3,
        traffic_limit_gb=1024,
        period_prices={'30': 19900},
        allowed_squads=[],
    )
    managed = Tariff(
        id=9,
        name='Managed tariff',
        network_category='wifi_lte',
        is_active=True,
        device_limit=3,
        traffic_limit_gb=0,
        lte_traffic_limit_gb=60,
        lte_period_days=30,
        period_prices={'30': 29900},
        allowed_squads=[],
    )
    db.add_all([user, tariff, managed])
    await db.flush()
    subscription = Subscription(
        id=1,
        user_id=1,
        tariff_id=9 if category else 1,
        network_category=category,
        device_limit=3,
        traffic_limit_gb=0 if category else 1024,
        status='pending',
        is_trial=is_trial,
        start_date=now,
        end_date=now + timedelta(days=30),
    )
    db.add(subscription)
    await db.commit()
    return user, subscription


@pytest.fixture(autouse=True)
def external_boundaries(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(type(settings), 'get_stars_rate', lambda self: 2.37)
    monkeypatch.setattr(stars_module, 'emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
    panel = AsyncMock()
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.create_remnawave_user', panel)
    cart_notification = AsyncMock()
    monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', cart_notification)
    clear_intent, delete_cart = AsyncMock(), AsyncMock()
    monkeypatch.setattr(autopurchase.user_cart_service, 'clear_topup_intent', clear_intent)
    monkeypatch.setattr(autopurchase, '_delete_cart_for_subscription', delete_cart)
    return SimpleNamespace(
        panel=panel, cart_notification=cart_notification, clear_intent=clear_intent, delete_cart=delete_cart
    )


async def assert_only_credit(db, expected, *, method):
    assert await db.scalar(select(User.balance_kopeks).where(User.id == 1)) == BALANCE + expected
    assert await db.scalar(select(Subscription.status).where(Subscription.id == 1)) == 'pending'
    transaction = (await db.execute(select(Transaction))).scalar_one()
    assert transaction.type == 'deposit' and transaction.amount_kopeks == expected
    assert transaction.payment_method == method and transaction.is_completed is True
    return transaction


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', ['simple_sub_1_1_30', 'trial_1'])
async def test_already_charged_legacy_stars_credits_actual_amount_once(monkeypatch, external_boundaries, payload):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db, is_trial=payload.startswith('trial_'))
        payment = service()
        args = dict(db=db, user_id=1, stars_amount=19, payload=payload, telegram_payment_charge_id='charge-unique-1')
        assert await payment.process_stars_payment(**args) is True
        assert await payment.process_stars_payment(**args) is True
        transaction = await assert_only_credit(db, 4503, method='telegram_stars')
        assert transaction.external_id == 'charge-unique-1'
        external_boundaries.panel.assert_not_awaited()
        external_boundaries.cart_notification.assert_not_awaited()
        external_boundaries.clear_intent.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_stars_credit_and_receipt_roll_back_together_then_retry_delivers(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        real_commit = db.commit
        fail_commit = AsyncMock(side_effect=RuntimeError('injected before commit'))
        monkeypatch.setattr(db, 'commit', fail_commit)
        args = dict(
            db=db, user_id=1, stars_amount=19, payload='simple_sub_1_1_30', telegram_payment_charge_id='charge-retry'
        )
        assert await service().process_stars_payment(**args) is False
        assert await db.scalar(select(User.balance_kopeks).where(User.id == 1)) == BALANCE
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 0
        monkeypatch.setattr(db, 'commit', real_commit)
        assert await service().process_stars_payment(**args) is True
        await assert_only_credit(db, 4503, method='telegram_stars')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['simple', 'trial'])
async def test_yookassa_retired_invoice_credits_exact_record_amount_and_links_receipt_once(
    monkeypatch, external_boundaries, kind
):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db, is_trial=kind == 'trial')
        metadata = (
            {'payment_purpose': 'simple_subscription_purchase', 'order_id': '1', 'subscription_period': '365'}
            if kind == 'simple'
            else {'type': 'trial', 'subscription_id': '1'}
        )
        row = YooKassaPayment(
            id=1,
            user_id=1,
            yookassa_payment_id='provider-fixed-1',
            amount_kopeks=19901,
            status='succeeded',
            is_paid=True,
            is_captured=True,
            test_mode=False,
            metadata_json=metadata,
        )
        db.add(row)
        await db.commit()
        payment = service()
        assert await payment._process_successful_yookassa_payment(db, row) is True
        assert await payment._process_successful_yookassa_payment(db, row) is True
        transaction = await assert_only_credit(db, 19901, method='yookassa')
        saved = await db.get(YooKassaPayment, 1, populate_existing=True)
        assert saved.transaction_id == transaction.id
        assert saved.metadata_json['donbaton_retired_invoice_credited'] is True
        assert saved.metadata_json['processing_completed'] is True
        external_boundaries.panel.assert_not_awaited()
        external_boundaries.cart_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_yookassa_failed_cutover_credit_rolls_back_receipt_link_and_balance(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        row = YooKassaPayment(
            id=1,
            user_id=1,
            yookassa_payment_id='provider-retry',
            amount_kopeks=19901,
            status='succeeded',
            is_paid=True,
            is_captured=True,
            test_mode=False,
            metadata_json={'payment_purpose': 'simple_subscription_purchase', 'order_id': '1'},
        )
        db.add(row)
        await db.commit()
        payment = service()
        mark = payment._mark_yookassa_payment_processing_completed
        monkeypatch.setattr(
            payment,
            '_mark_yookassa_payment_processing_completed',
            AsyncMock(side_effect=RuntimeError('injected before atomic commit')),
        )
        assert await payment._process_successful_yookassa_payment(db, row) is False
        assert await db.scalar(select(User.balance_kopeks).where(User.id == 1)) == BALANCE
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 0
        row = await db.get(YooKassaPayment, 1, populate_existing=True)
        assert row.transaction_id is None
        monkeypatch.setattr(payment, '_mark_yookassa_payment_processing_completed', mark)
        assert await payment._process_successful_yookassa_payment(db, row) is True
        await assert_only_credit(db, 19901, method='yookassa')


@pytest.mark.asyncio
async def test_successful_trial_handler_routes_retired_invoice_by_real_charge_id(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db, is_trial=True)
        payment = service()
        monkeypatch.setattr(handlers, 'PaymentService', lambda bot: payment)
        old_trial_handler = AsyncMock()
        monkeypatch.setattr(handlers, '_handle_trial_payment', old_trial_handler)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=901001),
            bot=None,
            successful_payment=SimpleNamespace(
                total_amount=19,
                currency='XTR',
                invoice_payload='trial_1',
                telegram_payment_charge_id='actual-charge-321',
            ),
            answer=AsyncMock(),
        )
        await handlers.handle_successful_payment(message, db, AsyncMock())
        old_trial_handler.assert_not_awaited()
        receipt = await assert_only_credit(db, 4503, method='telegram_stars')
        assert receipt.external_id == 'actual-charge-321'
        assert 'зачислены' in message.answer.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', ['simple_sub_1_1_30', 'trial_1'])
@pytest.mark.parametrize('category,allowed', [(None, False), ('wifi_lte', True)])
async def test_stars_precheckout_checks_pending_subscription_category_before_charge(
    monkeypatch, payload, category, allowed
):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db, category=category, is_trial=payload.startswith('trial_'))

        @asynccontextmanager
        async def session_scope():
            yield db

        monkeypatch.setattr('app.database.database.AsyncSessionLocal', session_scope)
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=901001), total_amount=19, invoice_payload=payload, answer=AsyncMock()
        )
        await handlers.handle_pre_checkout_query(query)
        query.answer.assert_awaited_once()
        assert query.answer.call_args.kwargs['ok'] is allowed
        if not allowed:
            assert 'устарели' in query.answer.call_args.kwargs['error_message']
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 0


@pytest.mark.asyncio
async def test_flags_off_keeps_old_invoice_and_cart_policy_without_extra_queries(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', False)
    db = AsyncMock()
    assert await guard.pending_invoice_retired(db, 1, 99) is False
    assert await guard.stars_invoice_retired(db, 1, 'trial_99') is False
    assert await guard.saved_tariff_cart_retired(db, {'tariff_id': 1, 'period_days': 365}) is False
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'cart',
    [
        {'cart_mode': 'extend', 'subscription_id': 1, 'period_days': 30},
        {'cart_mode': 'extend', 'tariff_id': 1, 'subscription_id': 1, 'period_days': 30},
        {'cart_mode': 'tariff_purchase', 'tariff_id': 1, 'period_days': 365},
        {'cart_mode': 'tariff_purchase', 'tariff_id': 9, 'period_days': 365},
        {'cart_mode': 'tariff_purchase', 'tariff_id': 9, 'period_days': 45},
    ],
)
async def test_stale_or_repriced_saved_cart_drops_intent_before_debit(monkeypatch, external_boundaries, cart):
    async with memory_session(monkeypatch, TABLES) as db:
        user, _ = await seed(db)
        debit = AsyncMock()
        monkeypatch.setattr(autopurchase, 'subtract_user_balance', debit)
        assert await autopurchase._process_single_cart(db, user, cart) is False
        debit.assert_not_awaited()
        external_boundaries.delete_cart.assert_awaited_once_with(1, cart)
        external_boundaries.clear_intent.assert_awaited_once_with(1)
        assert await db.scalar(select(User.balance_kopeks).where(User.id == 1)) == BALANCE


@pytest.mark.asyncio
async def test_current_managed_30_day_cart_is_not_rejected_as_legacy(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        assert await guard.saved_tariff_cart_retired(db, {'tariff_id': 9, 'period_days': 30}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('days', [90, 180, 360])
async def test_any_priced_period_of_managed_tariff_keeps_saved_cart(monkeypatch, days):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        tariff = await db.scalar(select(Tariff).where(Tariff.id == 9))
        tariff.period_prices = {'30': 29900, '90': 79900, '180': 129900, '360': 179900}
        await db.commit()
        assert await guard.saved_tariff_cart_retired(db, {'tariff_id': 9, 'period_days': days}) is False
        assert await guard.saved_tariff_cart_retired(db, {'tariff_id': 9, 'period_days': 45}) is True


@pytest.mark.asyncio
@pytest.mark.parametrize('identifier', [True, 1.1, 2**63, '999999999999999999999999999', '-1'])
async def test_invalid_pending_ids_are_retired_without_database_overflow(identifier):
    db = AsyncMock()
    assert await guard.pending_invoice_retired(db, 1, identifier) is True
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['_auto_extend_subscription', '_auto_purchase_tariff'])
async def test_custom_paid_terms_block_direct_autopurchase_before_price_or_debit(
    monkeypatch,
    external_boundaries,
    entrypoint,
):
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await seed(db, category='wifi_lte')
        subscription.status = 'active'
        subscription.device_limit = 15
        now = datetime.now(UTC)
        # Real persisted marker used by the shared runtime renewal guard.
        quota = LteQuota(
            subscription_id=1,
            limit_bytes=0,
            period_days=30,
            period_starts_at=now,
            period_ends_at=subscription.end_date,
            legacy_state={
                'original_paid_until': subscription.end_date.isoformat(),
                'runtime': {},
                'snapshot': {'renewal_requires_expiry': True, 'subscription': {'is_trial': False}},
            },
        )
        db.add(quota)
        await db.commit()
        debit = AsyncMock()
        monkeypatch.setattr(autopurchase, 'subtract_user_balance', debit)
        monkeypatch.setattr('app.database.crud.user.subtract_user_balance', debit)
        pricing = AsyncMock()
        monkeypatch.setattr(autopurchase.pricing_engine, 'calculate_tariff_purchase_price', pricing)
        cart = {'cart_mode': 'extend', 'subscription_id': 1, 'tariff_id': 9, 'period_days': 30}
        assert await getattr(autopurchase, entrypoint)(db, user, cart) is False
        debit.assert_not_awaited()
        pricing.assert_not_awaited()
        external_boundaries.clear_intent.assert_awaited_once_with(1)
        assert await db.scalar(select(User.balance_kopeks).where(User.id == 1)) == BALANCE
        assert await db.scalar(select(Subscription.device_limit).where(Subscription.id == 1)) == 15


@pytest.mark.asyncio
async def test_generic_minicart_and_direct_service_cannot_restore_old_global_quota(monkeypatch, external_boundaries):
    async with memory_session(monkeypatch, TABLES) as db:
        user, _ = await seed(db)
        cart = {'period_days': 365, 'traffic_gb': 2048, 'devices': 15}
        assert await autopurchase._process_legacy_generic_cart(db, user, cart) is False
        external_boundaries.delete_cart.assert_awaited_once_with(1, cart)
        for call in (
            MiniAppSubscriptionPurchaseService().build_options(db, user),
            MiniAppSubscriptionPurchaseService().submit_purchase(db, SimpleNamespace(), SimpleNamespace()),
        ):
            with pytest.raises(PurchaseValidationError) as caught:
                await call
            assert caught.value.code == 'managed_catalog_required'
        assert await db.scalar(select(User.balance_kopeks).where(User.id == 1)) == BALANCE
