"""Протокол, ограниченный повтор, дедлайн и учёт стоимости без сети."""

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.config import settings
from app.services.ai_support import pricing, provider
from app.services.ai_support.settings_view import snapshot


@pytest.mark.parametrize(('kind', 'thinking'), [('deepseek', False), ('deepseek', True), ('openai_compatible', False)])
async def test_body_and_usage(transport, monkeypatch, kind, thinking):
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER', kind)
    monkeypatch.setattr(settings, 'AI_SUPPORT_THINKING_ENABLED', thinking)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        assert request.headers['authorization'] == 'Bearer test-only-key'
        return httpx.Response(
            200,
            text='\n \n'
            + json.dumps(
                {
                    'choices': [{'message': {'role': 'assistant', 'content': 'Ответ'}, 'finish_reason': 'stop'}],
                    'usage': {
                        'prompt_tokens': 30,
                        'prompt_cache_hit_tokens': 10,
                        'prompt_cache_miss_tokens': 20,
                        'completion_tokens': 12,
                        'completion_tokens_details': {'reasoning_tokens': 5},
                    },
                }
            ),
        )

    transport(handler)
    config = snapshot()
    result = await provider.complete(
        [{'role': 'user', 'content': 'Вопрос'}], [{'type': 'function'}], config, deadline=time.monotonic() + 5
    )
    body = bodies[0]
    assert 'user_id' not in body and 'user' not in body
    assert body['tools'] == [{'type': 'function'}] and body['tool_choice'] == 'auto'
    if kind == 'deepseek':
        assert body['thinking'] == {'type': 'enabled' if thinking else 'disabled'}
        assert ('reasoning_effort' in body) == thinking
    else:
        assert 'thinking' not in body
    assert result.usage == provider.Usage(30, 10, 20, 12, 5)
    assert provider.parse_usage({}) == provider.Usage()


@pytest.mark.parametrize(
    ('status', 'code', 'attempts'),
    [
        (400, 'bad_request', 1),
        (401, 'auth', 1),
        (403, 'auth', 1),
        (402, 'balance', 1),
        (422, 'invalid', 1),
        (429, 'rate_limit', 2),
        (500, 'provider_error', 2),
        (503, 'provider_error', 2),
    ],
)
async def test_retries(transport, status, code, attempts):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Retry-After': '0'})

    transport(handler)
    with pytest.raises(provider.ProviderError, match=code):
        await provider.complete([], [], snapshot(), deadline=time.monotonic() + 5)
    assert len(calls) == attempts


async def test_network_retry_and_retry_after_deadline(transport):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError('private details must not escape')

    transport(handler)
    with pytest.raises(provider.ProviderError, match='network') as exc:
        await provider.complete([], [], snapshot(), deadline=time.monotonic() + 5)
    assert 'private' not in str(exc.value)
    assert len(calls) == 2
    calls.clear()
    transport(lambda request: calls.append(request) or httpx.Response(429, headers={'Retry-After': '3600'}))
    with pytest.raises(provider.ProviderError):
        await provider.complete([], [], snapshot(), deadline=time.monotonic() + 1)
    assert len(calls) == 1


async def test_deadline(transport):
    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200)

    transport(slow)
    start = time.monotonic()
    with pytest.raises(provider.ProviderError, match='timeout'):
        await provider.complete([], [], snapshot(), deadline=start + 0.02)
    assert time.monotonic() - start < 0.15


@pytest.mark.parametrize(
    ('message', 'finish', 'code'),
    [
        ({'content': ''}, 'stop', 'empty'),
        ({'content': 'Половина ответа'}, 'length', 'truncated'),
        ({'content': None}, 'stop', 'empty'),
    ],
)
async def test_empty_truncated(transport, message, finish, code):
    transport(lambda request: httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': finish}]}))
    with pytest.raises(provider.ProviderError, match=code):
        await provider.complete([], [], snapshot(), deadline=time.monotonic() + 1)


def test_peak_and_cost():
    assert pricing.is_peak(datetime(2026, 11, 6, 1, tzinfo=UTC))
    assert not pricing.is_peak(datetime(2026, 11, 7, 1, tzinfo=UTC))
    assert not pricing.is_peak(datetime(2026, 11, 6, 4, tzinfo=UTC))
    usage = provider.Usage(1_000_000, 300_000, 700_000, 100_000, 50_000)
    peak = pricing.cost(usage, snapshot(), datetime(2026, 11, 6, 6, tzinfo=UTC))
    assert peak == Decimal('0.3318')
    assert pricing.cost(usage, snapshot(), datetime(2026, 11, 7, 6, tzinfo=UTC)) == peak / 2
    assert pricing.cost(usage, replace(snapshot(), model='custom')) == peak
    custom = replace(snapshot(), model='custom', input_hit=1, input_miss=2, output_price=3)
    assert pricing.cost(usage, custom) == Decimal(2)
    # Без данных о кэше весь prompt идёт по miss, reasoning не прибавляется ещё раз.
    assert pricing.cost(provider.Usage(prompt_tokens=1_000_000), custom) == Decimal(2)
