"""Presentation and validation of explicitly managed DonBaton products."""

from typing import Any


# Порядок кортежа задаёт порядок вкладок в витрине: с 10.09.2026 по решению владельца
# «Wi-Fi + LTE» идёт первой и подставляется по умолчанию (см. select_category): со второй
# позиции её пролистывали и покупали тарифы без LTE.
NETWORK_CATEGORIES = ('wifi_lte', 'wifi')
CATEGORY_LABELS = {'wifi_lte': 'Wi-Fi + LTE', 'wifi': 'Только Wi-Fi', 'legacy': 'Другие тарифы'}
NETWORK_EXPLANATION = (
    'Wi-Fi — обычные серверы без лимита трафика. LTE — отдельный пакет обходных серверов. '
    'Пакет расходуется при выборе обходного сервера, независимо от сети телефона.'
)


def network_category(value: Any) -> str | None:
    category = getattr(value, 'network_category', None)
    return category if isinstance(category, str) and category in NETWORK_CATEGORIES else None


def tariff_network_fields(tariff: Any) -> dict:
    category = network_category(tariff)
    return {
        'network_category': category,
        'lte_traffic_limit_gb': getattr(tariff, 'lte_traffic_limit_gb', None) if category == 'wifi_lte' else None,
        'lte_period_days': getattr(tariff, 'lte_period_days', None) if category == 'wifi_lte' else None,
    }


def tariff_traffic_label(tariff: Any, legacy_label: str) -> str:
    category = network_category(tariff)
    if category == 'wifi':
        return 'Wi-Fi: безлимит'
    if category == 'wifi_lte':
        return f'Wi-Fi: безлимит · LTE: {tariff.lte_traffic_limit_gb} ГиБ каждые {tariff.lte_period_days} дней'
    return legacy_label


def available_categories(tariffs: list) -> list[str]:
    present = {network_category(tariff) or 'legacy' for tariff in tariffs}
    if present == {'legacy'} or not present:
        return []
    return [category for category in (*NETWORK_CATEGORIES, 'legacy') if category in present]


def select_category(tariffs: list, requested: str | None = None) -> str | None:
    categories = available_categories(tariffs)
    if requested in categories:
        return requested
    return 'wifi_lte' if 'wifi_lte' in categories else next(iter(categories), None)


def category_tariffs(tariffs: list, selected: str | None) -> list:
    if selected is None:
        return tariffs
    return [tariff for tariff in tariffs if (network_category(tariff) or 'legacy') == selected]


def validate_tariff_definition(tariff: Any) -> None:
    """Validate draft data without requiring a configured or enabled panel."""
    raw_category = getattr(tariff, 'network_category', None)
    if raw_category is None:
        return
    if raw_category not in NETWORK_CATEGORIES:
        raise ValueError('Неизвестная категория сети')
    if getattr(tariff, 'is_trial_available', False) or getattr(tariff, 'show_in_gift', False):
        raise ValueError('Пробные и подарочные Wi-Fi/LTE-тарифы пока не поддерживаются')
    if tariff.traffic_limit_gb != 0:
        raise ValueError('Тариф Wi-Fi должен иметь общий безлимит: traffic_limit_gb=0')
    if any(getattr(tariff, field, False) for field in ('is_daily', 'custom_traffic_enabled', 'custom_days_enabled')):
        raise ValueError('Wi-Fi/LTE поддерживает фиксированные периоды и отдельные пакеты LTE')
    limit = getattr(tariff, 'lte_traffic_limit_gb', None)
    period = getattr(tariff, 'lte_period_days', None)
    if raw_category == 'wifi_lte':
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError('Укажите положительный LTE-пакет в ГиБ')
        if period != 30:
            raise ValueError('Период обновления LTE-пакета должен быть 30 дней')
    elif limit not in (None, 0) or period is not None:
        raise ValueError('Тариф «Только Wi-Fi» не содержит LTE-пакет')
    packages = getattr(tariff, 'traffic_topup_packages', None) or {}
    for size, price in packages.items():
        if not str(size).isdigit() or not 1 <= int(size) <= 100_000:
            raise ValueError('LTE-докупки должны иметь положительный объём в ГиБ')
        if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
            raise ValueError('Цена LTE-докупки должна быть положительным целым числом копеек')
    if raw_category == 'wifi' and (packages or getattr(tariff, 'traffic_topup_enabled', False)):
        raise ValueError('Докупки LTE доступны в категории «Wi-Fi + LTE»')


async def validate_user_tariff_purchase(db, user, tariff) -> None:
    """Check current paid rights before a checkout commits a balance debit."""
    from app.config import settings
    from app.database.crud.subscription import get_subscription_by_user_id
    from app.services.lte_quota_service import validate_subscription_transition, validate_tariff_for_sale

    if network_category(tariff):
        validate_tariff_for_sale(tariff)
    if settings.is_multi_tariff_enabled():
        return  # independent subscriptions do not switch each other's entitlement
    existing = await get_subscription_by_user_id(db, user.id)
    if existing is not None and (network_category(tariff) or network_category(existing)):
        validate_subscription_transition(existing, tariff)
