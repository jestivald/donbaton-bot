"""Cabinet checkout preserves paid custom terms and reports safe refusals."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import purchase as api
from app.cabinet.schemas.subscription import TariffPurchaseRequest
from app.config import settings
from app.database.crud import subscription as subscription_crud, user as user_crud
from app.database.models import LteQuota, User
from app.services.lte_quota_service import LtePolicyError
from tests.donbaton.test_lte_legacy import EPOCH, activate, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI
from tests.donbaton.test_paid_trial_cutover import TABLES
from tests.fixtures.sqlite_memory import memory_session


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ENABLED', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_READY', True)
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', EPOCH)
    monkeypatch.setattr(settings, 'DONBATON_WIFI_SQUAD_UUIDS', WIFI)
    monkeypatch.setattr(settings, 'DONBATON_LTE_SQUAD_UUIDS', LTE)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(User, 'get_primary_promo_group', lambda self: None)


async def boundaries(monkeypatch, db, sub, tariff):
    from app.services import yandex_offline_conv_service

    user = await db.get(User, sub.user_id)
    user.balance_kopeks = 100000
    await db.commit()
    await db.refresh(tariff, ['allowed_promo_groups'])
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(api, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(api, 'get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr(subscription_crud, 'get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr(subscription_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    monkeypatch.setattr(api, 'get_server_squad_by_uuid', AsyncMock(return_value=None))
    monkeypatch.setattr(api.SubscriptionService, 'update_remnawave_user', AsyncMock())
    monkeypatch.setattr(api.SubscriptionService, 'create_remnawave_user', AsyncMock())
    monkeypatch.setattr(api.user_cart_service, 'save_user_cart', AsyncMock())
    monkeypatch.setattr(yandex_offline_conv_service, 'store_cid_only', AsyncMock())

    async def debit(_db, charged_user, amount, *args, **kwargs):
        charged_user.balance_kopeks -= amount
        await _db.commit()
        return True

    async def refund(_db, refunded_user, amount, *args, **kwargs):
        await _db.refresh(refunded_user)
        refunded_user.balance_kopeks += amount
        await _db.commit()
        return True

    debit_mock = AsyncMock(side_effect=debit)
    refund_mock = AsyncMock(side_effect=refund)
    transaction_mock = AsyncMock(return_value=SimpleNamespace(id=1))
    monkeypatch.setattr(api, 'subtract_user_balance', debit_mock)
    monkeypatch.setattr(api, 'add_user_balance', refund_mock)
    monkeypatch.setattr(api, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(api, 'create_transaction', transaction_mock)
    return user, debit_mock, refund_mock, transaction_mock


@pytest.mark.asyncio
async def test_legacy_grant_refusal_is_http_400_before_debit(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, _, grants = await legacy_seed(
            db, status='expired', paid_until=NOW - timedelta(days=1), grants=[(500, NOW + timedelta(days=10))]
        )
        user, debit, refund, transaction = await boundaries(monkeypatch, db, sub, target)
        with pytest.raises(HTTPException) as caught:
            await api.purchase_tariff(TariffPurchaseRequest(tariff_id=target.id, period_days=30), user=user, db=db)
        assert caught.value.status_code == 400
        assert 'перенести действующие докупки' in caught.value.detail
        debit.assert_not_awaited()
        refund.assert_not_awaited()
        transaction.assert_not_awaited()
        assert user.balance_kopeks == 100000 and grants[0].traffic_gb == 500


@pytest.mark.asyncio
async def test_active_custom_terms_refuse_renewal_without_charging_old_extra_devices(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, devices=15, custom=True)
        await activate(db, sub, target, snapshot)
        user, debit, _, transaction = await boundaries(monkeypatch, db, sub, target)
        with pytest.raises(HTTPException) as caught:
            await api.purchase_tariff(TariffPurchaseRequest(tariff_id=target.id, period_days=30), user=user, db=db)
        assert caught.value.status_code == 400
        assert 'индивидуальные условия' in caught.value.detail
        debit.assert_not_awaited()
        transaction.assert_not_awaited()
        assert sub.device_limit == 15 and sub.end_date.isoformat() == snapshot['subscription']['end_date']


@pytest.mark.asyncio
@pytest.mark.parametrize('devices', [0, 15])
async def test_expired_custom_same_tariff_quotes_and_purchases_only_target_device_limit(devices, monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(
            db, devices=devices, custom=True, status='expired', paid_until=NOW - timedelta(days=1)
        )
        await activate(db, sub, target, snapshot)
        user, debit, refund, _ = await boundaries(monkeypatch, db, sub, target)
        preview = await api._build_tariff_response(db, target, user=user, subscription=sub)
        assert preview['device_limit'] == 3 and preview['extra_devices_count'] == 0
        assert preview['periods'][0]['price_kopeks'] == 29900
        result = await api.purchase_tariff(TariffPurchaseRequest(tariff_id=target.id, period_days=30), user=user, db=db)
        assert result['success'] is True and result['charged_amount'] == 29900
        assert result['subscription'].device_limit == 3
        await db.refresh(sub)
        quota = await db.get(LteQuota, sub.id)
        assert sub.device_limit == 3 and sub.status == 'active'
        assert sub.end_date > datetime.now(UTC) + timedelta(days=29)
        assert quota.legacy_state['snapshot']['subscription']['device_limit'] == devices
        assert user.balance_kopeks == 70100
        debit.assert_awaited_once()
        refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_race_after_debit_refunds_once_and_returns_http_400(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, status='expired', paid_until=NOW - timedelta(days=1))
        await activate(db, sub, target, snapshot)
        user, debit, refund, _ = await boundaries(monkeypatch, db, sub, target)
        monkeypatch.setattr(api, 'extend_subscription', AsyncMock(side_effect=LtePolicyError('Условия изменились')))
        with pytest.raises(HTTPException) as caught:
            await api.purchase_tariff(TariffPurchaseRequest(tariff_id=target.id, period_days=30), user=user, db=db)
        assert caught.value.status_code == 400 and caught.value.detail == 'Условия изменились'
        debit.assert_awaited_once()
        refund.assert_awaited_once()
        await db.refresh(user)
        assert user.balance_kopeks == 100000
