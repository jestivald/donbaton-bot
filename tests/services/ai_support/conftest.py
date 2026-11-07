"""Изолированные тесты ядра: SQLite без async-драйвера и только MockTransport."""

from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import AiSupportRequest
from app.services.ai_support import knowledge, limits, provider, repository, retention, service


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    for key, field in type(settings).model_fields.items():
        if key.startswith('AI_SUPPORT_'):
            monkeypatch.setattr(settings, key, field.default)
    monkeypatch.setattr(settings, 'AI_SUPPORT_ENABLED', True)
    monkeypatch.setattr(settings, 'AI_SUPPORT_API_KEY', 'test-only-key')
    knowledge._cache.clear()
    limits._users.clear()
    limits.invalidate_budget()
    service._alert_times.clear()
    service.set_admin_alert_handler(None)
    monkeypatch.setattr(service, '_provider_failures', 0)


@pytest.fixture
def journal(monkeypatch):
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE tickets (id INTEGER PRIMARY KEY)')
    AiSupportRequest.__table__.create(engine)

    class LocalSession:
        async def __aenter__(self):
            self.session = Session(engine)
            return self

        async def __aexit__(self, *args):
            self.session.close()

        def get_bind(self):
            return engine

        async def execute(self, statement):
            return self.session.execute(statement)

        async def get(self, model, key):
            return self.session.get(model, key)

        async def commit(self):
            self.session.commit()

    for module in (repository, limits, retention):
        monkeypatch.setattr(module, 'AsyncSessionLocal', LocalSession)
    yield SimpleNamespace(engine=engine, session=LocalSession)
    engine.dispose()


@pytest.fixture
def transport(monkeypatch):
    clients = []

    def install(handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        monkeypatch.setattr(provider, '_client', client)
        monkeypatch.setattr(provider, '_proxy', '')
        return client

    yield install
    # MockTransport не держит сокеты, закрытие проверяется отдельно.
