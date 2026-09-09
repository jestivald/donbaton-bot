"""Synthetic pipeline evidence for domain tests; real Redis is never contacted.

Heartbeat tests explicitly restore the production reader and exercise missing,
stale and malformed proofs against an in-memory Redis stub.
"""

from datetime import UTC, datetime

import pytest

from app.config import settings
from app.services import lte_quota_service


TEST_LTE_NODES = [
    '00000000-0000-4000-8000-000000000011',
    '00000000-0000-4000-8000-000000000012',
]


@pytest.fixture(autouse=True)
def synthetic_telemetry(monkeypatch):
    monkeypatch.setattr(settings, 'DONBATON_LTE_ACCOUNTING_EPOCH', 'synthetic-accounting-epoch')
    monkeypatch.setattr(settings, 'DONBATON_LTE_NODE_UUIDS', ','.join(TEST_LTE_NODES))

    async def observed_heartbeat(*, now=None):
        return {
            'version': 1,
            'epoch': settings.DONBATON_LTE_ACCOUNTING_EPOCH,
            'observed_at': (now or datetime.now(UTC)).isoformat(),
            'node_ids': list(TEST_LTE_NODES),
        }

    monkeypatch.setattr(lte_quota_service, '_read_telemetry_heartbeat', observed_heartbeat)
