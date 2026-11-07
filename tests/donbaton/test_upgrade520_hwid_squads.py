"""HWID deletion is opt-in; network products require their whole squad set."""

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings, settings
from app.database.models import User
from app.external.remnawave_api import RemnaWaveAPIError
from app.services import checkout_squad_validation as checkout
from app.services.panel_sync.identity import PanelIdentity
from app.services.panel_sync.writer import push_subscription, write_panel_user_with_squad_recovery
from tests.donbaton.test_lte_legacy import BASELINE, EPOCH, TABLES, activate, legacy_seed
from tests.donbaton.test_lte_quota import LTE, NOW, WIFI, sample
from tests.fixtures.sqlite_memory import memory_session
from tests.services.panel_sync.test_device_limit_enforcement import api_with_devices
from tests.services.panel_sync.test_writer import _sub, _user


def test_hwid_trimming_default_and_settings_registration():
    from app.services.system_settings_service import BotConfigurationService

    assert Settings.model_fields['PANEL_SYNC_TRIM_EXCESS_HWID'].default is False
    assert BotConfigurationService.CATEGORY_KEY_OVERRIDES['PANEL_SYNC_TRIM_EXCESS_HWID'] == 'REMNAWAVE'
    assert 'до решения владельца' in BotConfigurationService.SETTING_HINTS['PANEL_SYNC_TRIM_EXCESS_HWID']['description']


async def test_disabled_trimming_writes_cap_without_reading_or_removing_devices(monkeypatch):
    monkeypatch.setattr(settings, 'PANEL_SYNC_TRIM_EXCESS_HWID', False)
    api, devices = api_with_devices(4)
    await push_subscription(api, _user(), _sub(remnawave_id=42, device_limit=2), multi_tariff=True)
    assert api.update_user.await_args.kwargs['hwid_device_limit'] == 2
    assert len(devices) == 4
    api.get_user_devices_all.assert_not_awaited()
    api.remove_device.assert_not_awaited()


@pytest.mark.parametrize('before_boundary', [True, False])
async def test_enabled_trim_uses_legacy_lte_policy_limit_at_window_boundary(monkeypatch, before_boundary):
    for key, value in dict(
        DONBATON_LTE_ENABLED=True,
        DONBATON_LTE_ACCOUNTING_READY=True,
        DONBATON_LTE_ACCOUNTING_EPOCH=EPOCH,
        DONBATON_WIFI_SQUAD_UUIDS=WIFI,
        DONBATON_LTE_SQUAD_UUIDS=LTE,
        PANEL_SYNC_TRIM_EXCESS_HWID=True,
        MULTI_TARIFF_ENABLED=False,
    ).items():
        monkeypatch.setattr(settings, key, value)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, custom=True, devices=15, paid_until=NOW + timedelta(days=1))
        await activate(db, sub, target, snapshot)
        sub.end_date = NOW + timedelta(days=60)
        await db.commit()
        user = await db.get(User, 1)
        panel = sample(BASELINE)
        panel.expire_at = sub.end_date
        devices = [{'userId': 11, 'hwid': f'local-{i}', 'updatedAt': NOW.isoformat()} for i in range(15)]

        async def get_devices(panel_id):
            assert panel_id == 11
            return {'devices': list(devices), 'total': len(devices)}

        async def remove_device(panel_id, hwid):
            assert panel_id == 11
            devices[:] = [row for row in devices if row['hwid'] != hwid]
            return True

        api = SimpleNamespace(
            update_user=AsyncMock(return_value=panel),
            create_user=AsyncMock(),
            get_user_devices_all=AsyncMock(side_effect=get_devices),
            remove_device=AsyncMock(side_effect=remove_device),
        )
        moment = NOW if before_boundary else NOW + timedelta(days=2)
        await push_subscription(
            api, user, sub, db=db, identity=PanelIdentity(panel_user=panel), reset_devices=False, now=moment
        )
        expected = 15 if before_boundary else target.device_limit
        assert api.update_user.await_args.kwargs['hwid_device_limit'] == expected
        assert len(devices) == expected


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte'])
@pytest.mark.parametrize('catalog', [[WIFI], [LTE], [], None])
async def test_network_preflight_refuses_missing_required_squad_or_unknown_catalog(monkeypatch, category, catalog):
    @asynccontextmanager
    async def client():
        yield SimpleNamespace(
            get_internal_squads=AsyncMock(
                return_value=None if catalog is None else [SimpleNamespace(uuid=x) for x in catalog]
            )
        )

    monkeypatch.setattr(checkout, 'RemnaWaveService', lambda: SimpleNamespace(get_api_client=client))
    requested = [WIFI, LTE] if category == 'wifi_lte' else [WIFI]
    if catalog is not None and set(requested).issubset(catalog):
        await checkout.validate_checkout_squads(requested, network_category=category)
    else:
        with pytest.raises(checkout.CheckoutSquadsUnavailableError):
            await checkout.validate_checkout_squads(requested, network_category=category)
    assert requested == ([WIFI, LTE] if category == 'wifi_lte' else [WIFI])


@pytest.mark.parametrize('code', ['A018', 'A039', 'A118'])
@pytest.mark.parametrize('catalog', [[WIFI], [LTE], [], [WIFI, LTE], None])
async def test_network_recovery_never_acknowledges_a_narrowed_write(code, catalog):
    error = RemnaWaveAPIError('write failed', response_data={'errorCode': code})
    write = AsyncMock(side_effect=error)
    api = SimpleNamespace(
        get_internal_squads=AsyncMock(
            return_value=None if catalog is None else [SimpleNamespace(uuid=x) for x in catalog]
        )
    )
    requested = [WIFI, LTE]
    with pytest.raises(RemnaWaveAPIError):
        await write_panel_user_with_squad_recovery(
            api, write, require_all_squads=True, active_internal_squads=requested
        )
    write.assert_awaited_once_with(active_internal_squads=[WIFI, LTE])
    assert requested == [WIFI, LTE]


async def test_ordinary_recovery_keeps_upstream_partial_survival():
    error = RemnaWaveAPIError('write failed', response_data={'errorCode': 'A018'})
    result = object()
    write = AsyncMock(side_effect=[error, result])
    api = SimpleNamespace(get_internal_squads=AsyncMock(return_value=[SimpleNamespace(uuid=WIFI)]))
    updated, surviving = await write_panel_user_with_squad_recovery(api, write, active_internal_squads=[WIFI, LTE])
    assert updated is result
    assert surviving == (WIFI,)
    assert write.await_args.kwargs['active_internal_squads'] == [WIFI]


async def test_network_requirement_survives_nested_grace_recovery_and_does_not_leak():
    error = RemnaWaveAPIError('write failed', response_data={'errorCode': 'A039'})
    write = AsyncMock(side_effect=error)
    api = SimpleNamespace(get_internal_squads=AsyncMock(return_value=[SimpleNamespace(uuid=WIFI)]))

    async def grace_wrapper(**kwargs):
        result, _ = await write_panel_user_with_squad_recovery(api, write, **kwargs)
        return result

    with pytest.raises(RemnaWaveAPIError):
        await write_panel_user_with_squad_recovery(
            api, grace_wrapper, require_all_squads=True, active_internal_squads=[WIFI, LTE]
        )
    write.assert_awaited_once_with(active_internal_squads=[WIFI, LTE])
    write.side_effect = [error, object()]
    _, surviving = await write_panel_user_with_squad_recovery(api, write, active_internal_squads=[WIFI, LTE])
    assert surviving == (WIFI,)


async def test_network_push_retains_pending_expiry_and_full_local_squads_when_lte_is_missing(monkeypatch):
    for key, value in dict(
        DONBATON_LTE_ENABLED=True,
        DONBATON_LTE_ACCOUNTING_READY=True,
        DONBATON_LTE_ACCOUNTING_EPOCH=EPOCH,
        DONBATON_WIFI_SQUAD_UUIDS=WIFI,
        DONBATON_LTE_SQUAD_UUIDS=LTE,
        MULTI_TARIFF_ENABLED=False,
    ).items():
        monkeypatch.setattr(settings, key, value)
    async with memory_session(monkeypatch, TABLES) as db:
        sub, target, snapshot, _ = await legacy_seed(db, custom=True, devices=15)
        await activate(db, sub, target, snapshot)
        pending_expiry = sub.end_date + timedelta(days=30)
        sub.pending_panel_expire_at = pending_expiry
        sub.connected_squads = [WIFI, LTE]
        await db.commit()
        panel = sample(BASELINE)
        panel.expire_at = sub.end_date
        user = await db.get(User, 1)
        error = RemnaWaveAPIError('LTE squad deleted', response_data={'errorCode': 'A118'})
        api = SimpleNamespace(
            update_user=AsyncMock(side_effect=error),
            create_user=AsyncMock(),
            get_internal_squads=AsyncMock(return_value=[SimpleNamespace(uuid=WIFI)]),
        )
        with pytest.raises(RemnaWaveAPIError):
            await push_subscription(
                api, user, sub, db=db, identity=PanelIdentity(panel_user=panel), reset_devices=False, now=NOW
            )
        api.update_user.assert_awaited_once()
        assert set(api.update_user.await_args.kwargs['active_internal_squads']) == {WIFI, LTE}
        assert sub.pending_panel_expire_at == pending_expiry
        assert sub.connected_squads == [WIFI, LTE]
        assert sub.remnawave_id == 11
        assert sub.end_date.isoformat() == snapshot['subscription']['end_date']


@pytest.mark.parametrize('category', ['wifi', 'wifi_lte', None])
@pytest.mark.parametrize('entrypoint', ['standalone', 'renewal'])
async def test_grace_recovery_keeps_network_squads_without_an_outer_writer(monkeypatch, category, entrypoint):
    from app.services import grace_access_runtime as runtime
    from app.services.grace_access_service import GraceAccessMode, GraceBillingState
    from app.services.remnawave_service import remnawave_service

    billing = GraceBillingState(
        subscription_id=1,
        remnawave_id=11,
        status='active',
        end_at=NOW + timedelta(days=30),
        traffic_limit_bytes=0,
        used_traffic_bytes=0,
        device_limit=3,
        squad_uuids=(WIFI, LTE),
        network_category=category,
    )
    panel = sample(BASELINE)
    panel.expire_at = billing.end_at
    panel.traffic_limit_bytes = 0
    panel.used_traffic_bytes = 0
    panel.last_traffic_reset_at = None
    panel.hwid_device_limit = 3
    panel.active_internal_squads = [{'uuid': WIFI}]
    panel.external_squad_uuid = None
    error = RemnaWaveAPIError('squad removed', response_data={'errorCode': 'A018'})
    api = SimpleNamespace(
        update_user=AsyncMock(side_effect=[error, panel]),
        get_internal_squads=AsyncMock(return_value=[SimpleNamespace(uuid=WIFI)]),
    )

    @asynccontextmanager
    async def client():
        yield api

    monkeypatch.setattr(remnawave_service, 'get_api_client', client)
    core = SimpleNamespace(
        payment_has_recovered=AsyncMock(return_value=True), complete_after_payment=AsyncMock(return_value=True)
    )
    announced = MagicMock()
    if entrypoint == 'renewal':
        monkeypatch.setattr(runtime, 'grace_access_runtime', SimpleNamespace(mode=GraceAccessMode.ACTIVE))
        monkeypatch.setattr(runtime, '_build_core', lambda *args, **kwargs: core)
        monkeypatch.setattr(runtime.SQLAlchemyGraceBillingGateway, 'get_subscription', AsyncMock(return_value=billing))
        monkeypatch.setattr(
            'app.services.traffic_accounting_service.restore_managed_grace', AsyncMock(return_value=None)
        )
        monkeypatch.setattr(runtime, 'announce_grace_event_after_commit', announced)
        operation = runtime.apply_recovered_grace_update_locked(AsyncMock(), api, 1, update_kwargs={}, source='test')
    else:
        operation = runtime.RemnawaveGracePanelGateway().apply_billing_state(billing, expected_overlay=None)
    if category:
        with pytest.raises(RemnaWaveAPIError):
            await operation
        assert api.update_user.await_count == 1
        assert api.update_user.await_args.kwargs['active_internal_squads'] == [WIFI, LTE]
        core.complete_after_payment.assert_not_awaited()
        announced.assert_not_called()
    else:
        await operation
        assert api.update_user.await_count == 2
        assert api.update_user.await_args.kwargs['active_internal_squads'] == [WIFI]
    assert billing.squad_uuids == (WIFI, LTE)
