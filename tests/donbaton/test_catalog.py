from types import SimpleNamespace

import pytest

from app.handlers.subscription.tariff_purchase import get_tariffs_keyboard
from app.services.donbaton_catalog import (
    available_categories,
    category_tariffs,
    network_category,
    select_category,
    tariff_traffic_label,
    validate_tariff_definition,
)


def plan(category=None, **overrides):
    values = dict(
        id=1,
        name='Личный',
        network_category=category,
        traffic_limit_gb=0,
        lte_traffic_limit_gb=60 if category == 'wifi_lte' else None,
        lte_period_days=30 if category == 'wifi_lte' else None,
        is_daily=False,
        custom_days_enabled=False,
        custom_traffic_enabled=False,
        is_trial_available=False,
        show_in_gift=False,
        traffic_topup_enabled=False,
        traffic_topup_packages={},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_mixed_catalog_isolates_lte_without_reclassifying_legacy_names():
    wifi, combo, legacy = plan('wifi'), plan('wifi_lte', id=2), plan(name='Wi-Fi + LTE', id=3)
    tariffs = [legacy, combo, wifi]
    assert network_category(legacy) is None
    assert available_categories(tariffs) == ['wifi_lte', 'wifi', 'legacy']
    assert select_category(tariffs) == 'wifi_lte'
    assert category_tariffs(tariffs, 'wifi') == [wifi]
    assert category_tariffs(tariffs, 'wifi_lte') == [combo]
    assert category_tariffs(tariffs, 'legacy') == [legacy]
    buttons = get_tariffs_keyboard([wifi], 'ru', categories=available_categories(tariffs), selected_category='wifi')
    callbacks = [button.callback_data for row in buttons.inline_keyboard for button in row]
    assert 'tariff_select:1' in callbacks
    assert 'tariff_select:2' not in callbacks
    assert 'tariff_category:legacy' in callbacks


def test_wifi_lte_tab_leads_the_row_and_is_preselected():
    wifi, combo = plan('wifi'), plan('wifi_lte', id=2)
    tariffs = [wifi, combo]
    assert available_categories(tariffs) == ['wifi_lte', 'wifi']
    assert select_category(tariffs) == 'wifi_lte'
    assert select_category(tariffs, 'wifi') == 'wifi'
    category_row = get_tariffs_keyboard(
        [combo], 'ru', categories=available_categories(tariffs), selected_category='wifi_lte'
    ).inline_keyboard[0]
    assert [button.callback_data for button in category_row] == ['tariff_category:wifi_lte', 'tariff_category:wifi']
    assert category_row[0].text == '✓ Wi-Fi + LTE'


def test_legacy_catalog_retains_original_list_and_traffic_label():
    legacy = plan(traffic_limit_gb=1024)
    assert available_categories([legacy]) == []
    assert select_category([legacy], 'wifi_lte') is None
    assert category_tariffs([legacy], None) == [legacy]
    assert tariff_traffic_label(legacy, '1024 ГБ') == '1024 ГБ'
    assert '60 ГиБ' in tariff_traffic_label(plan('wifi_lte'), 'безлимит')


@pytest.mark.parametrize(
    'overrides',
    [
        {'traffic_limit_gb': 1024},
        {'lte_traffic_limit_gb': 0},
        {'lte_period_days': 31},
        {'custom_traffic_enabled': True},
        {'is_daily': True},
        {'show_in_gift': True},
        {'is_trial_available': True},
        {'traffic_topup_packages': {'0': 19900}},
        {'traffic_topup_packages': {'20': -1}},
    ],
)
def test_invalid_managed_product_cannot_be_saved(overrides):
    with pytest.raises(ValueError):
        validate_tariff_definition(plan('wifi_lte', **overrides))


def test_wifi_cannot_silently_carry_lte_entitlement_or_packages():
    validate_tariff_definition(plan('wifi'))
    validate_tariff_definition(plan('wifi_lte', traffic_topup_enabled=True, traffic_topup_packages={'20': 7900}))
    with pytest.raises(ValueError):
        validate_tariff_definition(plan('wifi', traffic_topup_packages={'20': 7900}))
