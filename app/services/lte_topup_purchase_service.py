"""Atomic balance purchases for LTE buckets, shared by Telegram and cabinet."""

import asyncio
import re
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.crud.user import subtract_user_balance
from app.database.models import LteTopup, Subscription, Tariff, Transaction, TransactionType, User
from app.services.lte_quota_service import LtePolicyError, credit_topup, get_quota_status, validate_tariff_for_sale
from app.services.pricing_engine import PricingEngine


logger = structlog.get_logger(__name__)
GIB = 1024**3


class LtePurchaseError(ValueError):
    def __init__(self, message, *, code='lte_purchase_unavailable', status=400, missing_amount=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.missing_amount = missing_amount


def quote_lte_package(user, tariff, gb):
    validate_tariff_for_sale(tariff)
    if tariff.network_category != 'wifi_lte' or not tariff.traffic_topup_enabled:
        raise LtePurchaseError('LTE-пакеты доступны в тарифах «Wi-Fi + LTE»')
    packages = tariff.get_traffic_topup_packages()
    if type(gb) is not int or gb <= 0 or gb not in packages or packages[gb] <= 0:
        raise LtePurchaseError('Этот пакет LTE недоступен')
    base = packages[gb]
    percent = PricingEngine.get_addon_discount_percent(user, 'traffic', 30)
    final = PricingEngine.apply_discount(base, percent)
    if 0 < percent < 100 and final > 0:
        final = max(100, final)
    return {
        'gb': gb,
        'price_kopeks': final,
        'price_rubles': final / 100,
        'is_unlimited': False,
        'discount_percent': percent,
        'base_price_kopeks': base if percent else None,
        'discount_kopeks': base - final if percent else None,
    }


async def purchase_lte_topup(db, user, subscription, *, gb, request_id, expected_price_kopeks=None):
    """Charge and grant once. Commit both; retry remote delivery separately."""
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', request_id):
        raise LtePurchaseError('Обновите экран покупки и повторите выбор пакета', code='request_id_required')
    operation_id = f'lte-topup:{user.id}:{request_id}'
    transaction = None
    replay = False
    try:
        # One lock order across purchase paths: user, subscription, quota.
        await db.execute(select(User.id).where(User.id == user.id).with_for_update())
        subscription = (
            await db.execute(
                select(Subscription)
                .where(Subscription.id == subscription.id, Subscription.user_id == user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if subscription is None:
            raise LtePurchaseError('Подписка не найдена', status=404)
        bucket = (await db.execute(select(LteTopup).where(LteTopup.operation_id == operation_id))).scalar_one_or_none()
        if bucket is not None:
            if bucket.subscription_id != subscription.id or bucket.amount_bytes != gb * GIB:
                raise LtePurchaseError(
                    'Этот запрос уже использован для другой покупки', code='request_conflict', status=409
                )
            transaction = (
                await db.execute(
                    select(Transaction).where(Transaction.user_id == user.id, Transaction.external_id == operation_id)
                )
            ).scalar_one_or_none()
            if transaction is None:
                raise LtePurchaseError('Требуется сверка покупки', code='receipt_missing', status=409)
            paid = abs(transaction.amount_kopeks)
            replay = True
        else:
            now = datetime.now(UTC)
            ends_at = subscription.end_date
            if ends_at is not None and ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=UTC)
            if (
                subscription.network_category != 'wifi_lte'
                or subscription.is_trial
                or subscription.status != 'active'
                or ends_at is None
                or ends_at <= now
                or getattr(user, 'restriction_subscription', False)
            ):
                raise LtePurchaseError('Докупка доступна для действующей платной подписки «Wi-Fi + LTE»')
            tariff = await db.get(Tariff, subscription.tariff_id)
            if tariff is None:
                raise LtePurchaseError('Тариф не найден', status=404)
            quote = quote_lte_package(user, tariff, gb)
            quota = await get_quota_status(db, subscription)
            if quota is None:
                raise LtePurchaseError('Сначала необходимо подготовить LTE-квоту подписки')
            maximum = tariff.max_topup_traffic_gb or 0
            if maximum > 0 and quota['topup_bytes'] + gb * GIB > maximum * GIB:
                raise LtePurchaseError('Достигнут максимальный объём активных докупок LTE')
            paid = quote['price_kopeks']
            if expected_price_kopeks is not None and paid != expected_price_kopeks:
                raise LtePurchaseError(
                    'Цена пакета изменилась. Обновите список пакетов.', code='price_changed', status=409
                )
            description = f'Докупка LTE-трафика: {gb} ГиБ на 30 дней'
            if not await subtract_user_balance(db, user, paid, description, commit=False):
                await db.refresh(user, ['balance_kopeks'])
                raise LtePurchaseError(
                    'Недостаточно средств. Пополните баланс и повторите покупку.',
                    code='insufficient_funds',
                    status=402,
                    missing_amount=max(0, paid - user.balance_kopeks),
                )
            await credit_topup(db, subscription, bytes_amount=gb * GIB, operation_id=operation_id, now=now)
            transaction = await create_transaction(
                db,
                user.id,
                TransactionType.SUBSCRIPTION_PAYMENT,
                paid,
                description,
                external_id=operation_id,
                commit=False,
            )
        subscription_id, user_id = subscription.id, user.id
        await db.commit()
    except (LtePurchaseError, LtePolicyError):
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    # The purchased product is durable. Never charge or refund again on a panel timeout.
    if not replay:
        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=paid,
            user_id=user_id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            external_id=operation_id,
            description=transaction.description,
        )
    try:
        from app.services.subscription_service import SubscriptionService

        async with asyncio.timeout(10):
            synced = await SubscriptionService().update_remnawave_user(db, subscription)
            if not synced:
                raise RuntimeError('Panel synchronization deferred')
    except Exception as error:
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        logger.warning('LTE purchase delivery deferred', subscription_id=subscription_id, error=str(error))
        remnawave_retry_queue.enqueue(subscription_id=subscription_id, user_id=user_id, action='update')
    # The synchronization service may roll back and expire ORM objects on error.
    # Reload using scalar IDs captured before remote I/O.
    user = await db.get(User, user_id, populate_existing=True)
    subscription = await db.get(Subscription, subscription_id, populate_existing=True)
    return {
        'success': True,
        'message': 'LTE-пакет добавлен',
        'gb_added': gb,
        'new_traffic_limit_gb': 0,
        'amount_paid_kopeks': paid,
        'new_balance_kopeks': user.balance_kopeks,
        'replayed': replay,
        'network_category': 'wifi_lte',
        'lte_quota': await get_quota_status(db, subscription),
    }
