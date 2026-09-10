"""The published catalogue document must stay a valid, sellable DonBaton grid."""

import json
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

from app.services.donbaton_catalog import validate_tariff_definition
from app.utils.pricing_utils import format_period_description


CATALOG = Path(__file__).resolve().parents[2] / 'docs' / 'donbaton-catalog.json'
EXPECTED_PERIODS = [30, 90, 180, 360]
HISTORIC_GRID = {
    ('wifi', 3): [19900, 52900, 84900, 119900],
    ('wifi', 7): [29900, 79900, 129900, 179900],
    ('wifi_lte', 3): [29900, 79900, 129900, 179900],
    ('wifi_lte', 7): [44900, 119900, 189900, 269900],
}


def _offers():
    document = json.loads(CATALOG.read_text(encoding='utf-8'))
    assert document['currency'] == 'RUB' and document['traffic_unit'] == 'GiB'
    return document['tariffs']


def test_catalog_has_four_offers_with_the_standard_periods():
    seen = {}
    for offer in _offers():
        key = (offer['network_category'], offer['device_limit'])
        assert key not in seen, 'duplicate offer'
        seen[key] = offer
        assert [p['days'] for p in offer['period_prices']] == EXPECTED_PERIODS
        assert [p['price_kopeks'] for p in offer['period_prices']] == HISTORIC_GRID[key]
    assert set(seen) == set(HISTORIC_GRID)


def test_longer_periods_are_cheaper_per_day_but_dearer_in_total():
    for offer in _offers():
        prices = offer['period_prices']
        for previous, current in pairwise(prices):
            assert current['price_kopeks'] > previous['price_kopeks']
            assert current['price_kopeks'] / current['days'] < previous['price_kopeks'] / previous['days']


def test_every_offer_passes_the_catalog_policy_and_renders_month_labels():
    for offer in _offers():
        definition = SimpleNamespace(
            **{**offer, 'period_prices': {str(p['days']): p['price_kopeks'] for p in offer['period_prices']}}
        )
        validate_tariff_definition(definition)
        for period in offer['period_prices']:
            label = format_period_description(period['days'], 'ru')
            assert 'дн' not in label, f'{period["days"]} days should render as months, got {label}'
