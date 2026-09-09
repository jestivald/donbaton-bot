"""Competing upstream paths must not undo the independent LTE ledger."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Subscription
from app.services.remnawave_service import RemnaWaveService
from app.services.remnawave_webhook_service import RemnaWaveWebhookService


@pytest.mark.asyncio
@pytest.mark.parametrize('handler', ['_handle_user_limited', '_handle_user_traffic_reset', '_handle_user_enabled'])
async def test_global_panel_events_never_refill_or_reactivate_managed(handler):
    sub = Subscription(
        id=1,
        network_category='wifi_lte',
        status='disabled',
        traffic_limit_gb=0,
        end_date=datetime.now(UTC) + timedelta(days=10),
        connected_squads=['wifi', 'lte'],
    )
    db = SimpleNamespace(commit=AsyncMock())
    service = RemnaWaveWebhookService(bot=None)
    service._notify_user = AsyncMock()
    await getattr(service, handler)(db, SimpleNamespace(id=1), sub, {'status': 'ACTIVE', 'trafficLimitBytes': 1000})
    assert sub.status == 'disabled'
    assert sub.traffic_limit_gb == 0
    assert sub.connected_squads == ['wifi', 'lte']
    service._notify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_reverse_sync_preserves_purchased_squads_and_status(monkeypatch):
    sub = Subscription(
        id=1,
        network_category='wifi_lte',
        status='disabled',
        traffic_limit_gb=0,
        end_date=datetime.now(UTC) + timedelta(days=10),
        connected_squads=['wifi', 'lte'],
    )
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=sub))
    db = SimpleNamespace()
    await RemnaWaveService()._update_subscription_from_panel_data(
        db,
        SimpleNamespace(id=1),
        {'status': 'ACTIVE', 'activeInternalSquads': ['wifi'], 'trafficLimitBytes': 999},
    )
    assert sub.status == 'disabled'
    assert sub.traffic_limit_gb == 0
    assert sub.connected_squads == ['wifi', 'lte']
