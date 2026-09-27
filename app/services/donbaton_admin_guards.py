"""Отказы админских действий, которые обошли бы раздельный учёт Wi-Fi/LTE.

Сетевые условия подписки (категория, пакет LTE, устройства тарифа) ведёт
леджер, и меняет их только покупка: ``update_entitlement`` сверяет переход и
переносит пакет. Админские «сменить тариф» и «сделать платной/пробной» пишут
поля подписки напрямую, поэтому для сетевых подписок и тарифов они отказывают
— одинаково в кабинете, в бот-админке и в массовых действиях.

Здесь же проверка «сироты»: права только в панели (``lte_orphan_quotas``) не
должны получать бот-подписку мимо переноса леджера — иначе LTE-сквад панели
уезжает в неуправляемую подписку без учёта.
"""

from __future__ import annotations

from sqlalchemy import select

from app.database.models import LteOrphanQuota, Tariff
from app.services.donbaton_catalog import network_category


NETWORK_TARIFF_CHANGE_REFUSAL = 'Сетевой тариф меняется только покупкой (клиентом или «💳 Купить тариф» в админке)'
MANAGED_TRIAL_TO_PAID_REFUSAL = (
    'Пробную подписку Wi-Fi/LTE переводят в платную только покупкой тарифа '
    '(клиентом или «💳 Купить тариф» в админке): пробный пакет LTE сам на платный не сменится.'
)
MANAGED_PAID_TO_TRIAL_REFUSAL = (
    'Платную подписку Wi-Fi/LTE нельзя сделать пробной: её пакет LTE оплачен и ведётся учётом.'
)


async def uses_network_accounting(db, subscription) -> bool:
    """Подписка ведётся раздельным учётом или стоит на сетевом тарифе.

    Категорию читаем из базы (``_uses_lte_accounting``): перенос мог закоммититься
    после того, как вызывающий загрузил объект. Сетевой тариф без категории у
    подписки — тоже сетевая подписка, как и в ``needs_managed_policy``.
    """
    from app.database.crud.subscription import _uses_lte_accounting

    if await _uses_lte_accounting(db, subscription):
        return True
    tariff_id = getattr(subscription, 'tariff_id', None)
    if not tariff_id:
        return False
    return network_category(await db.get(Tariff, tariff_id)) is not None


async def tariff_change_refusal(db, subscription, target_tariff) -> str | None:
    """Причина отказа админской смены тарифа или ``None``, если смену можно применить."""
    if network_category(target_tariff) or await uses_network_accounting(db, subscription):
        return NETWORK_TARIFF_CHANGE_REFUSAL
    return None


async def subscription_type_change_refusal(db, subscription, *, to_trial: bool) -> str | None:
    """Причина отказа переключения «пробная ↔ платная» у сетевой подписки.

    Пробный пакет LTE помнит конец пробы (``trial_ends_at``) и платным периодом не
    пополняется; обратный переход дал бы платной подписке правила пробной.
    """
    if bool(getattr(subscription, 'is_trial', False)) == to_trial:
        return None
    if not await uses_network_accounting(db, subscription):
        return None
    return MANAGED_PAID_TO_TRIAL_REFUSAL if to_trial else MANAGED_TRIAL_TO_PAID_REFUSAL


async def has_orphan_lte_quota(db, panel_id) -> bool:
    """У аккаунта панели есть права только в панели — отдельный леджер сироты."""
    if panel_id is None:
        return False
    found = await db.execute(select(LteOrphanQuota.panel_id).where(LteOrphanQuota.panel_id == panel_id).limit(1))
    return found.scalar_one_or_none() is not None
