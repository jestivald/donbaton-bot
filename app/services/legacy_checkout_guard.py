"""Reject obsolete checkout instructions after the explicit DonBaton cutover.

Already charged invoices are converted to their paid balance amount by the
provider finalizer. This module only classifies instructions; it never charges,
changes a subscription, or substitutes a new tariff/price for an old checkout.
"""

from sqlalchemy import select

from app.config import settings
from app.database.models import Subscription, Tariff
from app.services.donbaton_catalog import network_category


RETIRED_CHECKOUT_MESSAGE = (
    'Условия этого счёта устарели. Выберите тариф заново; оплаченные средства останутся на балансе.'
)


def cutover_enabled() -> bool:
    return bool(settings.DONBATON_LTE_ENABLED)


def _identifier(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 10:
        value = int(value)
    return value if type(value) is int and 0 < value <= 2**31 - 1 else None


async def pending_invoice_retired(db, user_id: int, subscription_id) -> bool:
    if not cutover_enabled():
        return False
    identifier = _identifier(subscription_id)
    if identifier is None:
        return True
    subscription = await db.scalar(
        select(Subscription).where(Subscription.id == identifier, Subscription.user_id == user_id)
    )
    return subscription is None or subscription.status != 'pending' or network_category(subscription) is None


async def stars_invoice_retired(db, user_id: int, payload: str) -> bool:
    if not cutover_enabled():
        return False
    if payload.startswith('simple_sub_'):
        parts = payload[len('simple_sub_') :].split('_')
        if len(parts) != 3 or parts[0] != str(user_id) or parts[2] != '30':
            return True
        return await pending_invoice_retired(db, user_id, parts[1])
    if payload.startswith('trial_'):
        return await pending_invoice_retired(db, user_id, payload[len('trial_') :])
    return False


async def saved_tariff_cart_retired(db, cart: dict) -> bool:
    if not cutover_enabled():
        return False
    identifier = _identifier(cart.get('tariff_id'))
    days = _identifier(cart.get('period_days'))
    if identifier is None or days is None:
        return True
    tariff = await db.scalar(select(Tariff).where(Tariff.id == identifier))
    return (
        tariff is None
        or network_category(tariff) is None
        or not tariff.is_active
        or days != 30
        or str(days) not in (tariff.period_prices or {})
    )
