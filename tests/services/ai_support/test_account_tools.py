"""Разрешённые поля аккаунта и устройств без личных и панельных идентификаторов."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import settings
from app.services.ai_support import api, tools
from app.services.ai_support.settings_view import snapshot


class DB:
    def __init__(self, user, transactions=()):
        self.user = user
        self.transactions = transactions

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, *args):
        return self.user

    async def execute(self, *args):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.transactions))


def subscription():
    return SimpleNamespace(
        network_category='wifi_lte',
        tariff=SimpleNamespace(name='Wi-Fi + LTE', network_category='wifi_lte'),
        end_date=datetime.now(UTC) + timedelta(days=10),
        status='active',
        is_trial=False,
        device_limit=3,
        autopay_enabled=True,
        remnawave_id=77,
    )


async def test_account_overview_has_only_allowed_fields(monkeypatch):
    from app.services import donbaton_payment_window, lte_quota_service

    user = SimpleNamespace(
        telegram_id=987654321, email='private@example.com', email_verified=True, balance_kopeks=129900, remnawave_id=88
    )
    transaction = SimpleNamespace(
        type='subscription_payment',
        created_at=datetime.now(UTC),
        amount_kopeks=29900,
        payment_method='yookassa',
        is_completed=True,
    )
    monkeypatch.setattr(tools, 'AsyncSessionLocal', lambda: DB(user, [transaction]))
    monkeypatch.setattr(tools, 'subscriptions_for', AsyncMock(return_value=[subscription()]))
    monkeypatch.setattr(donbaton_payment_window, 'payment_window', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        lte_quota_service,
        'get_quota_status',
        AsyncMock(
            return_value={
                'remaining_bytes': 3 * 1024**3,
                'limit_bytes': 5 * 1024**3,
                'topup_bytes': 0,
                'is_unlimited': False,
                'period_ends_at': datetime.now(UTC).isoformat(),
                'accounting_state': 'ready',
            }
        ),
    )
    result = await tools.account_overview(1)
    assert result['balance_rub'] == 1299
    assert result['telegram_login'] and result['email_login']
    assert result['subscriptions'][0]['category'] == 'Wi-Fi + LTE'
    assert result['subscriptions'][0]['lte']['remaining_gb'] == 3
    assert result['subscriptions'][0]['lte']['package_gb'] == 5
    assert result['transactions'][0]['type'] == 'покупка подписки'
    encoded = json.dumps(result)
    assert '987654321' not in encoded and 'private@example.com' not in encoded
    assert 'subscription_id' not in encoded and 'remnawave' not in encoded


async def test_devices_uses_existing_api_and_discards_hwid(monkeypatch):
    from app.services.remnawave_service import RemnaWaveService

    user = SimpleNamespace(remnawave_id=88)
    monkeypatch.setattr(tools, 'AsyncSessionLocal', lambda: DB(user))
    monkeypatch.setattr(tools, 'subscriptions_for', AsyncMock(return_value=[subscription()]))
    get_devices = AsyncMock(
        return_value={
            'devices': [
                {
                    'hwid': 'private-hwid',
                    'platform': 'iOS',
                    'deviceModel': 'iPhone 16',
                    'userAgent': 'Happ/4.11.0',
                    'createdAt': '2026-11-07T09:00:00Z',
                    'updatedAt': '2026-11-07T10:00:00Z',
                }
            ]
        }
    )

    class Client:
        async def __aenter__(self):
            return SimpleNamespace(get_user_devices_all=get_devices)

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(RemnaWaveService, 'get_api_client', lambda self: Client())
    result = await tools.devices(1)
    get_devices.assert_awaited_once_with(77)
    assert result['devices'][0]['application'] == 'Happ'
    assert result['devices'][0]['version'] == '4.11.0'
    assert result['devices'][0]['added_at'] == '07.11.2026 12:00 МСК'
    assert 'private-hwid' not in json.dumps(result) and '88' not in json.dumps(result)


async def test_account_error_and_permission_gates(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_SHARE_ACCOUNT_DATA', True)
    overview = AsyncMock(side_effect=RuntimeError('private error'))
    monkeypatch.setattr(tools, 'account_overview', overview)
    call = {'function': {'name': 'get_account_overview', 'arguments': '{}'}}
    state = tools.ToolState()
    assert (
        await tools.execute(call, api.AiRequestContext(api.AiChannel.BOT_CHAT, 1), snapshot(), state)
        == tools.UNAVAILABLE
    )
    overview.reset_mock()
    for context in (
        api.AiRequestContext(api.AiChannel.ADMIN_TEST, None),
        api.AiRequestContext(api.AiChannel.BOT_CHAT, 1, allow_account_tools=False),
    ):
        assert len(tools.definitions(context, snapshot())) == 1
        assert await tools.execute(call, context, snapshot(), state) == tools.UNAVAILABLE
    overview.assert_not_awaited()
