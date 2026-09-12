"""Клиент панели, взятый из сервиса, переживает соседа по процессу.

12.09.2026, боевой бот: `RuntimeError: Session is closed` из цикла LTE-квот, 248
штук в час, ровно раз в час пачкой. `SubscriptionService.get_api_client()` отдавал
ОБЩИЙ `self.api`, а `RemnaWaveAPI.__aenter__` перезаписывает `self.session` и
`__aexit__` её закрывает — счётчика входов у клиента нет. `run_quota_cycle` держит
клиента открытым на весь `asyncio.gather` по сотням подписок; `monitoring_service`
держит ОДИН экземпляр `SubscriptionService` и на своих же задачах (`_lte_loop` и
цикл мониторинга) входил в того же клиента вторым — выход соседа закрывал сессию
под живыми запросами цикла.

Сторож держит оба сервиса: `RemnaWaveService` починили раньше и той же правкой,
и откат любого из них должен падать здесь, а не через сутки в боевом логе.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from app.config import settings
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService


#: Порт, на котором заведомо никто не слушает: живая сессия упрётся в отказ
#: соединения, мёртвая — выдаст RuntimeError ещё до сети.
DEAD_URL = 'http://127.0.0.1:1/api/system/stats'


@pytest.fixture
def configured(monkeypatch):
    """Минимально настроенная панель: сеть не нужна, нужен только валидный конфиг."""
    monkeypatch.setattr(settings, 'REMNAWAVE_API_URL', 'http://127.0.0.1:1', raising=False)
    monkeypatch.setattr(settings, 'REMNAWAVE_API_KEY', 'test-key', raising=False)
    monkeypatch.setattr(settings, 'REMNAWAVE_SECRET_KEY', None, raising=False)
    monkeypatch.setattr(settings, 'REMNAWAVE_USERNAME', None, raising=False)
    monkeypatch.setattr(settings, 'REMNAWAVE_PASSWORD', None, raising=False)
    monkeypatch.setattr(settings, 'REMNAWAVE_CADDY_TOKEN', None, raising=False)
    monkeypatch.setattr(settings, 'REMNAWAVE_AUTH_TYPE', 'api_key', raising=False)


async def _assert_session_alive(api, *, whose: str) -> None:
    """Сессия обязана дожить до запроса: до патча тут был `Session is closed`."""
    assert api.session is not None, f'{whose}: сессия не создана'
    assert not api.session.closed, f'{whose}: сессию закрыл сосед по процессу'
    try:
        async with api.session.get(DEAD_URL):
            pass
    except RuntimeError as error:  # pragma: no cover - ветка падения до патча
        pytest.fail(f'{whose}: запрос не ушёл — {error}')
    except (aiohttp.ClientError, OSError, TimeoutError):
        # Соединения нет и не должно быть: важно, что запрос дошёл до сети.
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('factory', [SubscriptionService, RemnaWaveService], ids=['subscription', 'remnawave'])
async def test_inner_client_exit_does_not_close_outer_session(configured, factory):
    """Вложенный клиент закрылся — внешний всё ещё делает запросы."""
    service = factory()

    async with service.get_api_client() as outer:
        async with service.get_api_client() as inner:
            assert inner is not outer, 'сервис отдал ОБЩИЙ экземпляр клиента'
            await _assert_session_alive(inner, whose='вложенный')
        # Сосед вышел и закрыл СВОЮ сессию. Внешний клиент этого знать не должен.
        await _assert_session_alive(outer, whose='внешний')


@pytest.mark.asyncio
@pytest.mark.parametrize('factory', [SubscriptionService, RemnaWaveService], ids=['subscription', 'remnawave'])
async def test_quota_cycle_survives_parallel_monitoring_pass(configured, factory):
    """Боевая расстановка: долгий цикл квот и короткий проход мониторинга рядом.

    Один экземпляр сервиса на обе задачи — именно так живёт `monitoring_service`.
    """
    service = factory()
    monitoring_done = asyncio.Event()

    async def monitoring_pass():
        # Короткий проход: вошёл, сходил в панель, вышел — и закрыл за собой сессию.
        async with service.get_api_client() as api:
            await _assert_session_alive(api, whose='мониторинг')
        monitoring_done.set()

    async def quota_cycle():
        async with service.get_api_client() as api:
            task = asyncio.create_task(monitoring_pass())
            await monitoring_done.wait()
            await task
            # Цикл всё ещё внутри своего `async with` — здесь и рвалось на бою.
            await _assert_session_alive(api, whose='цикл квот')

    await quota_cycle()


@pytest.mark.asyncio
@pytest.mark.parametrize('factory', [SubscriptionService, RemnaWaveService], ids=['subscription', 'remnawave'])
async def test_each_call_yields_its_own_client(configured, factory):
    """Каждый вызов — свой экземпляр и своя сессия, и обе закрываются за собой."""
    service = factory()

    async with service.get_api_client() as first:
        first_session = first.session
    async with service.get_api_client() as second:
        second_session = second.session

    assert first is not second
    assert first_session is not second_session
    assert first_session.closed and second_session.closed, 'сессию за собой не закрыли'
