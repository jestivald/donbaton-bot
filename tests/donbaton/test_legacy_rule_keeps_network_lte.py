"""Правило «старой подписки» апстрима 4.14.0 не перекрывает LTE-ветку форка.

Апстрим считает «старой» платную подписку без ``tariff_id`` при включённых
тарифах (``app/utils/legacy_subscription.py``) и отказывает ей в докупках — в
боте и в кабинете, а в меню вместо «Продлить» даёт «Перейти на тариф».

Сетевая подписка ДонБатона тариф имеет: его ставят покупка и перенос старых
прав (``import_legacy_quota`` пишет технический тариф). Цена пакета LTE тоже
берётся из тарифа (``lte_topup_purchase_service``), поэтому сетевой подписке
без тарифа докупать LTE было нечем и до 4.14.0. Здесь закреплено:

* сетевая подписка с тарифом «старой» не считается: меню, кабинетный список
  пакетов и покупка LTE идут прежним путём;
* в боте LTE-ветка стоит раньше апстримовского отказа — при любом тарифе;
* сетевой подписке без тарифа кабинет отвечает ``tariff_required`` до
  обращения к леджеру квот.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import traffic as cabinet_traffic
from app.cabinet.routes.subscription_modules.helpers import ensure_subscription_has_tariff
from app.cabinet.schemas.subscription import TrafficPurchaseRequest
from app.config import Settings
from app.handlers.subscription import lte as bot_lte, traffic as bot_traffic
from app.keyboards import inline as keyboards
from app.utils.legacy_subscription import is_legacy_subscription


NETWORK_TARIFF_ID = 9


def _network_subscription(*, tariff_id: int | None) -> SimpleNamespace:
    tariff = (
        SimpleNamespace(id=tariff_id, is_daily=False, is_free=False, can_topup_traffic=lambda: True)
        if tariff_id
        else None
    )
    return SimpleNamespace(
        id=1,
        user_id=1,
        tariff_id=tariff_id,
        tariff=tariff,
        is_trial=False,
        network_category='wifi_lte',
        status='active',
        actual_status='active',
        traffic_limit_gb=0,
        end_date=None,
        is_daily_paused=False,
    )


@pytest.fixture
def tariffs_mode(monkeypatch):
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)


def test_network_subscription_with_tariff_is_not_legacy(tariffs_mode):
    subscription = _network_subscription(tariff_id=NETWORK_TARIFF_ID)

    assert is_legacy_subscription(subscription) is False
    ensure_subscription_has_tariff(subscription)  # не бросает


def test_network_subscription_with_tariff_keeps_renewal_and_lte_buttons(tariffs_mode, monkeypatch):
    monkeypatch.setattr(keyboards, 'get_display_subscription_link', lambda sub: None)
    subscription = _network_subscription(tariff_id=NETWORK_TARIFF_ID)

    markup = keyboards.get_subscription_keyboard('ru', has_subscription=True, is_trial=False, subscription=subscription)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]

    assert 'subscription_extend' in callbacks
    assert 'subscription_autopay' in callbacks
    assert 'buy_traffic' in callbacks
    # «Перейти на тариф» апстрима ведёт на tariff_switch — сетевой подписке с тарифом его не дают.
    assert 'tariff_switch' not in callbacks


@pytest.mark.asyncio
async def test_cabinet_quotes_lte_packages_for_network_subscription_with_tariff(tariffs_mode, monkeypatch):
    subscription = _network_subscription(tariff_id=NETWORK_TARIFF_ID)
    tariff = SimpleNamespace(
        id=NETWORK_TARIFF_ID,
        traffic_topup_enabled=True,
        get_traffic_topup_packages=lambda: {60: 19900, 20: 7900},
    )

    async def get_tariff(db, tariff_id):
        return tariff if tariff_id == NETWORK_TARIFF_ID else None

    monkeypatch.setattr(cabinet_traffic, 'resolve_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', get_tariff)
    monkeypatch.setattr(
        cabinet_traffic,
        'quote_lte_package',
        lambda user, tariff, gb: {'gb': gb, 'price_kopeks': 100 * gb, 'price_rubles': float(gb)},
    )

    packages = await cabinet_traffic.get_traffic_packages(user=SimpleNamespace(id=1), db=None, subscription_id=None)

    assert [package.gb for package in packages] == [20, 60]


@pytest.mark.asyncio
async def test_cabinet_lte_purchase_reaches_the_ledger_for_network_subscription_with_tariff(tariffs_mode, monkeypatch):
    subscription = _network_subscription(tariff_id=NETWORK_TARIFF_ID)
    purchase = AsyncMock(return_value={'success': True})
    monkeypatch.setattr(cabinet_traffic, 'resolve_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(cabinet_traffic, 'purchase_lte_topup', purchase)

    result = await cabinet_traffic.purchase_traffic(
        request=TrafficPurchaseRequest(gb=20),
        user=SimpleNamespace(id=1, restriction_subscription=False),
        db=None,
        subscription_id=None,
    )

    assert result == {'success': True}
    purchase.assert_awaited_once()
    assert purchase.await_args.kwargs['gb'] == 20


@pytest.mark.asyncio
async def test_cabinet_refuses_lte_purchase_without_tariff_before_the_ledger(tariffs_mode, monkeypatch):
    subscription = _network_subscription(tariff_id=None)
    purchase = AsyncMock(return_value={'success': True})
    monkeypatch.setattr(cabinet_traffic, 'resolve_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(cabinet_traffic, 'purchase_lte_topup', purchase)

    with pytest.raises(HTTPException) as error:
        await cabinet_traffic.purchase_traffic(
            request=TrafficPurchaseRequest(gb=20),
            user=SimpleNamespace(id=1, restriction_subscription=False),
            db=None,
            subscription_id=None,
        )

    assert error.value.status_code == 400
    assert error.value.detail['code'] == 'tariff_required'
    purchase.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('tariff_id', [NETWORK_TARIFF_ID, None])
async def test_bot_routes_network_subscription_to_lte_packages_before_the_legacy_refusal(
    tariffs_mode, monkeypatch, tariff_id
):
    subscription = _network_subscription(tariff_id=tariff_id)
    show_lte_packages = AsyncMock()
    monkeypatch.setattr(bot_traffic, '_resolve_subscription', AsyncMock(return_value=(subscription, None)))
    monkeypatch.setattr(bot_lte, 'show_lte_packages', show_lte_packages)
    callback = SimpleNamespace(
        data='buy_traffic',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    db_user = SimpleNamespace(id=1, language='ru')

    await bot_traffic.handle_add_traffic(callback, db_user, db=None)

    show_lte_packages.assert_awaited_once()
    callback.answer.assert_not_awaited()
